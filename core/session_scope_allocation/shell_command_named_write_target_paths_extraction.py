"""从 shell 命令文本里识别「命令写明了要写的文件」——session 归属的第 2 层证据（中强度）。

背景（08ac4 调查，``docs/investigations/rvf-session-owned-attribution-blind-to-bash-and-subagent-writes-history-2026-10-04/``）：
session-owned 归属原先只认 Edit/Write/apply_patch，Bash / Codex exec 写的文件一律落进
protected/background，2026-10 本会话写过的文件 68% 漏审。本模块把一条命令按 ``&&``、``||``、
``;``、``|``、``&``、换行、括号切成命令段（heredoc 正文归属引出它的那一段），再按每段的命令种类
只提取**真实写入目标**：

- 重定向 / ``tee`` 的目标；
- ``sed -i`` / ``perl -i`` / ``yq -i`` / formatter 写模式 / ``rm`` / ``touch`` / ``truncate`` 的文件参数；
- ``mv`` 的源与目标、``cp`` / ``install`` / ``rsync`` / ``ditto`` / ``ln`` 的目标（目标是目录时补
  「目录/源文件名」）；
- ``git rm`` / ``git mv`` / ``git restore`` / ``git checkout -- <path>`` 的文件参数，``git apply`` 与
  ``patch`` 的 heredoc 补丁文件头；
- ``curl -o`` / ``wget -O`` 的输出文件；
- ``sort -o`` 的输出文件、``uniq`` / ``xxd`` 的第二个位置参数（输出文件）；
- python / node 内联脚本（``-c`` / ``-e`` 正文，或从 heredoc / 管道 heredoc 读 stdin 的正文）里的写文件
  调用：能把每个写调用的目标还原成字面量路径时只认这些目标（精确），否则退回「正文里以完整单行字符串
  字面量出现、且在脏路径集合里的路径」（启发式）。

会改文件但落不到具体路径的命令段（formatter 对目录、``git stash pop``、``git merge`` / ``rebase``、
``xargs rm``、目标是未解析变量的重定向…）记为「批量触碰」，供时间窗层降级为「有争议」。

解析器从 08ac4 只读统计脚本移植（那里已经过 RVF 双评审与 40 条抽查），这里只改了两点：
输出直接对照「当前脏路径集合」解析；不依赖统计脚本的任何候选集或 run 上下文。纯函数、无 I/O。
"""

from __future__ import annotations

import collections
import fnmatch
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SHELL_REDIRECTION_DEVICE_TARGETS_THAT_ARE_NOT_FILES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
SHELL_CONTROL_KEYWORDS_BEFORE_COMMAND_WORD = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "time"}
SHELL_COMMAND_WRAPPERS_BEFORE_COMMAND_WORD = {"sudo", "env", "command", "nohup", "builtin", "exec", "nice", "caffeinate", "noglob"}
SHELL_WORD_ASSIGNMENT_PATTERN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$", re.DOTALL)
SHELL_VARIABLE_REFERENCE_PATTERN = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
SHELL_MAXIMUM_COMMAND_SUBSTITUTION_NESTING_DEPTH = 16
SHELL_WRITE_TRIGGER_COMMAND_EXCERPT_MAXIMUM_CHARACTERS = 200
INLINE_SCRIPT_INTERPRETER_NAME_PATTERN = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|node|nodejs|bun|deno|tsx|ts-node)$")
# python / node 内联脚本正文里的「写文件调用」。不含 sys.stdout/stderr.write、pandas 的 .rename(...) 之类非文件写入。
INLINE_SCRIPT_FILE_WRITE_CALL_PATTERN = re.compile(
    r"write_text\(|write_bytes\(|open\([^)]*['\"](?:w|a|x)[bt+]?['\"]|(?<!stdout)(?<!stderr)\.write\(|"
    r"shutil\.(?:copy\w*|move|rmtree)\(|os\.(?:rename|replace|remove|unlink)\(|\.unlink\(|"
    r"\.to_(?:csv|json|parquet|excel|html|pickle|feather)\(|\.savefig\(|"
    r"writeFileSync\(|writeFile\(|appendFileSync\(|copyFileSync\(|renameSync\(|unlinkSync\(|fs\.rm(?:Sync)?\(|Bun\.write\("
)
FORMATTER_OR_LINTER_COMMAND_NAMES = {
    "biome", "prettier", "eslint", "oxlint", "ruff", "black", "isort", "gofmt", "goimports", "shfmt", "rustfmt",
    "clang-format", "autopep8", "yapf", "dprint",
}
FORMATTER_OR_LINTER_SUBCOMMAND_WORDS = {"format", "check", "lint", "fmt", "ci"}
# heredoc / 参数里的补丁文本文件头：Codex apply_patch 格式与 unified diff 格式。
PATCH_TEXT_FILE_HEADER_PATTERN = re.compile(
    r"^(?:\*\*\* (?:Add|Update|Delete) File: (?P<apply_patch_header>.+?)|\*\*\* Move to: (?P<apply_patch_move_to>.+?)|"
    r"\+\+\+ b/(?P<unified_diff_new_path>\S+)|--- a/(?P<unified_diff_old_path>\S+))\s*$",
    re.MULTILINE,
)
PACKAGE_MANAGER_SCRIPT_NAMES_THAT_REWRITE_FILES_PATTERN = re.compile(r"^(?:format|fmt|fix|lint:fix|lint-fix|write|prettier|biome:fix)(?::\S*)?$")
GIT_SUBCOMMANDS_THAT_REWRITE_WORK_TREE_IN_BULK = {"merge", "rebase", "pull", "cherry-pick", "revert", "am", "switch"}
# 内联脚本里「被当作路径使用」的字符串：只认单行字符串字面量的完整内容。三引号字符串（多为要写进文件的正文）
# 先整体剔除，避免正文里顺带提到的其他文件路径被当成写入目标。
INLINE_SCRIPT_TRIPLE_QUOTED_STRING_PATTERN = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN = re.compile(
    r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\\n]|\\.)*)`')
# 带「目标参数」的写调用：能读出目标表达式时，只把目标当作写入（精确模式）；读不出时退回「字面量提及」启发式。
INLINE_SCRIPT_DESTINATION_BEARING_WRITE_CALL_PATTERNS = (
    re.compile(r"\bopen\(\s*(?P<destination>[^,()]+?)\s*,\s*(?:mode\s*=\s*)?[rbf]?['\"][wax]"),
    re.compile(r"(?P<destination>Path\(\s*[^()]*?\)|[A-Za-z_][\w.]*)\s*\.\s*(?:write_text|write_bytes|unlink|touch)\("),
    re.compile(r"(?P<destination>Path\(\s*[^()]*?\)|[A-Za-z_][\w.]*)\s*\.\s*open\(\s*(?:mode\s*=\s*)?['\"][wax]"),
    re.compile(r"(?:writeFileSync|writeFile|appendFileSync|Bun\.write)\(\s*(?P<destination>[^,()]+?)\s*,"),
    re.compile(r"\.to_(?:csv|json|parquet|excel|html|pickle|feather)\(\s*(?P<destination>[^,()]+?)\s*[,)]"),
    re.compile(r"\.savefig\(\s*(?P<destination>[^,()]+?)\s*[,)]"),
    re.compile(r"shutil\.(?:copy\w*|move)\(\s*[^,()]+?\s*,\s*(?P<destination>[^,()]+?)\s*[,)]"),
    re.compile(r"\bos\.(?:rename|replace)\(\s*(?P<destination>[^,()]+?)\s*,"),
    re.compile(r"\bos\.(?:rename|replace)\(\s*[^,()]+?\s*,\s*(?P<destination>[^,()]+?)\s*\)"),
    re.compile(r"\bos\.(?:remove|unlink)\(\s*(?P<destination>[^,()]+?)\s*\)"),
    re.compile(r"\b(?:fs\.)?(?:rmSync|unlinkSync|renameSync|copyFileSync)\(\s*(?P<destination>[^,()]+?)\s*[,)]"),
)
INLINE_SCRIPT_VARIABLE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|;)[ \t]*(?:const\s+|let\s+|var\s+)?(?P<name>[A-Za-z_]\w*)\s*(?::\s*[\w\[\]]+\s*)?=\s*(?P<expression>[^\n;=][^\n;]*)",
    re.MULTILINE)
INLINE_SCRIPT_FOR_LOOP_OVER_LITERAL_LIST_PATTERN = re.compile(
    r"\bfor\s+(?P<name>[A-Za-z_]\w*)\s+in\s+[\[(](?P<items>[^\]\)\n]*)[\])]")
# 只由标识符、/、Path(…)、os.path.join / path.join / path.resolve 与字符串字面量拼成的路径表达式
INLINE_SCRIPT_PATH_EXPRESSION_GLUE_PATTERN = re.compile(
    r"^(?:\s|[A-Za-z_][\w.]*\s*/|/|,|\(|\)|(?:pathlib\.)?Path|os\.path\.join|path\.join|path\.resolve|\.resolve\(\))*$")
# EXPERIMENT_NODE: 写入目标解析不到确切路径时，是否允许按「脏路径里唯一的同名文件」兜底，以及兜底要求的最短文件名长度。
MINIMUM_UNIQUE_BASENAME_LENGTH_FOR_BASENAME_FALLBACK_MATCH = 10
FORMATTER_OR_INTERPRETER_RUNNER_COMMAND_NAMES = {
    "npx", "bunx", "pnpm", "pnpx", "yarn", "uv", "uvx", "poetry", "pipx", "pdm", "hatch", "bun", "python", "python3",
}
XARGS_OR_FIND_EXEC_WRITER_COMMAND_NAMES = {
    "rm", "sed", "perl", "mv", "cp", "touch", "truncate", "git", "npx", "bunx", "chmod",
} | FORMATTER_OR_LINTER_COMMAND_NAMES

INLINE_SCRIPT_MENTIONED_PATH_WRITE_KIND = "inline_python_or_node_script_write_call_with_path_mention"
INLINE_SCRIPT_LITERAL_DESTINATION_WRITE_KIND = "inline_python_or_node_script_write_call_literal_destination"
# ``git restore`` / ``git checkout -- <path>`` 是把文件还原成某个提交里的版本：会改文件，但不是「这段 diff 的作者」证据。
GIT_RESTORE_OR_CHECKOUT_FILE_ARGUMENT_WRITE_KIND = "git_restore_or_checkout_file_argument"
WRITE_KINDS_THAT_ARE_NOT_AUTHORSHIP_EVIDENCE = frozenset({GIT_RESTORE_OR_CHECKOUT_FILE_ARGUMENT_WRITE_KIND})

# 只读命令：不开「可能写入」的时间窗（RVF reviewer 在同一 worktree 跑的 rg / git diff / 测试只读部分，
# 不该被当成并发写入者；主会话等 reviewer 的 ``rvf wait`` 也不该被当成一段写入窗口）。
# EXPERIMENT_NODE: 只读命令名单的宽窄——宽了会把真写入当只读（时间窗漏判），窄了会把只读当写入（争议变多）。
# 解释器 / 脚本执行（awk、source、``.``）一律按不透明处理：awk 的 ``print > "file"``、被 source 的脚本都能写文件。
# sort / uniq / xxd 能把输出写进文件，按参数判定（见 _sort_or_uniq_or_xxd_output_file_write_effect），不在本名单里。
READ_ONLY_SHELL_COMMAND_NAMES = frozenset({
    "rg", "grep", "egrep", "fgrep", "ag", "cat", "less", "more", "head", "tail", "wc", "nl", "cut",
    "tr", "column", "ls", "tree", "pwd", "cd", "pushd", "popd", "echo", "printf", "which", "type", "command", "whereis",
    "date", "sleep", "true", "false", "test", "[", "[[", "stat", "file", "du", "df", "ps", "lsof", "realpath", "readlink",
    "dirname", "basename", "diff", "cmp", "comm", "shasum", "sha256sum", "md5", "md5sum", "od", "hexdump", "jq",
    "yq", "env", "printenv", "id", "whoami", "hostname", "uname", "tput", "export", "set", "unset", "local", "declare",
    "readonly", "typeset", "exit", "return", "wait", "time", "history", "fd", "bat",
})
# sed 脚本里不带参数、也不会写文件的单字母命令（``l`` / ``L`` / ``q`` / ``Q`` 可带一个数字参数）。
SED_SCRIPT_COMMANDS_WITHOUT_FILE_WRITE_EFFECT = frozenset("=dDgGhHnNpPxzFlLqQ")
# git commit 的这些选项让调用成功退出却不产生提交（``--short`` / ``--porcelain`` / ``--long`` 隐含 ``--dry-run``）。
GIT_COMMIT_OPTIONS_THAT_DO_NOT_CREATE_A_COMMIT = frozenset({"--dry-run", "--short", "--porcelain", "--long", "-h", "--help"})
READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files", "ls-tree", "blame", "grep", "describe",
    "merge-base", "cat-file", "for-each-ref", "shortlog", "whatchanged", "name-rev", "reflog", "remote", "config",
    "symbolic-ref", "show-ref", "count-objects", "check-ignore", "check-attr", "var", "help", "version",
})
READ_ONLY_RVF_SUBCOMMANDS = frozenset({"wait", "status", "results", "handoff", "--help", "help"})


# --------------------------------------------------------------------------------------------
# shell 命令 → 命令段
# --------------------------------------------------------------------------------------------


@dataclass
class ShellCommandSegment:
    """命令里由 &&、||、;、|、&、换行、括号切出的一段。heredoc 正文归属引出它的那一段。"""

    shell_text: str = ""
    words: list[str] = field(default_factory=list)  # 去引号后的词（不含重定向目标、heredoc 定界符）
    redirections: list[tuple[str, str]] = field(default_factory=list)  # (含 fd 前缀的操作符, 目标词)
    heredoc_bodies: list[str] = field(default_factory=list)
    heredoc_delimiters_awaiting_body: list[tuple[str, bool]] = field(default_factory=list)
    # 本段的 stdin 来自管道上一段（``cat <<'EOF' | python3 -`` 的脚本正文在上一段的 heredoc 里）
    pipe_source_segment: "ShellCommandSegment | None" = None


def _find_unescaped_character(text: str, character: str, start: int) -> int:
    position = start
    while position < len(text):
        found = text.find(character, position)
        if found == -1:
            return len(text)
        if found > 0 and text[found - 1] == "\\":
            position = found + 1
            continue
        return found
    return len(text)


def split_shell_command_into_segments(command: str) -> list[ShellCommandSegment]:
    segments: list[ShellCommandSegment] = []
    _scan_shell_command_into_segments(command, 0, False, segments, 0)
    return segments


def _scan_shell_command_into_segments(
    command: str, start: int, stop_at_unmatched_close_parenthesis: bool, segments: list[ShellCommandSegment], nesting_depth: int
) -> int:
    """逐字符扫描：引号、转义、``$( )``、反引号、heredoc 正文里的 ``>``、``;``、``|`` 都不会被当成 shell 语法。
    ``$( )`` / ``<( )`` 递归扫描，里面的命令段也加入 segments。返回扫描停止处（``)`` 之后）的下标。"""
    length = len(command)
    index = start
    segment_start = start
    current = ShellCommandSegment()
    segments_since_last_newline: list[ShellCommandSegment] = []
    word_characters: list[str] = []
    word_started = False
    word_contains_quoting = False
    pending_redirection_operator: str | None = None
    pending_heredoc_strips_leading_tabs: bool | None = None
    parenthesis_depth = 0

    def finish_word() -> None:
        nonlocal word_characters, word_started, word_contains_quoting
        nonlocal pending_redirection_operator, pending_heredoc_strips_leading_tabs
        if not word_started:
            return
        word = "".join(word_characters)
        if pending_heredoc_strips_leading_tabs is not None:
            current.heredoc_delimiters_awaiting_body.append((word, pending_heredoc_strips_leading_tabs))
            pending_heredoc_strips_leading_tabs = None
        elif pending_redirection_operator is not None:
            current.redirections.append((pending_redirection_operator, word))
            pending_redirection_operator = None
        else:
            current.words.append(word)
        word_characters = []
        word_started = False
        word_contains_quoting = False

    def finish_segment(end_index: int, separator: str) -> None:
        nonlocal current, pending_redirection_operator, pending_heredoc_strips_leading_tabs
        finish_word()
        pending_redirection_operator = None
        pending_heredoc_strips_leading_tabs = None
        current.shell_text = command[segment_start:end_index].strip()
        finished: ShellCommandSegment | None = None
        if current.words or current.redirections or current.heredoc_delimiters_awaiting_body:
            segments.append(current)
            segments_since_last_newline.append(current)
            finished = current
        current = ShellCommandSegment(pipe_source_segment=finished if separator == "|" else None)

    def consume_heredoc_bodies_after_newline(newline_index: int) -> int:
        position = newline_index + 1
        for segment in segments_since_last_newline:
            for delimiter, strips_leading_tabs in segment.heredoc_delimiters_awaiting_body:
                body_lines: list[str] = []
                while position < length:
                    line_end = command.find("\n", position)
                    line_end = length if line_end == -1 else line_end
                    line = command[position:line_end]
                    position = line_end + 1
                    comparable_line = line.lstrip("\t") if strips_leading_tabs else line
                    if comparable_line.rstrip() == delimiter:
                        break
                    body_lines.append(line)
                segment.heredoc_bodies.append("\n".join(body_lines))
            segment.heredoc_delimiters_awaiting_body = []
        segments_since_last_newline.clear()
        return min(position, length)

    def scan_nested_command_substitution(position: int) -> int:
        if nesting_depth >= SHELL_MAXIMUM_COMMAND_SUBSTITUTION_NESTING_DEPTH:
            closing = command.find(")", position)
            return length if closing == -1 else closing + 1
        return _scan_shell_command_into_segments(command, position, True, segments, nesting_depth + 1)

    def read_double_quoted(position: int) -> int:
        while position < length:
            character = command[position]
            if character == '"':
                return position + 1
            if character == "\\" and position + 1 < length:
                escaped = command[position + 1]
                if escaped == "\n":
                    position += 2
                    continue
                if escaped in '"\\$`':
                    word_characters.append(escaped)
                    position += 2
                    continue
                word_characters.append(character)
                position += 1
                continue
            if character == "$" and command.startswith("$(", position):
                end = scan_nested_command_substitution(position + 2)
                word_characters.append(command[position:end])
                position = end
                continue
            if character == "`":
                closing = _find_unescaped_character(command, "`", position + 1)
                word_characters.append(command[position:closing + 1])
                position = closing + 1
                continue
            word_characters.append(character)
            position += 1
        return length

    def read_ansi_c_quoted(position: int) -> int:
        decoded: list[str] = []
        while position < length and command[position] != "'":
            if command[position] == "\\" and position + 1 < length:
                escaped = command[position + 1]
                decoded.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'"}.get(escaped, "\\" + escaped))
                position += 2
                continue
            decoded.append(command[position])
            position += 1
        word_characters.append("".join(decoded))
        return position + 1

    while index < length:
        character = command[index]
        following = command[index + 1] if index + 1 < length else ""
        if character == "\n":
            finish_segment(index, "\n")
            index = consume_heredoc_bodies_after_newline(index)
            segment_start = index
            continue
        if character in " \t\r":
            finish_word()
            index += 1
            continue
        if character == "\\":
            if following == "\n":
                index += 2
                continue
            word_characters.append(following)
            word_started = True
            word_contains_quoting = True
            index += 2
            continue
        if character == "'":
            closing = command.find("'", index + 1)
            closing = length if closing == -1 else closing
            word_characters.append(command[index + 1:closing])
            word_started = True
            word_contains_quoting = True
            index = closing + 1
            continue
        if character == '"':
            word_started = True
            word_contains_quoting = True
            index = read_double_quoted(index + 1)
            continue
        if character == "`":
            closing = _find_unescaped_character(command, "`", index + 1)
            word_characters.append(command[index:closing + 1])
            word_started = True
            index = closing + 1
            continue
        if character == "$" and following == "(":
            end = scan_nested_command_substitution(index + 2)
            word_characters.append(command[index:end])
            word_started = True
            index = end
            continue
        if character == "$" and following == "{":
            closing = command.find("}", index + 2)
            closing = length - 1 if closing == -1 else closing
            word_characters.append(command[index:closing + 1])
            word_started = True
            index = closing + 1
            continue
        if character == "$" and following == "'":
            word_started = True
            word_contains_quoting = True
            index = read_ansi_c_quoted(index + 2)
            continue
        if character == "#" and not word_started:
            newline = command.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if character == ";":
            finish_segment(index, ";")
            index += 1
            segment_start = index
            continue
        if character == "|":
            separator = "||" if following == "|" else ("|&" if following == "&" else "|")
            finish_segment(index, "||" if separator == "||" else "|")
            index += len(separator)
            segment_start = index
            continue
        if character == "&":
            if following == "&":
                finish_segment(index, "&&")
                index += 2
                segment_start = index
                continue
            if following == ">":
                finish_word()
                operator = "&>>" if command.startswith("&>>", index) else "&>"
                pending_redirection_operator = operator
                index += len(operator)
                continue
            finish_segment(index, "&")
            index += 1
            segment_start = index
            continue
        if character == "(":
            parenthesis_depth += 1
            finish_segment(index, "(")
            index += 1
            segment_start = index
            continue
        if character == ")":
            if parenthesis_depth == 0 and stop_at_unmatched_close_parenthesis:
                finish_segment(index, ")")
                for segment in segments_since_last_newline:
                    segment.heredoc_bodies.extend("" for _ in segment.heredoc_delimiters_awaiting_body)
                    segment.heredoc_delimiters_awaiting_body = []
                return index + 1
            parenthesis_depth = max(0, parenthesis_depth - 1)
            finish_segment(index, ")")
            index += 1
            segment_start = index
            continue
        if character in "<>":
            if following == "(":
                # 进程替换 <( … ) / >( … )
                end = scan_nested_command_substitution(index + 2)
                word_characters.append(command[index:end])
                word_started = True
                index = end
                continue
            file_descriptor_prefix = ""
            if word_started and not word_contains_quoting and word_characters and "".join(word_characters).isdigit():
                file_descriptor_prefix = "".join(word_characters)
                word_characters = []
                word_started = False
            else:
                finish_word()
            operator = next(candidate for candidate in ("<<<", "<<-", "<<", "<>", "<&", ">>", ">|", ">&", "<", ">")
                            if command.startswith(candidate, index))
            index += len(operator)
            if operator in ("<<", "<<-"):
                pending_heredoc_strips_leading_tabs = operator == "<<-"
            else:
                pending_redirection_operator = file_descriptor_prefix + operator
            continue
        word_characters.append(character)
        word_started = True
        index += 1
    finish_segment(length, "end")
    for segment in segments_since_last_newline:
        segment.heredoc_bodies.extend("" for _ in segment.heredoc_delimiters_awaiting_body)
        segment.heredoc_delimiters_awaiting_body = []
    return length


# --------------------------------------------------------------------------------------------
# 命令段 → 写入目标
# --------------------------------------------------------------------------------------------


@dataclass
class ShellWriteTargetToken:
    """某个命令段里一个确定的写入目标（尚未对照脏路径集合解析）。"""

    write_kind: str
    target_token: str
    working_directory: str | None
    trigger_segment_excerpt: str
    allow_unique_basename_fallback: bool = True
    match_dirty_paths_under_directory_prefix: bool = False


@dataclass
class InlineScriptBodyWithFileWriteCall:
    """python / node 内联脚本正文里出现了写文件调用。"""

    unique_single_line_string_literal_contents: tuple[str, ...]
    # 每个写调用的目标都能还原成字符串字面量时，这里是这些目标（精确模式）；否则为 None（退回字面量提及启发式）
    write_destination_literal_contents: tuple[str, ...] | None
    working_directory: str | None
    script_body_text: str
    trigger_segment_excerpt: str


@dataclass
class ShellCommandNamedWriteTargetAnalysis:
    write_target_tokens: list[ShellWriteTargetToken] = field(default_factory=list)
    inline_script_bodies_with_file_write_call: list[InlineScriptBodyWithFileWriteCall] = field(default_factory=list)
    # 会改文件但无法落到具体路径的命令段（formatter 对目录、git stash pop / merge / rebase、xargs rm、
    # 目标是未解析变量的重定向…）：时间窗层据此把同一调用期间改动的文件降为「有争议」。
    bulk_or_unresolvable_target_write_segment_count: int = 0
    # 本命令里 ``git commit`` / ``rvf land commit`` 段的有效工作目录（已跟随 cd 与 git -C；None = 未知）。
    git_commit_segment_working_directories: list[str | None] = field(default_factory=list)
    # 既不是已知只读命令、也没解析出写入目标的命令段（``python3 run.py``、``npm test``、``make`` 之类不透明命令）。
    opaque_or_possibly_writing_segment_count: int = 0
    shell_parse_failed: bool = False

    @property
    def rewrites_work_tree_in_bulk_or_unresolvably(self) -> bool:
        return self.bulk_or_unresolvable_target_write_segment_count > 0

    @property
    def has_no_possible_write_effect(self) -> bool:
        """整条命令只由已知只读命令组成：不开「可能写入」的时间窗。解析失败按「可能写」处理。"""
        return (
            not self.shell_parse_failed
            and not self.write_target_tokens
            and not self.inline_script_bodies_with_file_write_call
            and self.bulk_or_unresolvable_target_write_segment_count == 0
            and self.opaque_or_possibly_writing_segment_count == 0
            and not self.git_commit_segment_working_directories
        )


def _expand_shell_variables_in_word(word: str, shell_variables: dict[str, str]) -> str:
    expanded = SHELL_VARIABLE_REFERENCE_PATTERN.sub(
        lambda match: shell_variables.get(match.group("braced") or match.group("plain"), match.group(0)), word)
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(Path.home()) + expanded[1:]
    return expanded


def _shell_short_option_letters(argument: str) -> str:
    match = re.match(r"^-([A-Za-z0-9]+)", argument)
    return match.group(1) if match and not argument.startswith("--") else ""


def _shell_positional_arguments(arguments: list[str], options_taking_value: Iterable[str] = ()) -> list[str]:
    options_taking_value = set(options_taking_value)
    positional: list[str] = []
    index = 0
    after_double_dash = False
    while index < len(arguments):
        argument = arguments[index]
        if after_double_dash:
            positional.append(argument)
        elif argument == "--":
            after_double_dash = True
        elif argument.startswith("-") and argument != "-":
            if argument in options_taking_value:
                index += 1
        else:
            positional.append(argument)
        index += 1
    return positional


def _shell_change_directory_destination(arguments: list[str], current_working_directory: str | None) -> str | None:
    positional = [argument for argument in arguments if not (argument.startswith("-") and argument != "-")]
    if not positional:
        return str(Path.home())
    destination = positional[0]
    if destination == "-" or "$" in destination or "`" in destination:
        return None
    if os.path.isabs(destination):
        return os.path.normpath(destination)
    if current_working_directory is None:
        return None
    return os.path.normpath(os.path.join(current_working_directory, destination))


def _formatter_or_linter_runs_in_write_mode(formatter_name: str, formatter_arguments: list[str]) -> bool:
    flags = set(formatter_arguments)
    if formatter_name in ("black", "isort", "rustfmt"):
        return not ({"--check", "--diff", "--check-only"} & flags)
    if formatter_name == "ruff":
        return ("format" in flags and not ({"--check", "--diff"} & flags)) or "--fix" in flags
    if formatter_name == "prettier":
        return bool({"--write", "-w"} & flags)
    if formatter_name in ("eslint", "oxlint"):
        return "--fix" in flags
    if formatter_name == "biome":
        return bool({"--write", "--apply", "--apply-unsafe", "--fix", "--unsafe"} & flags)
    if formatter_name in ("gofmt", "goimports", "shfmt"):
        return "-w" in flags
    if formatter_name in ("clang-format", "autopep8", "yapf"):
        return bool({"-i", "--in-place"} & flags)
    if formatter_name == "dprint":
        return "fmt" in flags
    return False


def patch_text_file_header_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for match in PATCH_TEXT_FILE_HEADER_PATTERN.finditer(patch_text):
        path = next((value for value in match.groups() if value), "").strip()
        if path and path != "/dev/null":
            paths.append(path)
    return paths


def analyze_shell_command_named_write_targets(
    command: str, initial_working_directory: str | None
) -> ShellCommandNamedWriteTargetAnalysis:
    """把一条 shell 命令按命令段解析出写入目标。与脏路径集合无关，可缓存复用。"""
    analysis = ShellCommandNamedWriteTargetAnalysis()
    try:
        segments = split_shell_command_into_segments(command)
    except Exception:  # pragma: no cover - 防御：解析器异常不应中断归属
        analysis.shell_parse_failed = True
        return analysis
    working_directory = os.path.normpath(initial_working_directory) if initial_working_directory else None
    shell_variables: dict[str, str] = {"HOME": str(Path.home())}
    for segment in segments:
        excerpt = segment.shell_text[:SHELL_WRITE_TRIGGER_COMMAND_EXCERPT_MAXIMUM_CHARACTERS]
        segment_working_directory = working_directory

        def add_write_target(write_kind: str, token: str, *, allow_unique_basename_fallback: bool = True,
                             match_dirty_paths_under_directory_prefix: bool = False,
                             target_working_directory: str | None = None) -> None:
            analysis.write_target_tokens.append(ShellWriteTargetToken(
                write_kind, token,
                target_working_directory if target_working_directory is not None else segment_working_directory,
                excerpt, allow_unique_basename_fallback, match_dirty_paths_under_directory_prefix))

        words = list(segment.words)
        position = 0
        while position < len(words) and words[position] in SHELL_CONTROL_KEYWORDS_BEFORE_COMMAND_WORD:
            position += 1
        while position < len(words):
            assignment = SHELL_WORD_ASSIGNMENT_PATTERN.match(words[position])
            if not assignment:
                break
            shell_variables[assignment.group("name")] = _expand_shell_variables_in_word(assignment.group("value"), shell_variables)
            position += 1
        while position < len(words):
            wrapper_name = os.path.basename(words[position])
            if wrapper_name in SHELL_COMMAND_WRAPPERS_BEFORE_COMMAND_WORD:
                position += 1
                while position < len(words) and (words[position].startswith("-") or SHELL_WORD_ASSIGNMENT_PATTERN.match(words[position])):
                    position += 1
                continue
            if wrapper_name in ("timeout", "gtimeout"):
                position += 1
                while position < len(words) and words[position].startswith("-"):
                    position += 1
                position += 1  # 时长参数
                continue
            break
        command_words = [_expand_shell_variables_in_word(word, shell_variables) for word in words[position:]]

        # 1) 重定向：只认目标路径（2>&1、>&2 之类 fd 复制与 /dev/null 不算）
        for operator, target in segment.redirections:
            if ">" not in operator:
                continue
            if operator.endswith(">&") and (target.isdigit() or target == "-"):
                continue
            expanded_target = _expand_shell_variables_in_word(target, shell_variables)
            if expanded_target in SHELL_REDIRECTION_DEVICE_TARGETS_THAT_ARE_NOT_FILES:
                continue
            if "$" in expanded_target or "`" in expanded_target:
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
                continue
            add_write_target("shell_redirection_overwrite_or_append_target", expanded_target)

        if not command_words:
            continue
        command_name = os.path.basename(command_words[0])
        arguments = command_words[1:]
        pipe_source_heredoc_bodies = segment.pipe_source_segment.heredoc_bodies if segment.pipe_source_segment else []
        if not _shell_segment_is_known_read_only(command_name, arguments):
            analysis.opaque_or_possibly_writing_segment_count += 1

        # 2) 按命令种类提取文件参数
        if command_name in ("cd", "pushd"):
            working_directory = _shell_change_directory_destination(arguments, working_directory)
            continue
        if command_name in ("export", "declare", "local", "readonly", "typeset"):
            for argument in arguments:
                assignment = SHELL_WORD_ASSIGNMENT_PATTERN.match(argument)
                if assignment:
                    shell_variables[assignment.group("name")] = assignment.group("value")
            continue
        if command_name == "tee":
            for target in _shell_positional_arguments(arguments):
                add_write_target("tee_command_file_argument", target)
        elif command_name in ("sed", "gsed") and any(
                argument == "--in-place" or argument.startswith("--in-place=") or "i" in _shell_short_option_letters(argument)
                for argument in arguments):
            positional = _shell_positional_arguments(arguments, {"-e", "--expression", "-f", "--file", "-l"})
            has_explicit_script_option = any(argument in ("-e", "--expression", "-f", "--file")
                                             or argument.startswith("--expression=") for argument in arguments)
            for target in (positional if has_explicit_script_option else positional[1:]):
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name == "perl" and any("i" in _shell_short_option_letters(argument) for argument in arguments):
            positional = _shell_positional_arguments(arguments, {"-e", "-E"})
            cluster_ending_with_code_option = any(
                _shell_short_option_letters(argument)[-1:] in ("e", "E") and argument not in ("-e", "-E") for argument in arguments)
            has_separate_code_option = any(argument in ("-e", "-E") for argument in arguments)
            files = positional[1:] if cluster_ending_with_code_option or not has_separate_code_option else positional
            for target in files:
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name in ("mv", "cp", "install", "rsync", "ditto", "ln"):
            _add_move_or_copy_write_targets(command_name, arguments, add_write_target)
        elif command_name in ("rm", "unlink", "touch", "truncate", "shred"):
            recursive = any(argument in ("--recursive",) or set("rR") & set(_shell_short_option_letters(argument))
                            for argument in arguments) and command_name == "rm"
            for target in _shell_positional_arguments(arguments, {"-s", "--size", "-r"} if command_name == "truncate" else ()):
                add_write_target("remove_or_touch_or_truncate_file_argument", target,
                                 match_dirty_paths_under_directory_prefix=recursive)
        elif command_name == "git":
            _analyze_git_segment_write_targets(arguments, segment, pipe_source_heredoc_bodies, working_directory,
                                               add_write_target, analysis)
        elif command_name == "rvf" and arguments[:2] == ["land", "commit"]:
            # rvf land commit 在 --repo（缺省为当前目录）里做 git commit。
            repository_argument = next(
                (arguments[option_index + 1] for option_index, argument in enumerate(arguments[:-1]) if argument == "--repo"),
                None,
            )
            analysis.git_commit_segment_working_directories.append(
                _shell_change_directory_destination([repository_argument], working_directory)
                if repository_argument else working_directory
            )
        elif command_name in ("patch", "apply_patch", "applypatch"):
            header_paths = patch_text_file_header_paths("\n".join([*segment.heredoc_bodies, *pipe_source_heredoc_bodies, *arguments]))
            for target in header_paths:
                add_write_target("patch_text_file_header_in_heredoc_or_argument", target)
            if not header_paths:
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
        elif command_name in ("curl", "wget"):
            for option_index, argument in enumerate(arguments[:-1]):
                letters = _shell_short_option_letters(argument)
                if (command_name == "curl" and (argument == "--output" or letters.endswith("o"))) or \
                        (command_name == "wget" and (argument == "--output-document" or letters.endswith("O"))):
                    add_write_target("download_output_file_argument", arguments[option_index + 1])
        elif command_name in ("sort", "uniq", "xxd"):
            for target in _sort_or_uniq_or_xxd_output_file_write_effect(command_name, arguments)[1]:
                add_write_target("sort_or_uniq_or_xxd_output_file_argument", target)
        elif command_name == "yq" and any(argument in ("-i", "--inplace") for argument in arguments):
            for target in _shell_positional_arguments(arguments)[1:]:
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name == "xargs":
            following = _shell_positional_arguments(arguments, {"-I", "-n", "-P", "-L", "-d", "-E", "-s"})
            if following and os.path.basename(following[0]) in XARGS_OR_FIND_EXEC_WRITER_COMMAND_NAMES:
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
        elif command_name == "find":
            exec_commands = [arguments[option_index + 1] for option_index, argument in enumerate(arguments[:-1])
                             if argument in ("-exec", "-execdir")]
            if "-delete" in arguments or any(os.path.basename(name) in XARGS_OR_FIND_EXEC_WRITER_COMMAND_NAMES for name in exec_commands):
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
        elif command_name in ("npm", "pnpm", "yarn", "bun", "make", "just"):
            script_names = arguments[1:2] if arguments[:1] in (["run"], ["run-script"]) else arguments[:1]
            if any(PACKAGE_MANAGER_SCRIPT_NAMES_THAT_REWRITE_FILES_PATTERN.match(name) for name in script_names):
                analysis.bulk_or_unresolvable_target_write_segment_count += 1

        # 3) formatter / linter 写模式（可能经 npx / bunx / uv run / python -m 调起）
        runner_is_first_word = command_name in FORMATTER_OR_INTERPRETER_RUNNER_COMMAND_NAMES or "node_modules/.bin" in command_words[0]
        formatter_index = next((word_index for word_index, word in enumerate(command_words)
                                if os.path.basename(word) in FORMATTER_OR_LINTER_COMMAND_NAMES
                                and (word_index == 0 or runner_is_first_word)), None)
        if formatter_index is not None:
            formatter_name = os.path.basename(command_words[formatter_index])
            formatter_arguments = command_words[formatter_index + 1:]
            if _formatter_or_linter_runs_in_write_mode(formatter_name, formatter_arguments):
                positional = [argument for argument in _shell_positional_arguments(formatter_arguments, {
                    "--config", "-c", "--config-path", "--stdin-filepath", "--line-length", "-l", "--target-version",
                    "--ignore-path", "--max-diagnostics", "--log-level", "--extend-select", "--select", "--ignore"})
                    if argument not in FORMATTER_OR_LINTER_SUBCOMMAND_WORDS]
                directory_like = [argument for argument in positional if argument in (".", "./") or argument.endswith("/") or "*" in argument]
                if not positional or directory_like:
                    analysis.bulk_or_unresolvable_target_write_segment_count += 1
                for target in positional:
                    if target not in directory_like:
                        add_write_target("formatter_or_linter_write_mode_file_argument", target)

        # 4) python / node 内联脚本（-c / -e 正文，或从 heredoc / 管道 heredoc 读 stdin 的正文）
        interpreter_index = next((word_index for word_index, word in enumerate(command_words)
                                  if INLINE_SCRIPT_INTERPRETER_NAME_PATTERN.match(os.path.basename(word))
                                  and (word_index == 0 or runner_is_first_word)), None)
        if interpreter_index is not None:
            interpreter_name = os.path.basename(command_words[interpreter_index])
            interpreter_arguments = command_words[interpreter_index + 1:]
            is_python_interpreter = interpreter_name.startswith("python")
            code_bodies: list[str] = []
            reads_code_from_stdin = True
            option_index = 0
            while option_index < len(interpreter_arguments):
                argument = interpreter_arguments[option_index]
                if (is_python_interpreter and argument == "-c") or \
                        (not is_python_interpreter and argument in ("-e", "--eval", "-p", "--print")):
                    if option_index + 1 < len(interpreter_arguments):
                        code_bodies.append(interpreter_arguments[option_index + 1])
                    reads_code_from_stdin = False
                    break
                if argument == "-":
                    break
                if argument == "-m" or not argument.startswith("-"):
                    reads_code_from_stdin = False
                    break
                option_index += 2 if argument in ("-W", "-X", "-r", "--require", "--import", "--loader") else 1
            if reads_code_from_stdin:
                code_bodies.extend(segment.heredoc_bodies)
                code_bodies.extend(pipe_source_heredoc_bodies)
                code_bodies.extend(target for operator, target in segment.redirections if operator.endswith("<<<"))
            script_body_text = "\n".join(body for body in code_bodies if body)
            if script_body_text and INLINE_SCRIPT_FILE_WRITE_CALL_PATTERN.search(script_body_text):
                analysis.inline_script_bodies_with_file_write_call.append(InlineScriptBodyWithFileWriteCall(
                    _inline_script_path_like_string_literal_contents(script_body_text),
                    _inline_script_write_destination_literal_contents_or_none(script_body_text), segment_working_directory,
                    script_body_text, excerpt))
    return analysis


def _sed_script_texts_or_none(arguments: list[str]) -> list[str] | None:
    """sed 命令里的脚本正文（``-e`` / ``--expression`` 或首个位置参数）。脚本来自文件（``-f``）或认不出时返回 None。"""
    scripts: list[str] = []
    has_explicit_script_option = False
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            positional.extend(arguments[index + 1:])
            break
        if argument in ("-e", "--expression"):
            if index + 1 >= len(arguments):
                return None
            has_explicit_script_option = True
            scripts.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--expression="):
            has_explicit_script_option = True
            scripts.append(argument.split("=", 1)[1])
            index += 1
            continue
        if argument == "--file" or argument.startswith("--file="):
            return None
        if argument in ("-l", "--line-length"):
            index += 2
            continue
        letters = _shell_short_option_letters(argument)
        if letters:
            if "f" in letters:
                return None
            if "e" in letters:
                # ``-ne 'script'``：e 必须是整个选项簇的最后一个字母，否则脚本粘在选项上，认不出就按不透明处理
                if argument != "-" + letters or not letters.endswith("e") or index + 1 >= len(arguments):
                    return None
                has_explicit_script_option = True
                scripts.append(arguments[index + 1])
                index += 2
                continue
            index += 1
            continue
        if argument.startswith("-") and argument != "-":
            index += 1
            continue
        positional.append(argument)
        index += 1
    if not has_explicit_script_option:
        if not positional:
            return None
        scripts.append(positional[0])
    return scripts


def _sed_script_may_write_file(script: str) -> bool:
    """sed 脚本里有 ``w`` / ``W`` 写命令、``s`` 命令的 ``w`` 标志或 ``e`` 执行（GNU）时视为可能写文件。
    逐个命令扫描地址与参数；遇到认不出的命令或未闭合的定界符一律按「可能写入」处理。"""
    length = len(script)

    def end_of_delimited_part(start: int, delimiter: str) -> int:
        """从 start 起找未转义的 delimiter，返回其后一位；没找到返回 -1。"""
        index = start
        while index < length:
            if script[index] == "\\":
                index += 2
                continue
            if script[index] == delimiter:
                return index + 1
            index += 1
        return -1

    def end_of_line(start: int, *, stop_at_semicolon: bool) -> int:
        index = start
        while index < length and script[index] != "\n" and not (stop_at_semicolon and script[index] == ";"):
            index += 2 if script[index] == "\\" else 1
        return min(index, length)

    position = 0
    while position < length:
        character = script[position]
        if character in " \t\n;{}!,+~$0123456789":  # 分隔、块、取反与行号 / 区间地址
            position += 1
        elif character == "#":
            position = end_of_line(position, stop_at_semicolon=False)
        elif character in "/\\":  # 正则地址 /re/ 或 \cREc，可带 I / M 修饰
            if character == "\\":
                if position + 1 >= length:
                    return True
                position = end_of_delimited_part(position + 2, script[position + 1])
            else:
                position = end_of_delimited_part(position + 1, "/")
            if position == -1:
                return True
            while position < length and script[position] in "IM":
                position += 1
        elif character in "sy":
            if position + 1 >= length or script[position + 1] in "\n\\":
                return True
            delimiter = script[position + 1]
            position = end_of_delimited_part(position + 2, delimiter)
            if position != -1:
                position = end_of_delimited_part(position, delimiter)
            if position == -1:
                return True
            if character == "s":
                while position < length and script[position] in "gpiImMe0123456789w":
                    if script[position] in "we":
                        return True
                    position += 1
        elif character in "aicrRbtT:":  # 追加 / 插入 / 替换文本、读文件、跳转标签：参数到行尾（标签到分号）
            position = end_of_line(position + 1, stop_at_semicolon=character in "btT:")
        elif character in SED_SCRIPT_COMMANDS_WITHOUT_FILE_WRITE_EFFECT:
            position += 1
        else:  # w / W / e，以及认不出的命令
            return True
    return False


def _getopt_style_options_and_positional_arguments(
    arguments: list[str], short_option_letters_taking_value: str, long_option_names_taking_value: tuple[str, ...],
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """GNU getopt 风格解析：返回 ([(选项名, 值或 None)], 位置参数)。短选项簇里的取值字母吃掉簇的剩余部分
    或下一个词（``-uo out.txt`` / ``-oout.txt``）；长选项支持 ``--name=value``、``--name value`` 与无歧义前缀缩写。"""
    options: list[tuple[str, str | None]] = []
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            positional.extend(arguments[index:])
            break
        if argument.startswith("--"):
            name, has_inline_value, inline_value = argument[2:].partition("=")
            prefix_matches = [long_name for long_name in long_option_names_taking_value if name and long_name.startswith(name)]
            canonical_name = name if name in long_option_names_taking_value else (
                prefix_matches[0] if len(prefix_matches) == 1 else None)
            if canonical_name is None or has_inline_value:
                options.append((canonical_name or name, inline_value if has_inline_value else None))
            else:
                options.append((canonical_name, arguments[index] if index < len(arguments) else None))
                index += 1
            continue
        if argument.startswith("-") and argument != "-":
            for letter_index, letter in enumerate(argument[1:], start=1):
                if letter in short_option_letters_taking_value:
                    value = argument[letter_index + 1:]
                    if not value:
                        value = arguments[index] if index < len(arguments) else None
                        index += 1
                    options.append((letter, value))
                    break
                options.append((letter, None))
            continue
        positional.append(argument)
    return options, positional


def _sort_or_uniq_or_xxd_output_file_write_effect(command_name: str, arguments: list[str]) -> tuple[bool, list[str]]:
    """sort / uniq / xxd 是否把输出写进文件：返回 (是否可能写文件, 能确定的输出文件参数)。

    - ``sort -o <file>`` / ``--output[=]<file>``；
    - ``uniq [INPUT [OUTPUT]]``、``xxd [infile [outfile]]``：第二个位置参数是输出文件（``-`` 是 stdout）；
    - ``xxd -r``（含 ``-rp`` 之类合写）一律按可能写入：xxd 自己按前缀解析选项，参数拆分拿不准时宁可算写。"""
    output_file_arguments: list[str] = []
    writes_file = False
    if command_name == "sort":
        options, _ = _getopt_style_options_and_positional_arguments(arguments, "kotST", (
            "output", "key", "field-separator", "buffer-size", "temporary-directory", "parallel", "batch-size",
            "files0-from", "compress-program", "random-source"))
        for name, value in options:
            if name in ("o", "output") and value not in SHELL_REDIRECTION_DEVICE_TARGETS_THAT_ARE_NOT_FILES:
                writes_file = True
                if value:
                    output_file_arguments.append(value)
    else:
        if command_name == "uniq":
            _, positional = _getopt_style_options_and_positional_arguments(
                arguments, "fsw", ("skip-fields", "skip-chars", "check-chars"))
        else:  # xxd：不是 getopt，取值选项按整词识别（``-c 8`` / ``-cols 8``；``-c8`` 值粘在选项上）
            positional = _shell_positional_arguments(arguments, {
                "-c", "-g", "-l", "-o", "-s", "-n", "-R", "-cols", "-groupsize", "-len", "-offset", "-seek", "-name"})
            arguments_before_double_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
            writes_file = any(argument.startswith("-") and argument != "-" and "r" in _shell_short_option_letters(argument)
                              for argument in arguments_before_double_dash)
        if len(positional) >= 2 and positional[1] != "-" and positional[1] not in SHELL_REDIRECTION_DEVICE_TARGETS_THAT_ARE_NOT_FILES:
            writes_file = True
            output_file_arguments.append(positional[1])
    return writes_file, output_file_arguments


def _shell_segment_is_known_read_only(command_name: str, arguments: list[str]) -> bool:
    if command_name in READ_ONLY_SHELL_COMMAND_NAMES:
        return True
    if command_name in ("sort", "uniq", "xxd"):
        return not _sort_or_uniq_or_xxd_output_file_write_effect(command_name, arguments)[0]
    if command_name in ("sed", "gsed"):
        if any(argument == "--in-place" or argument.startswith("--in-place=") or "i" in _shell_short_option_letters(argument)
               for argument in arguments):
            return False
        scripts = _sed_script_texts_or_none(arguments)
        return scripts is not None and not any(_sed_script_may_write_file(script) for script in scripts)
    if command_name == "find":
        return "-delete" not in arguments and not any(argument in ("-exec", "-execdir", "-ok", "-fprint") for argument in arguments)
    if command_name == "git":
        index = 0
        while index < len(arguments) and arguments[index].startswith("-"):
            index += 2 if arguments[index] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
        subcommand = arguments[index] if index < len(arguments) else ""
        subcommand_positional = [argument for argument in arguments[index + 1:] if not argument.startswith("-")]
        if any(argument == "--output" or argument.startswith("--output=") for argument in arguments[index + 1:]):
            return False  # git diff / log / show 的 --output=<file> 会把输出写进文件
        if subcommand in READ_ONLY_GIT_SUBCOMMANDS:
            return True
        if subcommand == "stash":
            return subcommand_positional[:1] in (["list"], ["show"])
        if subcommand in ("worktree", "branch", "tag"):
            return subcommand_positional[:1] in ([], ["list"])
        return False
    if command_name == "rvf":
        positional = [argument for argument in arguments if not argument.startswith("-")]
        return bool(positional) and positional[0] in READ_ONLY_RVF_SUBCOMMANDS
    return False


def _add_move_or_copy_write_targets(command_name: str, arguments: list[str], add_write_target: Callable[..., None]) -> None:
    """mv：源（被删）与目标都算写入；cp / install / rsync / ditto / ln：只认目标。目标是目录时补上「目录/源文件名」。"""
    target_directory: str | None = None
    for option_index, argument in enumerate(arguments):
        if argument in ("-t", "--target-directory") and option_index + 1 < len(arguments):
            target_directory = arguments[option_index + 1]
        elif argument.startswith("--target-directory="):
            target_directory = argument.split("=", 1)[1]
    positional = _shell_positional_arguments(arguments, {
        "-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group", "-e", "--exclude"})
    if target_directory is None:
        if len(positional) < 2:
            return
        sources, destination = positional[:-1], positional[-1]
    else:
        sources, destination = positional, target_directory
    recursive_copy = command_name in ("cp", "rsync", "ditto") and any(
        argument in ("--recursive", "--archive") or set("rRa") & set(_shell_short_option_letters(argument)) for argument in arguments)
    destination_kind = "move_command_source_or_destination" if command_name == "mv" else "copy_or_install_command_destination"
    add_write_target(destination_kind, destination)
    for source in sources:
        add_write_target(destination_kind, destination.rstrip("/") + "/" + os.path.basename(source.rstrip("/")),
                         allow_unique_basename_fallback=False)
        if command_name == "mv":
            add_write_target(destination_kind, source)
    if recursive_copy:
        add_write_target("copy_recursive_directory_destination_prefix", destination,
                         allow_unique_basename_fallback=False, match_dirty_paths_under_directory_prefix=True)


def _analyze_git_segment_write_targets(
    arguments: list[str], segment: ShellCommandSegment, pipe_source_heredoc_bodies: list[str],
    working_directory: str | None, add_write_target: Callable[..., None], analysis: ShellCommandNamedWriteTargetAnalysis,
) -> None:
    git_working_directory = working_directory
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-C" and index + 1 < len(arguments):
            git_working_directory = _shell_change_directory_destination([arguments[index + 1]], git_working_directory)
            index += 2
            continue
        if argument in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        break
    subcommand = arguments[index] if index < len(arguments) else ""
    subcommand_arguments = arguments[index + 1:]
    kind = "git_rm_mv_restore_checkout_file_argument"
    if subcommand == "commit":
        options_before_pathspec = subcommand_arguments[:subcommand_arguments.index("--")] \
            if "--" in subcommand_arguments else subcommand_arguments
        # --dry-run / -h / --help 成功退出却不产生提交：不能拿来当 HEAD 截断点
        if not GIT_COMMIT_OPTIONS_THAT_DO_NOT_CREATE_A_COMMIT & set(options_before_pathspec):
            analysis.git_commit_segment_working_directories.append(git_working_directory)
    elif subcommand == "mv":
        _add_move_or_copy_write_targets("mv", subcommand_arguments,
                                        lambda write_kind, token, **options: add_write_target(
                                            kind, token, target_working_directory=git_working_directory, **options))
    elif subcommand == "rm":
        recursive = any("r" in _shell_short_option_letters(argument) for argument in subcommand_arguments)
        for target in _shell_positional_arguments(subcommand_arguments):
            add_write_target(kind, target, match_dirty_paths_under_directory_prefix=recursive,
                             target_working_directory=git_working_directory)
    elif subcommand in ("restore", "checkout"):
        if subcommand == "checkout" and "--" in subcommand_arguments:
            paths = subcommand_arguments[subcommand_arguments.index("--") + 1:]
        else:
            paths = _shell_positional_arguments(subcommand_arguments, {"-s", "--source", "-b", "-B", "--orphan"})
        if subcommand == "checkout" and "--" not in subcommand_arguments:
            # 没有 ``--`` 时分不清 ``git checkout <文件>`` 与 ``git checkout <分支>``：参数照常当文件目标
            # （分支名不会命中脏路径），同时记一次批量触碰（切分支会整体改写工作树）。
            analysis.bulk_or_unresolvable_target_write_segment_count += 1
        for target in paths:
            if target in (".", "./", ":/"):
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
                continue
            add_write_target(GIT_RESTORE_OR_CHECKOUT_FILE_ARGUMENT_WRITE_KIND, target, target_working_directory=git_working_directory)
    elif subcommand == "apply":
        header_paths = patch_text_file_header_paths("\n".join([*segment.heredoc_bodies, *pipe_source_heredoc_bodies]))
        for target in header_paths:
            add_write_target("patch_text_file_header_in_heredoc_or_argument", target,
                             target_working_directory=git_working_directory)
        if not header_paths:
            analysis.bulk_or_unresolvable_target_write_segment_count += 1
    elif (subcommand == "stash" and subcommand_arguments[:1] in (["pop"], ["apply"])) or \
            (subcommand == "reset" and "--hard" in subcommand_arguments) or subcommand in GIT_SUBCOMMANDS_THAT_REWRITE_WORK_TREE_IN_BULK:
        analysis.bulk_or_unresolvable_target_write_segment_count += 1


# --------------------------------------------------------------------------------------------
# 内联脚本
# --------------------------------------------------------------------------------------------


def _inline_script_path_like_string_literal_contents(script_body_text: str) -> tuple[str, ...]:
    """内联脚本里像路径的单行字符串字面量内容（不含空白、含 / 或 .）。三引号字符串整体剔除。"""
    without_triple_quoted = INLINE_SCRIPT_TRIPLE_QUOTED_STRING_PATTERN.sub("", script_body_text)
    contents: set[str] = set()
    for match in INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN.finditer(without_triple_quoted):
        content = next((group for group in match.groups() if group is not None), "")
        if len(content) >= 3 and not re.search(r"\s", content) and ("/" in content or "." in content):
            contents.add(content)
    return tuple(sorted(contents))


def _inline_script_path_expression_literal_value_or_none(expression: str) -> str | None:
    """``"docs/a.md"`` / ``Path("docs/a.md")`` / ``ROOT / "docs" / "a.md"`` / ``path.join(root, "out/x.html")``
    → 拼起来的字面量路径；表达式里还有别的东西（函数调用、下标、拼接变量）时返回 None。"""
    expression = expression.strip().rstrip(";").strip()
    literals = list(INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN.finditer(expression))
    if not literals:
        return None
    glue = INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN.sub("", expression)
    glue = re.sub(r"\s*(?:#|//).*$", "", glue)  # 行尾注释（字面量已剔除，剩下的 # / // 只可能是注释）
    glue = re.sub(r"(?<![\w])[fFrRbB]{1,2}(?=\s|$|,|\)|/)", "", glue)  # 去掉字面量前缀残留（f"…" 里的 f）
    if not INLINE_SCRIPT_PATH_EXPRESSION_GLUE_PATTERN.match(glue):
        return None
    parts = [next((group for group in match.groups() if group is not None), "") for match in literals]
    return "/".join(part.strip("/") if index else part.rstrip("/") for index, part in enumerate(parts) if part)


def _inline_script_write_destination_literal_contents_or_none(script_body_text: str) -> tuple[str, ...] | None:
    """全部「带目标参数的写调用」的目标都能还原成字面量路径时返回这些目标；有任一目标还原不了、
    或一个带目标参数的写调用都认不出时返回 None。"""
    without_triple_quoted = INLINE_SCRIPT_TRIPLE_QUOTED_STRING_PATTERN.sub("\"\"", script_body_text)
    variable_literal_values: dict[str, set[str]] = collections.defaultdict(set)
    variables_with_non_literal_assignment: set[str] = set()
    for match in INLINE_SCRIPT_VARIABLE_ASSIGNMENT_PATTERN.finditer(without_triple_quoted):
        value = _inline_script_path_expression_literal_value_or_none(match.group("expression"))
        if value is None:
            variables_with_non_literal_assignment.add(match.group("name"))
        else:
            variable_literal_values[match.group("name")].add(value)
    for match in INLINE_SCRIPT_FOR_LOOP_OVER_LITERAL_LIST_PATTERN.finditer(without_triple_quoted):
        items = [next((group for group in literal.groups() if group is not None), "")
                 for literal in INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN.finditer(match.group("items"))]
        if items:
            variable_literal_values[match.group("name")].update(items)
        else:
            variables_with_non_literal_assignment.add(match.group("name"))
    destinations: set[str] = set()
    found_destination_bearing_call = False
    for pattern in INLINE_SCRIPT_DESTINATION_BEARING_WRITE_CALL_PATTERNS:
        for match in pattern.finditer(without_triple_quoted):
            found_destination_bearing_call = True
            destination_expression = match.group("destination").strip()
            literal_value = _inline_script_path_expression_literal_value_or_none(destination_expression)
            if literal_value is not None:
                destinations.add(literal_value)
                continue
            name = destination_expression.split(".", 1)[0] if re.fullmatch(r"[A-Za-z_][\w.]*", destination_expression) else ""
            if name and variable_literal_values.get(name) and name not in variables_with_non_literal_assignment:
                destinations.update(variable_literal_values[name])
                continue
            return None
    return tuple(sorted(destinations)) if found_destination_bearing_call else None


# --------------------------------------------------------------------------------------------
# 写入目标 → 当前脏路径
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShellCommandNamedWriteTargetDirtyPathMatch:
    """一条命令写明的写入目标落到了某个当前脏路径上。"""

    repository_relative_path: str
    write_kind: str
    # exact_relative_or_absolute_path / glob_pattern / under_written_directory_prefix / unique_basename
    match_kind: str
    trigger_command_excerpt: str


def _repository_relative_path_of_absolute_path(repository_root: str, absolute_path: str) -> str | None:
    if absolute_path == repository_root:
        return ""
    if absolute_path.startswith(repository_root + "/"):
        return absolute_path[len(repository_root) + 1:]
    return None


def _repository_root_spellings(repository_root: Path) -> tuple[str, ...]:
    """仓库根的两种写法：给定路径与 resolve 后的真实路径（macOS 的 /private/tmp ↔ /tmp）。"""
    spellings = [os.path.normpath(str(repository_root))]
    try:
        resolved = os.path.normpath(str(repository_root.resolve()))
    except OSError:
        resolved = spellings[0]
    if resolved not in spellings:
        spellings.append(resolved)
    return tuple(spellings)


def _relative_path_under_any_root(roots: tuple[str, ...], absolute_path: str) -> str | None:
    for root in roots:
        relative = _repository_relative_path_of_absolute_path(root, absolute_path)
        if relative is not None:
            return relative
    return None


def _resolve_write_target_token_to_dirty_paths(
    target: ShellWriteTargetToken, roots: tuple[str, ...], dirty_paths: set[str], unique_basename_to_dirty_path: dict[str, str],
) -> list[tuple[str, str]]:
    """写入目标 → 脏路径列表 [(路径, 匹配方式)]。仓库外的目标、未解析的变量一律不匹配。"""
    token = target.target_token.strip()
    if not token or "$" in token or "`" in token or "\n" in token:
        return []
    while token.startswith("./"):
        token = token[2:]
    working_directory = target.working_directory
    relative_candidates: list[str] = []
    if os.path.isabs(token):
        relative = _relative_path_under_any_root(roots, os.path.normpath(token))
        if relative is None:
            return []
        relative_candidates.append(relative)
    elif working_directory:
        relative = _relative_path_under_any_root(roots, os.path.normpath(os.path.join(working_directory, token)))
        if relative is not None:
            relative_candidates.append(relative)
    elif not token.startswith(".."):
        # 工作目录未知时按「相对仓库根」解析。
        relative_candidates.append(os.path.normpath(token))
    if any(character in token for character in "*?["):
        matches = [(path, "glob_pattern") for relative in relative_candidates
                   for path in sorted(dirty_paths) if fnmatch.fnmatchcase(path, relative)]
        return list(dict.fromkeys(matches))
    for relative in relative_candidates:
        if relative in dirty_paths:
            return [(relative, "exact_relative_or_absolute_path")]
    if target.match_dirty_paths_under_directory_prefix:
        for relative in relative_candidates:
            if relative in ("", "."):
                continue
            prefix = relative.rstrip("/") + "/"
            hits = [(path, "under_written_directory_prefix") for path in sorted(dirty_paths) if path.startswith(prefix)]
            if hits:
                return hits
    # 唯一同名兜底只在工作目录未知、得不到确切相对路径时才用：确切路径已知却不在脏集合里（写进被忽略目录 / 内容没变）
    # 时改认另一目录的同名文件，会把背景改动冒充成本次写入。
    if target.allow_unique_basename_fallback and not os.path.isabs(token) and not working_directory:
        dirty_path = unique_basename_to_dirty_path.get(token.rsplit("/", 1)[-1])
        if dirty_path:
            return [(dirty_path, "unique_basename")]
    return []


def _dirty_paths_mentioned_in_inline_script_body(
    script: InlineScriptBodyWithFileWriteCall, roots: tuple[str, ...], dirty_paths: set[str],
    unique_basename_to_dirty_path: dict[str, str],
) -> dict[str, str]:
    """返回 {脏路径: 匹配方式}。f-string / 模板字符串取最后一个插值之后的部分。"""
    # 脚本里的相对路径相对于进程 cwd：cwd 已知时只按 cwd 解析（cwd 在仓库外则相对路径不算本仓库写入），未知时按仓库根
    prefixes: list[str] = [""]
    working_directory_is_outside_repository = False
    if script.working_directory:
        working_directory_relative = _relative_path_under_any_root(roots, os.path.normpath(script.working_directory))
        if working_directory_relative is None:
            working_directory_is_outside_repository = True
        elif working_directory_relative:
            prefixes = [working_directory_relative.rstrip("/") + "/"]
    mentioned: dict[str, str] = {}
    literal_contents = script.write_destination_literal_contents \
        if script.write_destination_literal_contents is not None else script.unique_single_line_string_literal_contents
    for literal_content in literal_contents:
        stripped = literal_content
        # 带插值的字面量只剩尾段、前缀未知；不带插值且 cwd 已知时确切路径已定，不再按同名文件兜底。
        exact_relative_path_is_known = bool(script.working_directory) and "}" not in stripped
        if "}" in stripped:
            stripped = stripped.rsplit("}", 1)[1].lstrip("/")
            if not stripped:
                continue
        while stripped.startswith("./"):
            stripped = stripped[2:]
        if stripped.startswith("/"):
            relative = _relative_path_under_any_root(roots, os.path.normpath(stripped))
            if relative is not None and relative in dirty_paths:
                mentioned[relative] = "exact_relative_or_absolute_path"
            continue
        if stripped.startswith("..") or working_directory_is_outside_repository:
            continue
        resolved = next((prefix + stripped for prefix in prefixes if prefix + stripped in dirty_paths), None)
        if resolved is not None:
            mentioned[resolved] = "exact_relative_or_absolute_path"
            continue
        if exact_relative_path_is_known:
            continue
        dirty_path = unique_basename_to_dirty_path.get(stripped.rsplit("/", 1)[-1])
        if dirty_path and dirty_path not in mentioned:
            mentioned[dirty_path] = "unique_basename"
    return mentioned


def unique_basename_to_dirty_path_index(dirty_paths: Iterable[str]) -> dict[str, str]:
    """脏路径里**唯一**且足够长的文件名 → 该路径；供写入目标解析不到确切路径时兜底。"""
    by_basename: dict[str, list[str]] = collections.defaultdict(list)
    for path in dirty_paths:
        by_basename[path.rsplit("/", 1)[-1]].append(path)
    return {
        basename: paths[0]
        for basename, paths in by_basename.items()
        if len(paths) == 1 and len(basename) >= MINIMUM_UNIQUE_BASENAME_LENGTH_FOR_BASENAME_FALLBACK_MATCH
    }


def resolve_shell_command_named_write_targets_to_dirty_paths(
    analysis: ShellCommandNamedWriteTargetAnalysis,
    *,
    repository_root: Path,
    dirty_paths: set[str],
    unique_basename_to_dirty_path: dict[str, str] | None = None,
) -> list[ShellCommandNamedWriteTargetDirtyPathMatch]:
    """把一条命令的写入目标对照当前脏路径集合，返回命中的脏路径（每个路径只取第一次命中）。"""
    roots = _repository_root_spellings(repository_root)
    basename_index = (
        unique_basename_to_dirty_path
        if unique_basename_to_dirty_path is not None
        else unique_basename_to_dirty_path_index(dirty_paths)
    )
    matches: dict[str, ShellCommandNamedWriteTargetDirtyPathMatch] = {}
    for target in analysis.write_target_tokens:
        if target.write_kind in WRITE_KINDS_THAT_ARE_NOT_AUTHORSHIP_EVIDENCE:
            continue
        for path, match_kind in _resolve_write_target_token_to_dirty_paths(target, roots, dirty_paths, basename_index):
            matches.setdefault(path, ShellCommandNamedWriteTargetDirtyPathMatch(
                path, target.write_kind, match_kind, target.trigger_segment_excerpt))
    for script in analysis.inline_script_bodies_with_file_write_call:
        # 只认能还原成字面量路径的写入目标。「正文里提到了这个路径」的启发式会把只读的路径字面量也算成写入
        # （独立复核 M11），归属侧不用；判不准的交给单任务 worktree 默认认领或 agent 认领。
        if script.write_destination_literal_contents is None:
            continue
        write_kind = INLINE_SCRIPT_LITERAL_DESTINATION_WRITE_KIND
        for path, match_kind in _dirty_paths_mentioned_in_inline_script_body(script, roots, dirty_paths, basename_index).items():
            matches.setdefault(path, ShellCommandNamedWriteTargetDirtyPathMatch(
                path, write_kind, match_kind, script.trigger_segment_excerpt))
    return list(matches.values())


def shell_command_runs_git_commit_inside_repository(
    analysis: ShellCommandNamedWriteTargetAnalysis, *, repository_root: Path
) -> bool:
    """命令里有一段 ``git commit`` / ``rvf land commit``，且其有效工作目录在本仓库内。
    工作目录未知（例如 ``cd "$X"`` 之后）时按「不在本仓库」处理，宁可不截断。"""
    roots = _repository_root_spellings(repository_root)
    for working_directory in analysis.git_commit_segment_working_directories:
        if working_directory is None:
            continue
        if _relative_path_under_any_root(roots, os.path.normpath(working_directory)) is not None:
            return True
    return False


def summarize_shell_command_named_write_target_match(match: ShellCommandNamedWriteTargetDirtyPathMatch) -> dict[str, Any]:
    return {
        "path": match.repository_relative_path,
        "write_kind": match.write_kind,
        "match_kind": match.match_kind,
        "trigger_command_excerpt": match.trigger_command_excerpt,
    }
