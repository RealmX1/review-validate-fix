#!/usr/bin/env python3
"""只读统计：RVF session-owned 归属对 Bash / 子代理写入失明导致的漏审——历史复盘。

对 ``~/.rvf/user-local-data/run-logs/runs/*`` 里每个 ``scope_mode=session-owned`` 的 run：

1. 读 scope contract / tracker scope / session manifest 等工件，得到「候选路径」
   （primary ∪ protected ∪ background）与「正式覆盖路径」（tracker scope 路径；无 tracker 时退回
   fix_allowlist）。
2. 读该 run 对应的主会话 transcript（Claude ``.jsonl`` 及其 ``subagents/agent-*.jsonl``；
   Codex rollout），在「上一个同 transcript 的 run 开始 → 本 run 开始」窗口内，按写入来源给每个
   候选路径分类：主会话 Edit/Write、主会话 Bash、子代理、Codex apply_patch / exec。
   - Bash / exec 命令先按命令段切分（heredoc 正文归属引出它的那一段），只把**真实写入目标**记为被写：
     重定向 / tee 的目标，sed -i / perl -i / formatter 写模式 / rm / touch / git rm|restore|checkout 的文件参数，
     mv 的源与目标，cp / install 的目标，heredoc 补丁正文的文件头。python / node 内联脚本：写调用的目标能还原成
     字面量路径时只认目标；还原不了时退回「正文里以完整字符串字面量出现的候选路径」（仍是启发式）。
     每条 shell 来源的漏审都带触发命令摘录，供抽查误判率。
   - 新 Codex 的 ``custom_tool_call`` name=exec 是一段 JS：先解码 JS 字符串，再抽 ``tools.apply_patch`` 的文件头与
     ``tools.exec_command`` 的 cmd；item_completed 信封里的 FileChange 也计入。仍抽不出的 Codex run 记为不可判定。
3. 未覆盖且本会话写过的路径 = 漏审路径（启发式估计，误差双向：见报告「局限」）；统计行数、回执 / handoff /
   最终两行摘要是否提到覆盖缺口、fixer 因 protected 被迫 elevated 的次数，以及各改进候选在历史上能救回多少。
   rvf start / dispatch 回执按回执文本里的 run id 绑定到 run；没有 run id 的回执只在「本 run 开始 → 同 transcript
   下一个带合同 run 开始」之间绑定。
4. ``--only-run-id`` 只筛选要分析与输出的 run；窗口起点、下一轮上界与「后续是否覆盖」索引始终用全部合同历史建立。

只读保证：
- 只以只读方式打开 ``~/.rvf``、transcript、目标仓库下的文件；不打开 tracker SQLite；不调用任何
  ``rvf`` 子命令；git 只跑 ``git log``。
- 唯一的写入是 ``--output-directory`` 下的两个输出文件；该目录不得位于 ``~/.rvf`` 内，也不得位于任何
  git 工作树内（避免输出文件被某个会话的 RVF 当成改动）。
- ``sys.dont_write_bytecode`` 防止导入 RVF 源码时在仓库里生成 ``__pycache__``。

用法::

    python3 rvf-session-owned-attribution-coverage-gap-history-readonly-statistics-script.py \\
        --output-directory /Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/<目录>
    # 只看几个 run（验证用）：追加 --only-run-id <run_id> [--only-run-id ...]
    # 跳过「之后有没有 fix 提交触及漏审文件」的 git 查询：追加 --skip-later-fix-commit-git-query

输出：
- ``per-run-and-per-missed-path-statistics.json``：逐 run 行 + 逐漏审路径行 + 汇总。
- ``summary-tables.md``：报告引用的汇总表。
"""

from __future__ import annotations

import argparse
import collections
import datetime as datetime_module
import fnmatch
import glob
import json
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

DEFAULT_RVF_RUN_LOGS_RUNS_DIRECTORY = Path.home() / ".rvf" / "user-local-data" / "run-logs" / "runs"
RVF_STATE_ROOT_DIRECTORY = Path.home() / ".rvf"
DEFAULT_DECLINED_DECISION_REASONS_LEDGER_PATH = (
    Path.home() / ".rvf" / "agentic-initiation-declined-decision-reasons" / "declined-decision-reasons.jsonl"
)
DEFAULT_AGENTIC_SCOPE_ADJUSTMENTS_DIRECTORY = Path.home() / ".rvf" / "agentic-initiation-scope-adjustments"
DEFAULT_AGENTIC_SCOPE_ISSUE_LEDGER_DIRECTORY = Path.home() / ".rvf" / "agentic-initiation-scope-issue-ledger"
# 本脚本位于 <repo>/docs/investigations/<目录>/ 下。
RVF_SOURCE_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_GITHUB_CHECKOUTS_DIRECTORY = Path.home() / "Documents" / "GitHub"

RUN_ID_PATTERN = re.compile(r"^rvf-(?P<stamp>\d{8}T\d{6})Z-(?P<trigger>.+)-(?P<suffix>[0-9a-f]{8})$")
# 回执文本里出现的 run id（``run_id: rvf-…`` 或 run_dir 路径里的目录名），用于把回执绑定到具体 run。
RUN_ID_MENTIONED_IN_FREE_TEXT_PATTERN = re.compile(r"rvf-\d{8}T\d{6}Z-[A-Za-z0-9-]+?-[0-9a-f]{8}(?![0-9A-Za-z])")
KANBAN_TASK_WORKTREE_PATTERN = re.compile(r"/\.cline/worktrees/(?P<task_id>[0-9a-f]{5})/")

TRIGGER_KIND_DISPLAY_NAMES = {
    "agentic-initiation-start": "agent 自主 start",
    "stop-hook": "旧 Stop hook 派发",
    "user-prompt-submit-manual": "手动触发（UPS）",
    "prepare-run": "手动 prepare_review_run",
    "dispatcher": "dispatcher",
}

CLAUDE_FILE_WRITE_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CLAUDE_SHELL_TOOL_NAMES = {"Bash"}

# Bash / exec 命令的写入判定：先把命令按 &&、||、;、|、&、换行、括号切成命令段（heredoc 正文归属引出
# 它的那一段），再按每段的命令种类只提取「真实写入目标」——重定向 / tee 的目标、sed -i / perl -i /
# formatter 写模式 / rm / touch / git rm|restore|checkout 的文件参数、mv 的源与目标、cp / install 的目标、
# heredoc 补丁正文里的文件头。唯一仍属启发式的一类是 python / node 内联脚本：正文里出现写文件调用时，
# 把同一段脚本正文里提到的候选路径记为被写。
SHELL_REDIRECTION_DEVICE_TARGETS_THAT_ARE_NOT_FILES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
SHELL_CONTROL_KEYWORDS_BEFORE_COMMAND_WORD = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "time"}
SHELL_COMMAND_WRAPPERS_BEFORE_COMMAND_WORD = {"sudo", "env", "command", "nohup", "builtin", "exec", "nice", "caffeinate", "noglob"}
SHELL_WORD_ASSIGNMENT_PATTERN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$", re.DOTALL)
SHELL_VARIABLE_REFERENCE_PATTERN = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
SHELL_MAXIMUM_COMMAND_SUBSTITUTION_NESTING_DEPTH = 16
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
MINIMUM_UNIQUE_BASENAME_LENGTH_FOR_BASENAME_MATCH = 10
SHELL_WRITE_TRIGGER_COMMAND_EXCERPT_MAXIMUM_CHARACTERS = 200
SHELL_DERIVED_MISSED_PATH_SPOT_CHECK_SAMPLE_SIZE = 40
# 0aac3ba（2026-08-15，「补回发起侧 tracker scope 与 lease」）之后，agent 自主发起路径默认走 session-owned；
# 之后仍走 manual-all-uncommitted 的 run 是「归属为空 / allocator 降级」等兜底，统计它们与原因。
AGENTIC_INITIATION_TRACKER_SCOPE_RESTORED_AT_UTC = "2026-08-15T00:00:00"
# scope-of-work 里能识别出的降级原因（按优先级取第一个；同时记录全部命中）
AGENTIC_MANUAL_ALL_UNCOMMITTED_FALLBACK_REASON_PATTERNS = (
    ("degraded_allocator_reported_empty", re.compile(r"degraded_allocator_reported_empty")),
    ("degraded_transcript_schema_drift_suspected", re.compile(r"degraded_transcript_schema_drift_suspected")),
    ("ownership_empty_归属为空", re.compile(r"归属为空|归属全空")),
    ("force_full_rescope", re.compile(r"--force-full-rescope|force_full_rescope")),
)
SHELL_DERIVED_MISSED_PATH_SPOT_CHECK_RANDOM_SEED = 20261004

# 「明确说出有文件没被审」的措辞。protected / background 是 handoff 模板固有字样（模板要求把它们列为
# 「不得自动 stage」），单独出现不算提到覆盖缺口。
COVERAGE_GAP_STATEMENT_PATTERN = re.compile(
    r"覆盖缺口|漏审|未(?:被)?(?:正式)?审(?:查|过|到)?(?!出|计|核)|没(?:有)?(?:被)?(?:正式)?审(?:查|过|到)?(?!出|计|核)|"
    r"只(?:正式)?审(?:查)?了|不在\s*(?:本轮\s*)?(?:review|审查)\s*范围|(?:review|审查)\s*范围(?:之)?外|"
    r"未进入.{0,12}(?:审查|review)|not (?:been )?reviewed|unreviewed|outside (?:the )?review scope",
    re.IGNORECASE,
)
# 命中词附近出现这些字样时，说的是测试覆盖 / e2e 覆盖，不是审查覆盖。
COVERAGE_GAP_FALSE_POSITIVE_NEARBY_PATTERN = re.compile(r"测试|覆盖率|e2e|端到端|用例", re.IGNORECASE)
# 段落里要同时出现「文件 / 范围」类字样，才算在说审查范围。
COVERAGE_GAP_FILE_SCOPE_CONTEXT_PATTERN = re.compile(
    r"protected|background|unattributed|primary|scope|范围|背景|文件|路径|单元|tracker", re.IGNORECASE)
# 段落里还点出了原因（归属对 Bash / 子代理失明），记为「点明归属失明」。
ATTRIBUTION_BLINDNESS_CAUSE_PATTERN = re.compile(
    r"Bash|heredoc|python\s*脚本|\bsed\b|子代理|subagent|归属|attribution|只认|Edit\s*/\s*Write|transcript", re.IGNORECASE)
# 没用「未审」字眼、但明确说出「归属误判」并点出 Bash / 子代理原因的段落，也算发现并提到了。
ATTRIBUTION_MISJUDGMENT_STATEMENT_PATTERN = re.compile(
    r"归属[^\n。]{0,12}(?:误判|失明|抓不到|漏判|错配|为空|认不出|没认|未识别)|"
    r"(?:误判|误标|误归)[^\n。]{0,6}(?:为|成)?\s*(?:protected|background|背景)|"
    r"未归属|无归属|unattributed|ownership tracker", re.IGNORECASE)
ATTRIBUTION_MISJUDGMENT_CAUSE_PATTERN = re.compile(
    r"Bash|heredoc|python\s*脚本|\bsed\b|子代理|subagent|只认|Edit\s*/\s*Write|shell", re.IGNORECASE)
CLAIMED_REVIEWED_FILE_COUNT_PATTERN = re.compile(
    r"(?:审(?:查|了|过)?(?:了)?\s*(?P<a>\d+)\s*个(?:改动|未提交)?(?:文件|路径))|"
    r"(?:(?P<b>\d+)\s*个(?:改动|未提交)?(?:文件|路径)[^\n]{0,8}(?:已|全部|均)?(?:被)?审)|"
    r"(?:reviewed\s+(?P<c>\d+)\s+files)|"
    # handoff 模板的「reviewed scope paths」「主审查文件 / 范围」字段里写的数字，也是对覆盖面的声明
    r"(?:(?:reviewed scope paths|主审查文件|审查范围)[^\n]{0,40}?(?P<d>\d+)\s*个)",
    re.IGNORECASE,
)
START_RECEIPT_PATTERN = re.compile(r"自动 scope (?P<auto_scope_paths>\d+) 路径(?:；tracker scope 已分配[^）]*?(?P<units>\d+) 个可审单元)?")
DISPATCH_RECEIPT_PATTERN = re.compile(r"已按 (?P<strictness>\S+) 档派发 reviewer（primary_files (?P<primary_files>\d+)")
RVF_COMMAND_IN_SHELL_PATTERN = re.compile(r"\brvf(?:-agent)?\b[^\n|;&]*\b(?:start|dispatch)\b|rvf_agent_decision_cli\.py[^\n]*\b(?:start|dispatch)\b")
ATTRIBUTION_BLINDNESS_DECLINE_REASON_PATTERN = re.compile(
    r"Bash|子代理|subagent|归属|session-owned|覆盖|只认|未审|漏审|protected|background", re.IGNORECASE
)
ELEVATION_CAUSED_BY_PROTECTED_SCOPE_PATTERN = re.compile(
    r"protected|background|fix_allowlist|allowlist|无权|不准|不得修改|不允许修改|scope expansion|越界|边界外", re.IGNORECASE
)


# --------------------------------------------------------------------------------------------
# 通用只读工具
# --------------------------------------------------------------------------------------------


def read_json_file_or_none(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def read_text_file_or_empty(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return ""


def iterate_jsonl_records(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict):
                    yield line_number, record
    except FileNotFoundError:
        return


def normalize_timestamp_to_utc_second_precision(value: Any) -> str:
    """统一成 ``YYYY-MM-DDTHH:MM:SS``（UTC）便于字符串比较。"""
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    try:
        parsed = datetime_module.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:19]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime_module.timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def run_started_at_utc_from_run_id(run_id: str) -> str:
    match = RUN_ID_PATTERN.match(run_id)
    if not match:
        return ""
    stamp = match.group("stamp")
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"


def path_is_inside_any_git_work_tree(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return True
    return False


def load_rvf_shell_command_path_candidate_functions() -> tuple[Any, Any]:
    """复用 RVF 自己的 ``command_path_candidates``（对 Codex exec_command 的写入目标正则），
    用来回答「对 Claude Bash 也跑同一套正则能救回多少」。导入失败时返回 (None, None)。"""
    sys.path.insert(0, str(RVF_SOURCE_REPOSITORY_ROOT))
    try:
        from core.session_scope_allocation.session_change_manifest import (  # noqa: PLC0415
            command_path_candidates,
            command_workdir,
        )
    except Exception as error:  # pragma: no cover - 诊断输出
        print(f"[warn] 无法导入 RVF command_path_candidates：{error}", file=sys.stderr)
        return None, None
    return command_path_candidates, command_workdir


# --------------------------------------------------------------------------------------------
# transcript 解析
# --------------------------------------------------------------------------------------------


@dataclass
class SessionToolEvent:
    writer_origin: str  # main_session | subagent
    timestamp_utc: str
    line_number: int
    tool_name: str
    file_path: str | None = None
    shell_command: str | None = None
    shell_working_directory: str | None = None
    subagent_description: str | None = None
    codex_patch_paths: tuple[str, ...] = ()
    source_transcript_path: str = ""
    # 新 Codex exec（JS 文本）里出现了 tools.apply_patch / tools.exec_command 调用却抽不出路径或命令：
    # 该事件不可判定，所在窗口的 run 记为不可判定，而不是静默当作 0 写入。
    codex_exec_input_unparseable: bool = False
    # 按命令段解析出的写入目标；与候选集无关，同一事件在多个 run 之间复用，惰性计算一次。
    cached_shell_command_write_analysis: Any = None


@dataclass
class ParsedSessionTranscript:
    host_kind: str
    transcript_path: str
    main_session_events: list[SessionToolEvent] = field(default_factory=list)
    subagent_events: list[SessionToolEvent] = field(default_factory=list)
    subagent_transcript_count: int = 0
    rvf_start_or_dispatch_receipts: list[dict[str, Any]] = field(default_factory=list)
    final_replies_with_rvf_handoff_file: list[dict[str, Any]] = field(default_factory=list)


def tool_result_block_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""


def parse_claude_transcript_tool_events(
    transcript_path: Path, writer_origin: str, subagent_description: str | None = None, collect_receipts: bool = False
) -> tuple[list[SessionToolEvent], list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[SessionToolEvent] = []
    receipts: list[dict[str, Any]] = []
    final_replies: list[dict[str, Any]] = []
    rvf_shell_tool_use_ids: dict[str, dict[str, Any]] = {}
    for line_number, record in iterate_jsonl_records(transcript_path):
        timestamp_utc = normalize_timestamp_to_utc_second_precision(record.get("timestamp"))
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_name = block.get("name")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if tool_name in CLAUDE_FILE_WRITE_TOOL_NAMES:
                    file_path = tool_input.get("notebook_path" if tool_name == "NotebookEdit" else "file_path")
                    if isinstance(file_path, str):
                        events.append(SessionToolEvent(writer_origin, timestamp_utc, line_number, tool_name,
                                                       file_path=file_path, subagent_description=subagent_description,
                                                       source_transcript_path=str(transcript_path)))
                elif tool_name in CLAUDE_SHELL_TOOL_NAMES:
                    command = tool_input.get("command")
                    if isinstance(command, str):
                        events.append(SessionToolEvent(writer_origin, timestamp_utc, line_number, tool_name,
                                                       shell_command=command,
                                                       shell_working_directory=record.get("cwd"),
                                                       subagent_description=subagent_description,
                                                       source_transcript_path=str(transcript_path)))
                        if collect_receipts and RVF_COMMAND_IN_SHELL_PATTERN.search(command):
                            rvf_shell_tool_use_ids[str(block.get("id"))] = {
                                "line_number": line_number, "timestamp_utc": timestamp_utc, "command": command[:300]}
                elif tool_name in ("Agent", "Task"):
                    events.append(SessionToolEvent(writer_origin, timestamp_utc, line_number, "Agent",
                                                   subagent_description=str(tool_input.get("description") or "")[:120],
                                                   source_transcript_path=str(transcript_path)))
            elif collect_receipts and block_type == "tool_result" and str(block.get("tool_use_id")) in rvf_shell_tool_use_ids:
                origin = rvf_shell_tool_use_ids[str(block.get("tool_use_id"))]
                text = tool_result_block_text(block)
                receipt: dict[str, Any] = dict(origin)
                # 回执结果到达的时刻（run 已创建之后）；tool_use 的 timestamp 早于 run 创建，不能拿来比较。
                receipt["result_line_number"] = line_number
                receipt["result_timestamp_utc"] = timestamp_utc
                # 回执里出现的全部 run id（``run_id: …`` 行、run_dir / dispatch_status_path 路径），用于绑定到具体 run。
                receipt["receipt_mentioned_run_ids"] = sorted(set(RUN_ID_MENTIONED_IN_FREE_TEXT_PATTERN.findall(text)))
                start_match = START_RECEIPT_PATTERN.search(text)
                dispatch_match = DISPATCH_RECEIPT_PATTERN.search(text)
                if start_match:
                    receipt["start_receipt_auto_scope_path_count"] = int(start_match.group("auto_scope_paths"))
                    if start_match.group("units"):
                        receipt["start_receipt_reviewable_unit_count"] = int(start_match.group("units"))
                if dispatch_match:
                    receipt["dispatch_receipt_primary_files_count"] = int(dispatch_match.group("primary_files"))
                    receipt["dispatch_receipt_strictness"] = dispatch_match.group("strictness")
                if start_match or dispatch_match:
                    receipts.append(receipt)
            elif collect_receipts and block_type == "text" and record.get("type") == "assistant":
                text = block.get("text") or ""
                if "RVF_HANDOFF_FILE:" in text:
                    final_replies.append({"line_number": line_number, "timestamp_utc": timestamp_utc, "text": text[:8000]})
    return events, receipts, final_replies


def parse_claude_session_with_subagents(transcript_path: Path) -> ParsedSessionTranscript:
    parsed = ParsedSessionTranscript(host_kind="claude_code", transcript_path=str(transcript_path))
    main_events, receipts, final_replies = parse_claude_transcript_tool_events(
        transcript_path, "main_session", collect_receipts=True)
    parsed.main_session_events = main_events
    parsed.rvf_start_or_dispatch_receipts = receipts
    parsed.final_replies_with_rvf_handoff_file = final_replies
    # 与 adapters/claude_code/subagent.py::_subagents_dir 同口径：<proj>/<uuid>.jsonl → <proj>/<uuid>/subagents
    subagents_directory = transcript_path.parent / transcript_path.stem / "subagents"
    for subagent_transcript in sorted(subagents_directory.glob("agent-*.jsonl")):
        parsed.subagent_transcript_count += 1
        meta = read_json_file_or_none(subagent_transcript.with_suffix(".meta.json")) or {}
        description = str(meta.get("description") or "")[:160] if isinstance(meta, dict) else ""
        subagent_events, _, _ = parse_claude_transcript_tool_events(subagent_transcript, "subagent", description)
        parsed.subagent_events.extend(subagent_events)
    return parsed


CODEX_APPLY_PATCH_FILE_HEADER_PATTERN = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
# 新 Codex（gpt-5.6-sol 世代）的 custom_tool_call name=exec：input 是一段 JS，内含
# ``tools.apply_patch("*** Begin Patch\n…")``（JS 字符串，换行是 ``\n`` 转义）与
# ``tools.exec_command({cmd: "…", workdir: "…"})``。
CODEX_EXEC_JAVASCRIPT_APPLY_PATCH_CALL_PATTERN = re.compile(r"tools\s*\.\s*apply_patch\s*\(")
CODEX_EXEC_JAVASCRIPT_EXEC_COMMAND_CALL_PATTERN = re.compile(r"tools\s*\.\s*exec_command\s*\(")
CODEX_EXEC_JAVASCRIPT_ANY_TOOL_CALL_PATTERN = re.compile(r"tools\s*\.\s*[A-Za-z_]\w*\s*\(")
JAVASCRIPT_STRING_LITERAL_PATTERN = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`', re.DOTALL)
JAVASCRIPT_OBJECT_STRING_PROPERTY_PATTERN_TEMPLATE = r"(?:\b|[\"']){name}[\"']?\s*:\s*(?P<literal>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)"
JAVASCRIPT_OBJECT_IDENTIFIER_PROPERTY_PATTERN_TEMPLATE = r"(?:\b|[\"']){name}[\"']?\s*:\s*(?P<identifier>[A-Za-z_$][\w$]*)\b|[{{,]\s*(?P<shorthand>{name})\s*[,}}]"
JAVASCRIPT_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def decode_javascript_string_literal(literal: str) -> str:
    """把带引号的 JS 字符串字面量（"…" / '…' / `…`）解码成真实文本。"""
    body = literal[1:-1]
    decoded: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 >= len(body):
            decoded.append(character)
            index += 1
            continue
        escaped = body[index + 1]
        if escaped in JAVASCRIPT_SIMPLE_ESCAPES:
            decoded.append(JAVASCRIPT_SIMPLE_ESCAPES[escaped])
            index += 2
        elif escaped == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", body[index + 2:index + 4]):
            decoded.append(chr(int(body[index + 2:index + 4], 16)))
            index += 4
        elif escaped == "u" and body[index + 2:index + 3] == "{":
            closing = body.find("}", index + 3)
            try:
                decoded.append(chr(int(body[index + 3:closing], 16)))
                index = closing + 1
            except ValueError:
                decoded.append("u")
                index += 2
        elif escaped == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", body[index + 2:index + 6]):
            decoded.append(chr(int(body[index + 2:index + 6], 16)))
            index += 6
        elif escaped == "\n":
            index += 2  # 行接续
        else:
            decoded.append(escaped)
            index += 2
    return "".join(decoded)


def javascript_identifier_string_assignments(javascript_text: str) -> dict[str, str]:
    """``const name = "…"`` / ``let`` / ``var`` 形式的字符串常量，供 ``cmd: name`` / ``{cmd}`` 回查。"""
    assignments: dict[str, str] = {}
    for match in re.finditer(
            r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<literal>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)",
            javascript_text, re.DOTALL):
        assignments[match.group("name")] = decode_javascript_string_literal(match.group("literal"))
    return assignments


def javascript_object_string_property_value(
    call_argument_text: str, property_name: str, identifier_assignments: dict[str, str]
) -> str | None:
    literal_match = re.search(JAVASCRIPT_OBJECT_STRING_PROPERTY_PATTERN_TEMPLATE.format(name=property_name),
                              call_argument_text, re.DOTALL)
    if literal_match:
        return decode_javascript_string_literal(literal_match.group("literal"))
    identifier_match = re.search(JAVASCRIPT_OBJECT_IDENTIFIER_PROPERTY_PATTERN_TEMPLATE.format(name=property_name),
                                 call_argument_text)
    if identifier_match:
        identifier = identifier_match.group("identifier") or identifier_match.group("shorthand")
        return identifier_assignments.get(identifier)
    return None


def extract_codex_exec_javascript_tool_calls(javascript_text: str) -> tuple[list[str], list[tuple[str, str | None]], bool]:
    """从新 Codex exec 的 JS 文本里抽出 (apply_patch 文件头路径, [(exec_command 的 cmd, workdir)], 是否有调用抽不出)。"""
    identifier_assignments = javascript_identifier_string_assignments(javascript_text)
    apply_patch_paths: list[str] = []
    exec_commands: list[tuple[str, str | None]] = []
    has_unextractable_call = False
    if CODEX_EXEC_JAVASCRIPT_APPLY_PATCH_CALL_PATTERN.search(javascript_text):
        for literal_match in JAVASCRIPT_STRING_LITERAL_PATTERN.finditer(javascript_text):
            decoded = decode_javascript_string_literal(literal_match.group(0))
            if "*** " not in decoded:
                continue
            for header_match in CODEX_APPLY_PATCH_FILE_HEADER_PATTERN.finditer(decoded):
                path = (header_match.group(1) or header_match.group(2) or "").strip()
                if path:
                    apply_patch_paths.append(path)
        if not apply_patch_paths:
            has_unextractable_call = True
    call_starts = [match.start() for match in CODEX_EXEC_JAVASCRIPT_ANY_TOOL_CALL_PATTERN.finditer(javascript_text)]
    for match in CODEX_EXEC_JAVASCRIPT_EXEC_COMMAND_CALL_PATTERN.finditer(javascript_text):
        # 本次调用的参数文本：从调用处到下一个 tools.xxx( 调用之前
        following_starts = [start for start in call_starts if start > match.start()]
        call_argument_text = javascript_text[match.end():following_starts[0] if following_starts else len(javascript_text)]
        command = javascript_object_string_property_value(call_argument_text, "cmd", identifier_assignments)
        if command is None:
            command = javascript_object_string_property_value(call_argument_text, "command", identifier_assignments)
        workdir = javascript_object_string_property_value(call_argument_text, "workdir", identifier_assignments)
        if command is None:
            # cmd 来自数组 / 循环变量（``for (const [label, cmd] of commands)``）：退回「本段 JS 里所有像命令的
            # 字符串字面量」——这些数组元素就是实际执行的命令；一条都没有时才记为抽不出。
            fallback_commands = [
                decoded for decoded in (decode_javascript_string_literal(literal.group(0))
                                        for literal in JAVASCRIPT_STRING_LITERAL_PATTERN.finditer(javascript_text))
                if " " in decoded.strip() and decoded != workdir]
            if not fallback_commands:
                has_unextractable_call = True
                continue
            exec_commands.extend((fallback_command, workdir) for fallback_command in dict.fromkeys(fallback_commands)
                                 if (fallback_command, workdir) not in exec_commands)
            continue
        exec_commands.append((command, workdir))
    return apply_patch_paths, exec_commands, has_unextractable_call


def codex_shell_command_text_from_arguments(command: Any) -> str | None:
    """旧 Codex function_call 的 cmd / command：字符串原样；``["bash", "-lc", "…"]`` 取真正的命令串。"""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command:
        parts = [str(part) for part in command]
        if len(parts) >= 3 and os.path.basename(parts[0]) in ("bash", "zsh", "sh") and parts[1] in ("-lc", "-c"):
            return parts[2]
        return " ".join(parts)
    return None


def parse_codex_rollout(transcript_path: Path) -> ParsedSessionTranscript:
    parsed = ParsedSessionTranscript(host_kind="codex", transcript_path=str(transcript_path))
    session_working_directory: str | None = None
    for line_number, record in iterate_jsonl_records(transcript_path):
        timestamp_utc = normalize_timestamp_to_utc_second_precision(record.get("timestamp"))
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "session_meta" and isinstance(payload.get("cwd"), str):
            session_working_directory = payload["cwd"]
        # item_completed 信封：真正的条目在 payload.item 里
        if isinstance(payload.get("item"), dict):
            payload = payload["item"]
        payload_type = payload.get("type")

        def append_event(tool_name: str, **fields: Any) -> None:
            parsed.main_session_events.append(SessionToolEvent(
                "main_session", timestamp_utc, line_number, tool_name, source_transcript_path=str(transcript_path), **fields))

        if payload_type == "function_call" and payload.get("name") in ("exec_command", "shell", "local_shell"):
            arguments = payload.get("arguments")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except Exception:
                arguments = {}
            arguments = arguments if isinstance(arguments, dict) else {}
            command = codex_shell_command_text_from_arguments(arguments.get("cmd") or arguments.get("command"))
            if isinstance(command, str):
                append_event("codex_exec_command", shell_command=command,
                             shell_working_directory=arguments.get("workdir") or session_working_directory)
        elif payload_type in ("custom_tool_call", "function_call") and payload.get("name") == "apply_patch":
            # 旧 Codex：裸 apply_patch 文本（真实换行）
            patch_text = payload.get("input") or payload.get("arguments") or ""
            if isinstance(patch_text, str):
                paths = tuple((match.group(1) or match.group(2)).strip()
                              for match in CODEX_APPLY_PATCH_FILE_HEADER_PATTERN.finditer(patch_text))
                if paths:
                    append_event("codex_apply_patch", codex_patch_paths=paths, shell_working_directory=session_working_directory)
        elif payload_type in ("custom_tool_call", "function_call") and payload.get("name") == "exec":
            # 新 Codex：JS 文本里内嵌 tools.apply_patch / tools.exec_command，先解码 JS 字符串再抽取
            javascript_text = payload.get("input") or payload.get("arguments") or ""
            if not isinstance(javascript_text, str):
                continue
            apply_patch_paths, exec_commands, has_unextractable_call = extract_codex_exec_javascript_tool_calls(javascript_text)
            if apply_patch_paths:
                append_event("codex_apply_patch", codex_patch_paths=tuple(apply_patch_paths),
                             shell_working_directory=session_working_directory)
            for command, workdir in exec_commands:
                append_event("codex_exec_command", shell_command=command,
                             shell_working_directory=workdir or session_working_directory)
            if has_unextractable_call:
                append_event("codex_exec_input_unparseable", codex_exec_input_unparseable=True)
        elif payload_type in ("patch_apply_end", "FileChange") and isinstance(payload.get("changes"), dict):
            # 结构化的编辑结果：旧 schema 的 patch_apply_end、item_completed 信封里的 FileChange
            append_event("codex_apply_patch", codex_patch_paths=tuple(str(path) for path in payload["changes"].keys()),
                         shell_working_directory=session_working_directory)
        # item_completed 信封里的 CommandExecution 与上面的 exec_command 是同一条命令的完成记录，不重复计入。
    return parsed


def infer_host_kind_from_transcript_path(transcript_path: str | None) -> str:
    if not transcript_path:
        return "unknown_no_transcript"
    if "/.claude/projects/" in transcript_path:
        return "claude_code"
    if "/.codex/sessions/" in transcript_path:
        return "codex"
    if "/.kimi" in transcript_path:
        return "kimi"
    if "/.cursor" in transcript_path:
        return "cursor"
    return "unknown_other"


# --------------------------------------------------------------------------------------------
# 写入来源判定
# --------------------------------------------------------------------------------------------


def repository_relative_path_or_none(repository_roots: tuple[str, ...], file_path: str, working_directory: str | None) -> str | None:
    value = file_path.strip().strip("'\"")
    if not value:
        return None
    if not os.path.isabs(value):
        base = working_directory or repository_roots[0]
        value = os.path.normpath(os.path.join(base, value))
    for root in repository_roots:
        if value.startswith(root + "/"):
            return value[len(root) + 1:]
    return None


# --------------------------------------------------------------------------------------------
# shell 命令 → 命令段 → 写入目标
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


def find_unescaped_character(text: str, character: str, start: int) -> int:
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
    scan_shell_command_into_segments(command, 0, False, segments, 0)
    return segments


def scan_shell_command_into_segments(
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
        return scan_shell_command_into_segments(command, position, True, segments, nesting_depth + 1)

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
                closing = find_unescaped_character(command, "`", position + 1)
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
            closing = find_unescaped_character(command, "`", index + 1)
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


@dataclass
class ShellWriteTargetToken:
    """某个命令段里一个确定的写入目标（尚未对照候选集解析）。"""
    write_kind: str
    target_token: str
    working_directory: str | None
    trigger_segment_excerpt: str
    allow_unique_basename_fallback: bool = True
    match_candidates_under_directory_prefix: bool = False


@dataclass
class InlineScriptBodyWithFileWriteCall:
    """python / node 内联脚本正文里出现了写文件调用：正文里以完整单行字符串字面量出现的候选路径记为被写（启发式）。"""
    unique_single_line_string_literal_contents: tuple[str, ...]
    # 每个写调用的目标都能还原成字符串字面量时，这里是这些目标（精确模式）；否则为 None（退回字面量提及启发式）
    write_destination_literal_contents: tuple[str, ...] | None
    working_directory: str | None
    script_body_text: str
    trigger_segment_excerpt: str


@dataclass
class ShellCommandWriteAnalysis:
    write_target_tokens: list[ShellWriteTargetToken] = field(default_factory=list)
    inline_script_bodies_with_file_write_call: list[InlineScriptBodyWithFileWriteCall] = field(default_factory=list)
    # 会改文件但无法落到具体路径的命令段（formatter 对目录、git stash pop、xargs rm、目标是未解析变量的重定向…）
    bulk_or_unresolvable_target_write_segment_count: int = 0
    shell_parse_failed: bool = False


INLINE_SCRIPT_WRITE_KIND = "inline_python_or_node_script_write_call_with_path_mention"
INLINE_SCRIPT_LITERAL_DESTINATION_WRITE_KIND = "inline_python_or_node_script_write_call_literal_destination"
FORMATTER_OR_INTERPRETER_RUNNER_COMMAND_NAMES = {
    "npx", "bunx", "pnpm", "pnpx", "yarn", "uv", "uvx", "poetry", "pipx", "pdm", "hatch", "bun", "python", "python3",
}
XARGS_OR_FIND_EXEC_WRITER_COMMAND_NAMES = {
    "rm", "sed", "perl", "mv", "cp", "touch", "truncate", "git", "npx", "bunx", "chmod",
} | FORMATTER_OR_LINTER_COMMAND_NAMES


def expand_shell_variables_in_word(word: str, shell_variables: dict[str, str]) -> str:
    expanded = SHELL_VARIABLE_REFERENCE_PATTERN.sub(
        lambda match: shell_variables.get(match.group("braced") or match.group("plain"), match.group(0)), word)
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(Path.home()) + expanded[1:]
    return expanded


def shell_short_option_letters(argument: str) -> str:
    match = re.match(r"^-([A-Za-z0-9]+)", argument)
    return match.group(1) if match and not argument.startswith("--") else ""


def shell_positional_arguments(arguments: list[str], options_taking_value: Iterable[str] = ()) -> list[str]:
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


def shell_change_directory_destination(arguments: list[str], current_working_directory: str | None) -> str | None:
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


def formatter_or_linter_runs_in_write_mode(formatter_name: str, formatter_arguments: list[str]) -> bool:
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


def analyze_shell_command_write_targets(command: str, initial_working_directory: str | None) -> ShellCommandWriteAnalysis:
    """把一条 shell 命令按命令段解析出写入目标。与候选集无关，可缓存复用。"""
    analysis = ShellCommandWriteAnalysis()
    try:
        segments = split_shell_command_into_segments(command)
    except Exception:  # pragma: no cover - 防御：解析器异常不应中断统计
        analysis.shell_parse_failed = True
        return analysis
    working_directory = os.path.normpath(initial_working_directory) if initial_working_directory else None
    shell_variables: dict[str, str] = {"HOME": str(Path.home())}
    for segment in segments:
        excerpt = segment.shell_text[:SHELL_WRITE_TRIGGER_COMMAND_EXCERPT_MAXIMUM_CHARACTERS]
        segment_working_directory = working_directory

        def add_write_target(write_kind: str, token: str, *, allow_unique_basename_fallback: bool = True,
                             match_candidates_under_directory_prefix: bool = False,
                             target_working_directory: str | None = None) -> None:
            analysis.write_target_tokens.append(ShellWriteTargetToken(
                write_kind, token,
                target_working_directory if target_working_directory is not None else segment_working_directory,
                excerpt, allow_unique_basename_fallback, match_candidates_under_directory_prefix))

        words = list(segment.words)
        position = 0
        while position < len(words) and words[position] in SHELL_CONTROL_KEYWORDS_BEFORE_COMMAND_WORD:
            position += 1
        while position < len(words):
            assignment = SHELL_WORD_ASSIGNMENT_PATTERN.match(words[position])
            if not assignment:
                break
            shell_variables[assignment.group("name")] = expand_shell_variables_in_word(assignment.group("value"), shell_variables)
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
        command_words = [expand_shell_variables_in_word(word, shell_variables) for word in words[position:]]

        # 1) 重定向：只认目标路径（2>&1、>&2 之类 fd 复制与 /dev/null 不算）
        for operator, target in segment.redirections:
            if ">" not in operator:
                continue
            if operator.endswith(">&") and (target.isdigit() or target == "-"):
                continue
            expanded_target = expand_shell_variables_in_word(target, shell_variables)
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

        # 2) 按命令种类提取文件参数
        if command_name in ("cd", "pushd"):
            working_directory = shell_change_directory_destination(arguments, working_directory)
            continue
        if command_name in ("export", "declare", "local", "readonly", "typeset"):
            for argument in arguments:
                assignment = SHELL_WORD_ASSIGNMENT_PATTERN.match(argument)
                if assignment:
                    shell_variables[assignment.group("name")] = assignment.group("value")
            continue
        if command_name == "tee":
            for target in shell_positional_arguments(arguments):
                add_write_target("tee_command_file_argument", target)
        elif command_name in ("sed", "gsed") and any(
                argument == "--in-place" or argument.startswith("--in-place=") or "i" in shell_short_option_letters(argument)
                for argument in arguments):
            positional = shell_positional_arguments(arguments, {"-e", "--expression", "-f", "--file", "-l"})
            has_explicit_script_option = any(argument in ("-e", "--expression", "-f", "--file")
                                             or argument.startswith("--expression=") for argument in arguments)
            for target in (positional if has_explicit_script_option else positional[1:]):
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name == "perl" and any("i" in shell_short_option_letters(argument) for argument in arguments):
            positional = shell_positional_arguments(arguments, {"-e", "-E"})
            cluster_ending_with_code_option = any(
                shell_short_option_letters(argument)[-1:] in ("e", "E") and argument not in ("-e", "-E") for argument in arguments)
            has_separate_code_option = any(argument in ("-e", "-E") for argument in arguments)
            files = positional[1:] if cluster_ending_with_code_option or not has_separate_code_option else positional
            for target in files:
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name in ("mv", "cp", "install", "rsync", "ditto", "ln"):
            add_move_or_copy_write_targets(command_name, arguments, add_write_target)
        elif command_name in ("rm", "unlink", "touch", "truncate", "shred"):
            recursive = any(argument in ("--recursive",) or set("rR") & set(shell_short_option_letters(argument))
                            for argument in arguments) and command_name == "rm"
            for target in shell_positional_arguments(arguments, {"-s", "--size", "-r"} if command_name == "truncate" else ()):
                add_write_target("remove_or_touch_or_truncate_file_argument", target,
                                 match_candidates_under_directory_prefix=recursive)
        elif command_name == "git":
            analyze_git_segment_write_targets(arguments, segment, pipe_source_heredoc_bodies, working_directory,
                                              add_write_target, analysis)
        elif command_name in ("patch", "apply_patch", "applypatch"):
            header_paths = patch_text_file_header_paths("\n".join([*segment.heredoc_bodies, *pipe_source_heredoc_bodies, *arguments]))
            for target in header_paths:
                add_write_target("patch_text_file_header_in_heredoc_or_argument", target)
            if not header_paths:
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
        elif command_name in ("curl", "wget"):
            for option_index, argument in enumerate(arguments[:-1]):
                letters = shell_short_option_letters(argument)
                if (command_name == "curl" and (argument == "--output" or letters.endswith("o"))) or \
                        (command_name == "wget" and (argument == "--output-document" or letters.endswith("O"))):
                    add_write_target("download_output_file_argument", arguments[option_index + 1])
        elif command_name == "yq" and any(argument in ("-i", "--inplace") for argument in arguments):
            for target in shell_positional_arguments(arguments)[1:]:
                add_write_target("sed_or_perl_in_place_edit_file_argument", target)
        elif command_name == "xargs":
            following = shell_positional_arguments(arguments, {"-I", "-n", "-P", "-L", "-d", "-E", "-s"})
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
            if formatter_or_linter_runs_in_write_mode(formatter_name, formatter_arguments):
                positional = [argument for argument in shell_positional_arguments(formatter_arguments, {
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
                    inline_script_path_like_string_literal_contents(script_body_text),
                    inline_script_write_destination_literal_contents_or_none(script_body_text), segment_working_directory,
                    script_body_text, excerpt))
    return analysis


def add_move_or_copy_write_targets(command_name: str, arguments: list[str], add_write_target: Any) -> None:
    """mv：源（被删）与目标都算写入；cp / install / rsync / ditto / ln：只认目标。目标是目录时补上「目录/源文件名」。"""
    target_directory: str | None = None
    for option_index, argument in enumerate(arguments):
        if argument in ("-t", "--target-directory") and option_index + 1 < len(arguments):
            target_directory = arguments[option_index + 1]
        elif argument.startswith("--target-directory="):
            target_directory = argument.split("=", 1)[1]
    positional = shell_positional_arguments(arguments, {
        "-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group", "-e", "--exclude"})
    if target_directory is None:
        if len(positional) < 2:
            return
        sources, destination = positional[:-1], positional[-1]
    else:
        sources, destination = positional, target_directory
    recursive_copy = command_name in ("cp", "rsync", "ditto") and any(
        argument in ("--recursive", "--archive") or set("rRa") & set(shell_short_option_letters(argument)) for argument in arguments)
    destination_kind = "move_command_source_or_destination" if command_name == "mv" else "copy_or_install_command_destination"
    add_write_target(destination_kind, destination)
    for source in sources:
        add_write_target(destination_kind, destination.rstrip("/") + "/" + os.path.basename(source.rstrip("/")),
                         allow_unique_basename_fallback=False)
        if command_name == "mv":
            add_write_target(destination_kind, source)
    if recursive_copy:
        add_write_target("copy_recursive_directory_destination_prefix", destination,
                         allow_unique_basename_fallback=False, match_candidates_under_directory_prefix=True)


def analyze_git_segment_write_targets(
    arguments: list[str], segment: ShellCommandSegment, pipe_source_heredoc_bodies: list[str],
    working_directory: str | None, add_write_target: Any, analysis: ShellCommandWriteAnalysis,
) -> None:
    git_working_directory = working_directory
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-C" and index + 1 < len(arguments):
            git_working_directory = shell_change_directory_destination([arguments[index + 1]], git_working_directory)
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
    if subcommand == "mv":
        add_move_or_copy_write_targets("mv", subcommand_arguments,
                                       lambda write_kind, token, **options: add_write_target(
                                           kind, token, target_working_directory=git_working_directory, **options))
    elif subcommand == "rm":
        recursive = any("r" in shell_short_option_letters(argument) for argument in subcommand_arguments)
        for target in shell_positional_arguments(subcommand_arguments):
            add_write_target(kind, target, match_candidates_under_directory_prefix=recursive,
                             target_working_directory=git_working_directory)
    elif subcommand in ("restore", "checkout"):
        if subcommand == "checkout" and "--" in subcommand_arguments:
            paths = subcommand_arguments[subcommand_arguments.index("--") + 1:]
        else:
            paths = shell_positional_arguments(subcommand_arguments, {"-s", "--source", "-b", "-B", "--orphan"})
        for target in paths:
            if target in (".", "./", ":/"):
                analysis.bulk_or_unresolvable_target_write_segment_count += 1
                continue
            add_write_target(kind, target, target_working_directory=git_working_directory)
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


def repository_relative_path_of_absolute_path(repository_roots: tuple[str, ...], absolute_path: str) -> str | None:
    for root in repository_roots:
        if absolute_path == root:
            return ""
        if absolute_path.startswith(root + "/"):
            return absolute_path[len(root) + 1:]
    return None


def resolve_shell_write_target_token_to_candidate_paths(
    target: ShellWriteTargetToken, repository_roots: tuple[str, ...], candidate_paths: set[str],
    unique_basename_to_candidate: dict[str, str],
) -> list[tuple[str, str]]:
    """写入目标 → 候选路径列表 [(路径, 匹配方式)]。仓库外的目标、未解析的变量一律不匹配。"""
    token = target.target_token.strip()
    if not token or "$" in token or "`" in token or "\n" in token:
        return []
    while token.startswith("./"):
        token = token[2:]
    working_directory = target.working_directory
    resolved_outside_repository = False
    relative_candidates: list[str] = []
    if os.path.isabs(token):
        relative = repository_relative_path_of_absolute_path(repository_roots, os.path.normpath(token))
        if relative is None:
            return []
        relative_candidates.append(relative)
    else:
        if working_directory:
            relative = repository_relative_path_of_absolute_path(
                repository_roots, os.path.normpath(os.path.join(working_directory, token)))
            if relative is None:
                resolved_outside_repository = True
            else:
                relative_candidates.append(relative)
        # 记录里的 cwd 会跟随会话内持久的 cd（出项目目录时被宿主重置），以它为准；只有 cwd 未知时才按「相对仓库根」解析
        elif not token.startswith(".."):
            relative_candidates.append(os.path.normpath(token))
    if any(character in token for character in "*?["):
        matches = [(path, "glob_pattern") for relative in relative_candidates
                   for path in sorted(candidate_paths) if fnmatch.fnmatchcase(path, relative)]
        return list(dict.fromkeys(matches))
    for relative in relative_candidates:
        if relative in candidate_paths:
            return [(relative, "exact_relative_or_absolute_path")]
    if target.match_candidates_under_directory_prefix:
        for relative in relative_candidates:
            if relative in ("", "."):
                continue
            prefix = relative.rstrip("/") + "/"
            hits = [(path, "under_written_directory_prefix") for path in sorted(candidate_paths) if path.startswith(prefix)]
            if hits:
                return hits
    if target.allow_unique_basename_fallback and not os.path.isabs(token) and not resolved_outside_repository:
        candidate = unique_basename_to_candidate.get(token.rsplit("/", 1)[-1])
        if candidate:
            return [(candidate, "unique_basename")]
    return []


def inline_script_path_like_string_literal_contents(script_body_text: str) -> tuple[str, ...]:
    """内联脚本里像路径的单行字符串字面量内容（不含空白、含 / 或 .）。三引号字符串整体剔除。"""
    without_triple_quoted = INLINE_SCRIPT_TRIPLE_QUOTED_STRING_PATTERN.sub("", script_body_text)
    contents: set[str] = set()
    for match in INLINE_SCRIPT_SINGLE_LINE_STRING_LITERAL_PATTERN.finditer(without_triple_quoted):
        content = next((group for group in match.groups() if group is not None), "")
        if len(content) >= 3 and not re.search(r"\s", content) and ("/" in content or "." in content):
            contents.add(content)
    return tuple(sorted(contents))


def inline_script_path_expression_literal_value_or_none(expression: str) -> str | None:
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


def inline_script_write_destination_literal_contents_or_none(script_body_text: str) -> tuple[str, ...] | None:
    """全部「带目标参数的写调用」的目标都能还原成字面量路径（直接写、Path(…)、ROOT / "…"、或变量只被赋过字面量
    路径 / for 循环遍历字面量列表）时，返回这些目标；有任一目标还原不了、或一个带目标参数的写调用都认不出时返回 None。"""
    without_triple_quoted = INLINE_SCRIPT_TRIPLE_QUOTED_STRING_PATTERN.sub("\"\"", script_body_text)
    variable_literal_values: dict[str, set[str]] = collections.defaultdict(set)
    variables_with_non_literal_assignment: set[str] = set()
    for match in INLINE_SCRIPT_VARIABLE_ASSIGNMENT_PATTERN.finditer(without_triple_quoted):
        value = inline_script_path_expression_literal_value_or_none(match.group("expression"))
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
            literal_value = inline_script_path_expression_literal_value_or_none(destination_expression)
            if literal_value is not None:
                destinations.add(literal_value)
                continue
            name = destination_expression.split(".", 1)[0] if re.fullmatch(r"[A-Za-z_][\w.]*", destination_expression) else ""
            if name and variable_literal_values.get(name) and name not in variables_with_non_literal_assignment:
                destinations.update(variable_literal_values[name])
                continue
            return None
    return tuple(sorted(destinations)) if found_destination_bearing_call else None


def candidate_paths_mentioned_in_inline_script_body(
    script: InlineScriptBodyWithFileWriteCall, repository_roots: tuple[str, ...], candidate_paths: set[str],
    unique_basename_to_candidate: dict[str, str],
) -> dict[str, tuple[str, str]]:
    """返回 {候选路径: (匹配方式, 命中的字符串字面量)}。匹配方式：exact_relative_or_absolute_path / unique_basename。
    f-string / 模板字符串（``f"{ROOT}/docs/a.md"``、``${root}/x``）取最后一个插值之后的部分。"""
    # 脚本里的相对路径相对于进程 cwd：cwd 已知时只按 cwd 解析（cwd 在仓库外则相对路径不算本仓库写入），未知时按仓库根
    prefixes: list[str] = [""]
    working_directory_is_outside_repository = False
    if script.working_directory:
        working_directory_relative = repository_relative_path_of_absolute_path(repository_roots, script.working_directory)
        if working_directory_relative is None:
            working_directory_is_outside_repository = True
        elif working_directory_relative:
            prefixes = [working_directory_relative.rstrip("/") + "/"]
    mentioned: dict[str, tuple[str, str]] = {}
    literal_contents = script.write_destination_literal_contents \
        if script.write_destination_literal_contents is not None else script.unique_single_line_string_literal_contents
    for literal_content in literal_contents:
        stripped = literal_content
        if "}" in stripped:
            stripped = stripped.rsplit("}", 1)[1].lstrip("/")
            if not stripped:
                continue
        while stripped.startswith("./"):
            stripped = stripped[2:]
        if stripped.startswith("/"):
            relative = repository_relative_path_of_absolute_path(repository_roots, os.path.normpath(stripped))
            if relative is None:
                continue  # 仓库外的绝对路径（/tmp/… 报告等）不是本仓库的写入
            if relative in candidate_paths:
                mentioned[relative] = ("exact_relative_or_absolute_path", literal_content)
            continue
        if stripped.startswith("..") or working_directory_is_outside_repository:
            continue
        resolved = next((prefix + stripped for prefix in prefixes if prefix + stripped in candidate_paths), None)
        if resolved is not None:
            mentioned[resolved] = ("exact_relative_or_absolute_path", literal_content)
            continue
        candidate = unique_basename_to_candidate.get(stripped.rsplit("/", 1)[-1])
        if candidate and candidate not in mentioned:
            mentioned[candidate] = ("unique_basename", literal_content)
    return mentioned


def inline_script_body_excerpt_around_path_mention(script: InlineScriptBodyWithFileWriteCall, mentioned_token: str) -> str:
    body = script.script_body_text
    parts = [script.trigger_segment_excerpt[:80]]
    mention_position = body.find(mentioned_token)
    if mention_position >= 0:
        parts.append("【路径处】" + body[max(0, mention_position - 70):mention_position + len(mentioned_token) + 50])
    write_call = INLINE_SCRIPT_FILE_WRITE_CALL_PATTERN.search(body)
    if write_call:
        parts.append("【写调用处】" + body[max(0, write_call.start() - 70):write_call.end() + 50])
    return " … ".join(parts).replace("\n", " ⏎ ")[:420]


@dataclass
class PathWriteEvidence:
    main_session_edit_or_write_count: int = 0
    main_session_shell_write_count: int = 0
    main_session_shell_write_kinds: collections.Counter = field(default_factory=collections.Counter)
    main_session_shell_write_recoverable_by_rvf_regex_count: int = 0
    subagent_edit_or_write_count: int = 0
    subagent_shell_write_count: int = 0
    subagent_descriptions: set = field(default_factory=set)
    codex_apply_patch_count: int = 0
    codex_exec_write_count: int = 0
    shell_write_kinds_all_origins: collections.Counter = field(default_factory=collections.Counter)
    basename_only_match_count: int = 0
    precise_path_match_count: int = 0
    latest_main_session_edit_or_write_timestamp_utc: str = ""
    # 最早一次 shell 来源写入的触发命令（供抽查误判）
    first_shell_write_trigger_timestamp_utc: str = ""
    first_shell_write_trigger_kind: str = ""
    first_shell_write_trigger_command_excerpt: str = ""
    first_shell_write_trigger_writer_origin: str = ""
    first_shell_write_trigger_transcript_path: str = ""
    first_shell_write_trigger_transcript_line_number: int = 0

    def record_shell_write_trigger(self, event: SessionToolEvent, write_kind: str, excerpt: str) -> None:
        self.shell_write_kinds_all_origins[write_kind] += 1
        if self.first_shell_write_trigger_kind and event.timestamp_utc >= self.first_shell_write_trigger_timestamp_utc:
            return
        self.first_shell_write_trigger_timestamp_utc = event.timestamp_utc
        self.first_shell_write_trigger_kind = write_kind
        self.first_shell_write_trigger_command_excerpt = excerpt
        self.first_shell_write_trigger_writer_origin = event.writer_origin if event.tool_name != "codex_exec_command" \
            else "codex_exec_command"
        self.first_shell_write_trigger_transcript_path = event.source_transcript_path
        self.first_shell_write_trigger_transcript_line_number = event.line_number

    def write_origin_label(self) -> str:
        main_tool = self.main_session_edit_or_write_count > 0 or self.codex_apply_patch_count > 0
        subagent = self.subagent_edit_or_write_count > 0 or self.subagent_shell_write_count > 0
        main_shell = self.main_session_shell_write_count > 0 or self.codex_exec_write_count > 0
        if main_tool:
            return "main_session_edit_write_tool"
        if subagent and main_shell:
            return "subagent_and_main_session_shell_only"
        if subagent:
            return "subagent_only"
        if main_shell:
            return "codex_exec_shell_only" if self.codex_exec_write_count > 0 and self.main_session_shell_write_count == 0 \
                else "main_session_shell_only"
        return "no_write_found"


SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS = {
    "main_session_shell_only", "subagent_only", "subagent_and_main_session_shell_only", "codex_exec_shell_only",
}


def event_is_inside_write_origin_window(event: SessionToolEvent, window_start_utc: str, window_end_utc: str) -> bool:
    return (not window_start_utc or event.timestamp_utc >= window_start_utc) and \
        (not event.timestamp_utc or event.timestamp_utc < window_end_utc)


def collect_path_write_evidence(
    parsed_transcript: ParsedSessionTranscript,
    window_start_utc: str,
    window_end_utc: str,
    repository_roots: tuple[str, ...],
    candidate_paths: set[str],
    rvf_command_path_candidates: Any,
    rvf_command_workdir: Any,
) -> tuple[dict[str, PathWriteEvidence], dict[str, int]]:
    unique_basename_to_candidate: dict[str, str] = {}
    basename_counter = collections.Counter(path.rsplit("/", 1)[-1] for path in candidate_paths)
    for path in candidate_paths:
        basename = path.rsplit("/", 1)[-1]
        if basename_counter[basename] == 1 and len(basename) >= MINIMUM_UNIQUE_BASENAME_LENGTH_FOR_BASENAME_MATCH:
            unique_basename_to_candidate[basename] = path
    evidence: dict[str, PathWriteEvidence] = collections.defaultdict(PathWriteEvidence)
    window_tool_counts: collections.Counter = collections.Counter()
    for event in [*parsed_transcript.main_session_events, *parsed_transcript.subagent_events]:
        if not event_is_inside_write_origin_window(event, window_start_utc, window_end_utc):
            continue
        is_subagent = event.writer_origin == "subagent"
        prefix = "subagent" if is_subagent else "main_session"
        window_tool_counts[f"{prefix}:{event.tool_name}"] += 1
        if event.tool_name == "Agent" or event.codex_exec_input_unparseable:
            continue
        if event.file_path is not None:
            relative = repository_relative_path_or_none(repository_roots, event.file_path, event.shell_working_directory)
            if relative in candidate_paths:
                if is_subagent:
                    evidence[relative].subagent_edit_or_write_count += 1
                    if event.subagent_description:
                        evidence[relative].subagent_descriptions.add(event.subagent_description)
                else:
                    evidence[relative].main_session_edit_or_write_count += 1
                    evidence[relative].latest_main_session_edit_or_write_timestamp_utc = max(
                        evidence[relative].latest_main_session_edit_or_write_timestamp_utc, event.timestamp_utc)
            continue
        if event.codex_patch_paths:
            for patch_path in event.codex_patch_paths:
                relative = repository_relative_path_or_none(repository_roots, patch_path, event.shell_working_directory)
                if relative in candidate_paths:
                    evidence[relative].codex_apply_patch_count += 1
            continue
        if not event.shell_command:
            continue
        analysis: ShellCommandWriteAnalysis | None = event.cached_shell_command_write_analysis
        if analysis is None:
            analysis = analyze_shell_command_write_targets(event.shell_command, event.shell_working_directory)
            event.cached_shell_command_write_analysis = analysis
        if analysis.shell_parse_failed:
            window_tool_counts[f"{prefix}:shell_command_parse_failed"] += 1
        if analysis.bulk_or_unresolvable_target_write_segment_count:
            window_tool_counts[f"{prefix}:shell_bulk_or_unresolvable_target_write_command"] += 1
        # {候选路径: (匹配方式, 写入种类, 触发摘录)}；精确目标在前，内联脚本启发式在后
        written_paths: dict[str, tuple[str, str, str]] = {}
        for target in analysis.write_target_tokens:
            for path, match_kind in resolve_shell_write_target_token_to_candidate_paths(
                    target, repository_roots, candidate_paths, unique_basename_to_candidate):
                written_paths.setdefault(path, (match_kind, target.write_kind, target.trigger_segment_excerpt))
        for script in analysis.inline_script_bodies_with_file_write_call:
            for path, (match_kind, token) in candidate_paths_mentioned_in_inline_script_body(
                    script, repository_roots, candidate_paths, unique_basename_to_candidate).items():
                if path in written_paths:
                    continue
                write_kind = INLINE_SCRIPT_LITERAL_DESTINATION_WRITE_KIND \
                    if script.write_destination_literal_contents is not None else INLINE_SCRIPT_WRITE_KIND
                written_paths[path] = (match_kind, write_kind, inline_script_body_excerpt_around_path_mention(script, token))
        if not written_paths:
            continue
        window_tool_counts[f"{prefix}:shell_command_with_resolved_write_target"] += 1
        regex_recoverable: set[str] = set()
        if rvf_command_path_candidates is not None and not is_subagent:
            try:
                workdir = rvf_command_workdir(Path(repository_roots[0]), event.shell_working_directory)
                regex_recoverable = {item["path"] for item in rvf_command_path_candidates(
                    Path(repository_roots[0]), event.shell_command, workdir)}
            except Exception:
                regex_recoverable = set()
        for path, (match_kind, write_kind, excerpt) in written_paths.items():
            path_evidence = evidence[path]
            if match_kind == "unique_basename":
                path_evidence.basename_only_match_count += 1
            else:
                path_evidence.precise_path_match_count += 1
            path_evidence.record_shell_write_trigger(event, write_kind, excerpt)
            if is_subagent:
                path_evidence.subagent_shell_write_count += 1
                if event.subagent_description:
                    path_evidence.subagent_descriptions.add(event.subagent_description)
            elif event.tool_name == "codex_exec_command":
                path_evidence.codex_exec_write_count += 1
            else:
                path_evidence.main_session_shell_write_count += 1
                path_evidence.main_session_shell_write_kinds[write_kind] += 1
                if path in regex_recoverable:
                    path_evidence.main_session_shell_write_recoverable_by_rvf_regex_count += 1
    return evidence, dict(window_tool_counts)


# --------------------------------------------------------------------------------------------
# run 工件读取
# --------------------------------------------------------------------------------------------


def load_first_existing_json(artifacts_directory: Path, relative_candidates: Iterable[str]) -> Any:
    for relative in relative_candidates:
        value = read_json_file_or_none(artifacts_directory / relative)
        if value is not None:
            return value
    return None


def per_file_changed_line_counts_from_bootstrap_patch(patch_path: Path) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    current: str | None = None
    try:
        with open(patch_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("diff --git "):
                    match = re.match(r"diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
                    current = match.group(2) if match else None
                    if current is not None:
                        counts.setdefault(current, {"added_lines": 0, "deleted_lines": 0})
                elif current is None or line.startswith("+++") or line.startswith("---"):
                    continue
                elif line.startswith("+"):
                    counts[current]["added_lines"] += 1
                elif line.startswith("-"):
                    counts[current]["deleted_lines"] += 1
    except FileNotFoundError:
        pass
    return counts


def untracked_file_line_counts_from_bootstrap(bootstrap: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(bootstrap, dict):
        return counts
    for item in bootstrap.get("untracked_files") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        stored = item.get("stored_path")
        line_count = 0
        if isinstance(stored, str):
            try:
                with open(stored, "rb") as handle:
                    line_count = sum(1 for _ in handle)
            except Exception:
                line_count = 0
        counts[str(item["path"])] = line_count
    return counts


def detect_coverage_gap_mentions(text: str) -> dict[str, Any]:
    """按段落找「有文件没被审」的说法。返回 level：
    - ``names_attribution_blindness_as_cause``：说了没审，并点出归属对 Bash / 子代理失明；
    - ``says_some_files_not_reviewed``：说了某些文件没审 / 不在范围，没点原因；
    - ``none``。
    snippets 里 ``【…】`` 标出命中词，供人工抽查。"""
    level = "none"
    snippets: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        misjudgment = ATTRIBUTION_MISJUDGMENT_STATEMENT_PATTERN.search(paragraph)
        if misjudgment and ATTRIBUTION_MISJUDGMENT_CAUSE_PATTERN.search(paragraph):
            level = "names_attribution_blindness_as_cause"
            if len(snippets) < 3:
                start = max(0, misjudgment.start() - 60)
                snippets.append((paragraph[start:misjudgment.start()] + "【" + misjudgment.group(0) + "】" +
                                 paragraph[misjudgment.end():misjudgment.end() + 90]).replace("\n", " ⏎ "))
            continue
        for match in COVERAGE_GAP_STATEMENT_PATTERN.finditer(paragraph):
            nearby = paragraph[max(0, match.start() - 8):match.end() + 6]
            if COVERAGE_GAP_FALSE_POSITIVE_NEARBY_PATTERN.search(nearby):
                continue
            if not COVERAGE_GAP_FILE_SCOPE_CONTEXT_PATTERN.search(paragraph):
                continue
            paragraph_level = "names_attribution_blindness_as_cause" if ATTRIBUTION_BLINDNESS_CAUSE_PATTERN.search(paragraph) \
                else "says_some_files_not_reviewed"
            if paragraph_level == "names_attribution_blindness_as_cause" or level == "none":
                level = paragraph_level
            if len(snippets) < 3:
                start = max(0, match.start() - 60)
                snippets.append((paragraph[start:match.start()] + "【" + match.group(0) + "】" +
                                 paragraph[match.end():match.end() + 90]).replace("\n", " ⏎ "))
            break
    return {"level": level, "snippets": snippets}


def collect_reviewer_issue_paths_and_audit_text(artifacts_directory: Path) -> tuple[set[str], str]:
    issue_path_strings: set[str] = set()
    audit_texts: list[str] = []
    for result_path in glob.glob(str(artifacts_directory / "reviewers" / "*" / "review-result.json")) + \
            [str(artifacts_directory / "review-result.json")]:
        result = read_json_file_or_none(Path(result_path))
        if not isinstance(result, dict):
            continue
        if isinstance(result.get("audit_summary"), str):
            audit_texts.append(result["audit_summary"])
        for issue in result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            for key in ("path", "paths", "file", "files", "locations", "location"):
                value = issue.get(key)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, dict):
                        item = item.get("path") or item.get("file")
                    if isinstance(item, str) and item:
                        issue_path_strings.add(item.split(":", 1)[0].strip())
    return issue_path_strings, "\n".join(audit_texts)


def load_agentic_scope_adjustments_by_run_id(scope_adjustments_directory: Path) -> dict[str, dict[str, Any]]:
    by_run_id: dict[str, dict[str, Any]] = {}
    for record_path in glob.glob(str(scope_adjustments_directory / "*.json")):
        record = read_json_file_or_none(Path(record_path))
        if isinstance(record, dict) and record.get("run_id"):
            by_run_id[str(record["run_id"])] = record
    return by_run_id


def scan_scope_exception_issue_ledger(scope_issue_ledger_directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for record_path in sorted(glob.glob(str(scope_issue_ledger_directory / "*.json"))):
        record = read_json_file_or_none(Path(record_path))
        if isinstance(record, dict):
            entries.append({key: record.get(key) for key in (
                "operation_type", "distinct_path_count", "first_observed_at", "worktree", "note")})
    entries.sort(key=lambda item: str(item.get("first_observed_at")))
    return entries


def claimed_reviewed_file_counts(text: str) -> list[int]:
    numbers: list[int] = []
    for match in CLAIMED_REVIEWED_FILE_COUNT_PATTERN.finditer(text):
        for group in ("a", "b", "c", "d"):
            if match.group(group):
                numbers.append(int(match.group(group)))
    return numbers


def summarize_fix_attempt_elevations(artifacts_directory: Path) -> dict[str, Any]:
    elevated_attempts: list[dict[str, Any]] = []
    attempt_count = 0
    for result_path in sorted(glob.glob(str(artifacts_directory / "fix-attempts" / "*" / "result.json"))):
        attempt_count += 1
        result = read_json_file_or_none(Path(result_path)) or {}
        if result.get("status") != "elevated":
            continue
        detail_text = json.dumps(result.get("result_payload"), ensure_ascii=False) + read_text_file_or_empty(
            Path(result_path).parent / "elevation-detail.json")
        elevated_attempts.append({
            "issue_id": result.get("issue_id"),
            "caused_by_protected_or_out_of_allowlist_target": bool(ELEVATION_CAUSED_BY_PROTECTED_SCOPE_PATTERN.search(detail_text)),
            "detail_excerpt": detail_text[:300],
        })
    return {"fix_attempt_count": attempt_count, "elevated_attempts": elevated_attempts}


# --------------------------------------------------------------------------------------------
# git 后续修复信号（只读 git log）
# --------------------------------------------------------------------------------------------


def resolve_git_directory_for_history_query(repository: str) -> str | None:
    candidates = [repository]
    kanban_match = KANBAN_TASK_WORKTREE_PATTERN.search(repository + "/")
    if kanban_match:
        candidates.append(str(CANONICAL_GITHUB_CHECKOUTS_DIRECTORY / Path(repository).name))
    for candidate in candidates:
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, ".git")):
            return candidate
    return None


def later_commits_touching_paths(git_directory: str, since_utc: str, paths: list[str]) -> dict[str, list[dict[str, str]]]:
    if not paths:
        return {}
    touched: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for chunk_start in range(0, len(paths), 150):
        chunk = paths[chunk_start:chunk_start + 150]
        try:
            output = subprocess.run(
                ["git", "-C", git_directory, "log", "--exclude=refs/stash", "--all", f"--since={since_utc}Z",
                 "--format=%x1e%H%x09%cI%x09%s", "--name-only", "--", *chunk],
                capture_output=True, text=True, timeout=60, check=False,
            ).stdout
        except Exception:
            continue
        for block in output.split("\x1e"):
            lines = [line for line in block.strip().splitlines() if line.strip()]
            if not lines:
                continue
            header = lines[0].split("\t", 2)
            if len(header) < 3:
                continue
            for name in lines[1:]:
                if name in chunk:
                    touched[name].append({"sha": header[0][:10], "committed_at": header[1], "subject": header[2][:140]})
    for name in touched:
        touched[name].sort(key=lambda item: item["committed_at"])
    return touched


# --------------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------------


def enumerate_all_runs_with_scope_contract(run_logs_runs_directory: Path) -> list[dict[str, Any]]:
    """列出全部带 scope contract 的 run（任意 scope_mode）。不在这里按 --only-run-id 过滤：窗口起点、
    下一轮上界与「后续是否覆盖」索引都要用完整的合同历史建立，否则单 run 复查与全量口径不一致。"""
    runs: list[dict[str, Any]] = []
    for contract_path in sorted(run_logs_runs_directory.glob("*/artifacts/inputs/scope.contract.json")):
        run_directory = contract_path.parents[2]
        run_id = run_directory.name
        contract = read_json_file_or_none(contract_path)
        if not isinstance(contract, dict):
            continue
        artifacts_directory = run_directory / "artifacts"
        manifest = load_first_existing_json(artifacts_directory, ("inputs/session-manifest.json", "session-manifest.json")) or {}
        runs.append({
            "run_id": run_id,
            "run_directory": str(run_directory),
            "artifacts_directory": artifacts_directory,
            "contract": contract,
            "manifest": manifest if isinstance(manifest, dict) else {},
            "transcript_path": (manifest.get("transcript") if isinstance(manifest, dict) else None) or "",
            "run_started_at_utc": run_started_at_utc_from_run_id(run_id),
        })
    return runs


def analyze_runs(arguments: argparse.Namespace) -> dict[str, Any]:
    rvf_command_path_candidates, rvf_command_workdir = load_rvf_shell_command_path_candidate_functions()
    only_run_ids = set(arguments.only_run_id or [])
    all_runs_with_contract = enumerate_all_runs_with_scope_contract(Path(arguments.run_logs_runs_directory))
    scope_mode_counter = collections.Counter(run["contract"].get("scope_mode") for run in all_runs_with_contract)

    # 窗口起点：同一 transcript 上一个带 contract 的 run（任意 scope_mode）的开始时刻；
    # 下一轮上界：同一 transcript 下一个带 contract 的 run 的开始时刻（回执按时间绑定时的上界）。
    runs_by_transcript: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for run in all_runs_with_contract:
        runs_by_transcript[run["transcript_path"]].append(run)
    for transcript_runs in runs_by_transcript.values():
        transcript_runs.sort(key=lambda item: item["run_started_at_utc"])
        for position, run in enumerate(transcript_runs):
            has_transcript = bool(run["transcript_path"])
            run["window_start_utc"] = transcript_runs[position - 1]["run_started_at_utc"] if has_transcript and position > 0 else ""
            run["next_run_started_at_utc_on_same_transcript"] = \
                transcript_runs[position + 1]["run_started_at_utc"] if has_transcript and position + 1 < len(transcript_runs) else ""

    all_session_owned_runs = [run for run in all_runs_with_contract if run["contract"].get("scope_mode") == "session-owned"]
    # 「漏审后来是否被补上」索引：用全部 session-owned run 建立，与 --only-run-id 无关
    covered_later_index: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for run in all_session_owned_runs:
        tracker_scope = load_first_existing_json(run["artifacts_directory"], ("inputs/tracker-scope.json", "tracker-scope.json"))
        covered = set(tracker_scope.get("paths") or []) if isinstance(tracker_scope, dict) else set(run["contract"].get("fix_allowlist") or [])
        for path in covered:
            covered_later_index[(str(run["contract"].get("repo") or ""), path)].append(run["run_started_at_utc"])

    session_owned_runs = [run for run in all_session_owned_runs if not only_run_ids or run["run_id"] in only_run_ids]
    unknown_only_run_ids = only_run_ids - {run["run_id"] for run in all_session_owned_runs}
    if unknown_only_run_ids:
        print(f"[warn] --only-run-id 里这些 run 不是带合同的 session-owned run：{sorted(unknown_only_run_ids)}", file=sys.stderr)
    session_owned_runs.sort(key=lambda item: (item["transcript_path"], item["run_started_at_utc"]))

    scope_adjustments_by_run_id = load_agentic_scope_adjustments_by_run_id(Path(arguments.agentic_scope_adjustments_directory))
    run_rows: list[dict[str, Any]] = []
    missed_path_rows: list[dict[str, Any]] = []
    parsed_transcript_cache: dict[str, ParsedSessionTranscript] = {}
    current_transcript_key = None
    for run in session_owned_runs:
        transcript_path = run["transcript_path"]
        if transcript_path != current_transcript_key:
            parsed_transcript_cache.clear()  # 按 transcript 分组处理，只缓存当前一个，控制内存
            current_transcript_key = transcript_path
        run_rows_entry, run_missed_rows = analyze_single_run(
            run, parsed_transcript_cache, rvf_command_path_candidates, rvf_command_workdir, arguments, scope_adjustments_by_run_id)
        run_rows.append(run_rows_entry)
        missed_path_rows.extend(run_missed_rows)

    run_rows.sort(key=lambda item: item["run_started_at_utc"])
    # 同一 (仓库, 路径) 在后续 run 里有没有进过正式覆盖（漏审后来是否被补上）；索引已用全部合同历史建立
    for item in missed_path_rows:
        later = [started for started in covered_later_index.get((item["repository"], item["path"]), []) if started > item["run_started_at_utc"]]
        item["covered_in_a_later_session_owned_run"] = bool(later)
    agentic_manual_all_uncommitted_fallback_runs = []
    for run in all_runs_with_contract:
        run_id_match = RUN_ID_PATTERN.match(run["run_id"])
        if not run_id_match or run_id_match.group("trigger") != "agentic-initiation-start" or \
                run["contract"].get("scope_mode") != "manual-all-uncommitted" or \
                run["run_started_at_utc"] < AGENTIC_INITIATION_TRACKER_SCOPE_RESTORED_AT_UTC:
            continue
        scope_of_work_text = read_text_file_or_empty(run["artifacts_directory"] / "inputs" / "scope-of-work.md") or \
            read_text_file_or_empty(run["artifacts_directory"] / "scope-of-work.md")
        matched_reasons = [name for name, pattern in AGENTIC_MANUAL_ALL_UNCOMMITTED_FALLBACK_REASON_PATTERNS
                           if pattern.search(scope_of_work_text)]
        agentic_manual_all_uncommitted_fallback_runs.append({
            "run_id": run["run_id"], "run_month": run["run_started_at_utc"][:7],
            "primary_reason_recorded_in_scope_of_work": matched_reasons[0] if matched_reasons else "no_reason_recorded",
            "all_reasons_recorded_in_scope_of_work": matched_reasons})
    declined_reasons = scan_declined_decision_reason_ledger(Path(arguments.declined_decision_reasons_ledger_path))
    scope_exception_issue_ledger_entries = scan_scope_exception_issue_ledger(Path(arguments.agentic_scope_issue_ledger_directory))
    return {
        "generated_at_utc": datetime_module.datetime.now(datetime_module.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_logs_runs_directory": str(arguments.run_logs_runs_directory),
        "scope_mode_counts_among_runs_with_scope_contract": dict(scope_mode_counter),
        "run_with_scope_contract_count": len(all_runs_with_contract),
        "run_directory_count": sum(1 for path in Path(arguments.run_logs_runs_directory).iterdir() if path.is_dir()),
        "agentic_initiation_runs_falling_back_to_manual_all_uncommitted_after_tracker_scope_restored":
            agentic_manual_all_uncommitted_fallback_runs,
        "only_run_id_filter_applied_after_window_and_coverage_index": sorted(only_run_ids),
        "rvf_regex_reuse_available": rvf_command_path_candidates is not None,
        "run_rows": run_rows,
        "missed_path_rows": missed_path_rows,
        "declined_decision_reasons_mentioning_attribution_or_coverage": declined_reasons,
        "scope_exception_issue_ledger_entries": scope_exception_issue_ledger_entries,
    }


def write_origin_not_analyzable_reason_or_none(
    parsed: ParsedSessionTranscript | None, host_kind: str, window_start_utc: str, window_end_utc: str
) -> str | None:
    """写入来源不可判定的原因；可判定时返回 None。不可判定的 run 不进入任何漏审统计（而不是被当成 0 写入）。"""
    if parsed is None:
        return "transcript_missing"
    if host_kind not in ("claude_code", "codex"):
        return f"host_not_supported:{host_kind}"
    if host_kind == "codex":
        if not parsed.main_session_events:
            # 整份 rollout 一个能识别的工具调用都没有：多半是 schema 又漂移了
            return "codex_transcript_without_any_recognized_tool_call"
        if any(event.codex_exec_input_unparseable and event_is_inside_write_origin_window(event, window_start_utc, window_end_utc)
               for event in parsed.main_session_events):
            return "codex_exec_input_unparseable_in_window"
    return None


def rvf_receipts_bound_to_run(
    receipts: list[dict[str, Any]], run_id: str, run_started_at_utc: str, next_run_started_at_utc_on_same_transcript: str
) -> tuple[list[dict[str, Any]], str]:
    """把同一 transcript 里的 rvf start / dispatch 回执绑定到具体 run。
    1. 回执文本里出现本 run 的 run id（``run_id: …`` 或 run_dir 路径）→ 绑定；
    2. 回执文本里没有任何 run id 时，才按时间绑定：回执结果时刻落在「本 run 开始 → 同 transcript 下一个带合同 run
       开始」之间；
    3. 否则视为本 run 没有回执（不借用别的轮次的回执）。"""
    bound_by_run_id = [item for item in receipts if run_id in item.get("receipt_mentioned_run_ids", [])]
    if bound_by_run_id:
        return bound_by_run_id, "run_id_mentioned_in_receipt_text"
    bound_by_time_interval = [
        item for item in receipts
        if not item.get("receipt_mentioned_run_ids") and item.get("result_timestamp_utc", "") >= run_started_at_utc
        and (not next_run_started_at_utc_on_same_transcript or item.get("result_timestamp_utc", "") < next_run_started_at_utc_on_same_transcript)]
    if bound_by_time_interval:
        return bound_by_time_interval, "receipt_without_run_id_inside_interval_until_next_run_on_same_transcript"
    return [], "no_receipt_bound_to_this_run"


def analyze_single_run(
    run: dict[str, Any],
    parsed_transcript_cache: dict[str, ParsedSessionTranscript],
    rvf_command_path_candidates: Any,
    rvf_command_workdir: Any,
    arguments: argparse.Namespace,
    scope_adjustments_by_run_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contract = run["contract"]
    manifest = run["manifest"]
    artifacts_directory: Path = run["artifacts_directory"]
    run_id = run["run_id"]
    run_id_match = RUN_ID_PATTERN.match(run_id)
    trigger_kind = run_id_match.group("trigger") if run_id_match else "unknown"
    repository = str(contract.get("repo") or manifest.get("repo") or "")
    repository_roots = tuple(dict.fromkeys(root for root in (repository, os.path.realpath(repository) if repository else "",
                                                             repository.replace("/private/tmp/", "/tmp/"),
                                                             repository.replace("/tmp/", "/private/tmp/", 1)) if root))
    kanban_match = KANBAN_TASK_WORKTREE_PATTERN.search(repository + "/")

    main_harness = read_json_file_or_none(artifacts_directory / "inputs" / "main-harness.json") or {}
    summary = read_json_file_or_none(Path(run["run_directory"]) / "summary.json") or {}
    transcript_path = run["transcript_path"]
    host_kind = (main_harness.get("main_harness") if isinstance(main_harness, dict) else None) or \
        infer_host_kind_from_transcript_path(transcript_path)
    path_host_kind = infer_host_kind_from_transcript_path(transcript_path)
    if path_host_kind in ("claude_code", "codex", "kimi", "cursor"):
        host_kind = path_host_kind  # transcript 路径是硬证据；main-harness.json 曾被检测退化成 codex 默认值

    tracker_scope = load_first_existing_json(artifacts_directory, ("inputs/tracker-scope.json", "tracker-scope.json"))
    tracker_scope_paths: set[str] | None = None
    if isinstance(tracker_scope, dict):
        tracker_scope_paths = set(tracker_scope.get("paths") or [])
        tracker_scope_paths |= {hunk.get("path") for hunk in tracker_scope.get("hunks") or [] if isinstance(hunk, dict) and hunk.get("path")}
    primary_files = set(contract.get("primary_files") or [])
    protected_files = set(contract.get("protected_files") or [])
    background_files = set(contract.get("background_files") or [])
    fix_allowlist = set(contract.get("fix_allowlist") or [])
    covered_paths = tracker_scope_paths if tracker_scope_paths is not None else set(fix_allowlist)
    candidate_paths = primary_files | protected_files | background_files | covered_paths
    uncovered_paths = candidate_paths - covered_paths

    row: dict[str, Any] = {
        "run_id": run_id,
        "run_started_at_utc": run["run_started_at_utc"],
        "run_month": run["run_started_at_utc"][:7],
        "trigger_kind": trigger_kind,
        "host_kind": host_kind,
        "rvf_plugin_deploy": summary.get("rvf_plugin_deploy") or "unknown",
        "repository": repository,
        "kanban_task_id": kanban_match.group("task_id") if kanban_match else None,
        "transcript_path": transcript_path,
        "transcript_available": bool(transcript_path and os.path.exists(transcript_path)),
        "window_start_utc": run.get("window_start_utc", ""),
        "next_run_started_at_utc_on_same_transcript": run.get("next_run_started_at_utc_on_same_transcript", ""),
        "tracker_scope_present": tracker_scope_paths is not None,
        "candidate_path_count": len(candidate_paths),
        "primary_files_count": len(primary_files),
        "primary_units_count": len(contract.get("primary_units") or []),
        "covered_path_count": len(covered_paths),
        "fix_allowlist_count": len(fix_allowlist),
        "protected_files_count": len(protected_files),
        "background_files_count": len(background_files),
        "uncovered_path_count": len(uncovered_paths),
        "primary_files_overlapping_protected_count": len(primary_files & protected_files),
        "manifest_owned_dirty_path_count": len(manifest.get("owned_dirty_paths") or []),
        "manifest_unattributed_dirty_path_count": len(manifest.get("unattributed_dirty_paths") or []),
        "manifest_claude_write_event_count": len(manifest.get("claude_write_events") or []),
        "manifest_command_path_candidate_count": len(manifest.get("command_path_candidates") or []),
    }

    # 写入来源
    parsed: ParsedSessionTranscript | None = None
    if row["transcript_available"]:
        parsed = parsed_transcript_cache.get(transcript_path)
        if parsed is None:
            parsed = parse_codex_rollout(Path(transcript_path)) if host_kind == "codex" else \
                parse_claude_session_with_subagents(Path(transcript_path))
            parsed_transcript_cache[transcript_path] = parsed
    row["write_origin_not_analyzable_reason"] = write_origin_not_analyzable_reason_or_none(
        parsed, host_kind, run.get("window_start_utc", ""), run["run_started_at_utc"])
    row["write_origin_analyzable"] = row["write_origin_not_analyzable_reason"] is None
    if parsed is not None and host_kind == "codex":
        row["codex_exec_input_unparseable_event_count_in_window"] = sum(
            1 for event in parsed.main_session_events if event.codex_exec_input_unparseable
            and event_is_inside_write_origin_window(event, run.get("window_start_utc", ""), run["run_started_at_utc"]))
    origin_counts_uncovered: collections.Counter = collections.Counter()
    origin_counts_covered: collections.Counter = collections.Counter()
    counterexample_a_explanations: collections.Counter = collections.Counter()
    counterexample_b_explanations: collections.Counter = collections.Counter()
    missed_rows: list[dict[str, Any]] = []
    if row["write_origin_analyzable"]:
        assert parsed is not None
        row["subagent_transcript_count"] = parsed.subagent_transcript_count
        window_evidence, window_tool_counts = collect_path_write_evidence(
            parsed, run.get("window_start_utc", ""), run["run_started_at_utc"], repository_roots, candidate_paths,
            rvf_command_path_candidates, rvf_command_workdir)
        whole_session_evidence, _ = collect_path_write_evidence(
            parsed, "", run["run_started_at_utc"], repository_roots, candidate_paths, None, None)
        row["window_tool_counts"] = window_tool_counts
        tracked_line_counts = per_file_changed_line_counts_from_bootstrap_patch(artifacts_directory / "worktree-bootstrap.patch")
        bootstrap = read_json_file_or_none(artifacts_directory / "worktree-bootstrap.json")
        untracked_line_counts = untracked_file_line_counts_from_bootstrap(bootstrap)
        ownership_baseline = manifest.get("ownership_baseline") if isinstance(manifest.get("ownership_baseline"), dict) else {}
        head_commit_time_cutoff_utc = normalize_timestamp_to_utc_second_precision(ownership_baseline.get("head_committed_at")) \
            if ownership_baseline.get("mode") == "head_commit_time" else ""
        row["manifest_head_commit_time_cutoff_utc"] = head_commit_time_cutoff_utc
        committed_round_paths = set(manifest.get("owned_committed_round_paths") or [])
        prior_scope_union_present = bool(isinstance(tracker_scope, dict) and tracker_scope.get("unioned_prior_scope_unit_ids"))
        cross_session_conflict_paths = {
            item.get("path") for item in (read_json_file_or_none(artifacts_directory / "inputs" / "review-packet.metadata.json") or {}
                                          ).get("cross_session_conflicts") or [] if isinstance(item, dict)}
        for path in sorted(candidate_paths):
            evidence = window_evidence.get(path, PathWriteEvidence())
            label = evidence.write_origin_label()
            whole_label = whole_session_evidence.get(path, PathWriteEvidence()).write_origin_label()
            if label == "no_write_found":
                label = "written_earlier_in_session_before_window" if whole_label != "no_write_found" else "not_written_by_this_session"
            if path in covered_paths:
                origin_counts_covered[label] += 1
                if label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS:
                    # 反例 A：只被 Bash / 子代理写过却进了覆盖——拆成因
                    if whole_label == "main_session_edit_write_tool" or whole_session_evidence.get(
                            path, PathWriteEvidence()).main_session_edit_or_write_count > 0:
                        explanation = "same_path_edit_or_write_earlier_in_session"
                    elif path in committed_round_paths:
                        explanation = "committed_round_default_attribution"
                    elif prior_scope_union_present:
                        explanation = "prior_scope_units_unioned_back"
                    elif not row["tracker_scope_present"]:
                        explanation = "no_tracker_scope_fix_allowlist_from_manifest"
                    else:
                        explanation = "unexplained"
                    counterexample_a_explanations[explanation] += 1
                continue
            if label == "main_session_edit_write_tool":
                # 反例 B：主会话 Edit/Write 写过却没进覆盖——拆成因
                if path in cross_session_conflict_paths:
                    explanation = "claimed_by_another_session_cross_session_conflict"
                elif head_commit_time_cutoff_utc and evidence.latest_main_session_edit_or_write_timestamp_utc and \
                        evidence.latest_main_session_edit_or_write_timestamp_utc < head_commit_time_cutoff_utc:
                    # manifest 以 HEAD 提交时刻截断：HEAD 若是之后 fast-forward 进来的 base 提交，
                    # 本会话更早的、从未提交的 Edit/Write 会被整段忽略。
                    explanation = "edit_write_before_head_commit_time_cutoff"
                elif not row["tracker_scope_present"]:
                    explanation = "no_tracker_scope"
                else:
                    explanation = "unexplained"
                counterexample_b_explanations[explanation] += 1
                if explanation == "edit_write_before_head_commit_time_cutoff":
                    label = "main_session_edit_write_before_head_commit_time_cutoff"
            origin_counts_uncovered[label] += 1
            if label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS or label == "main_session_edit_write_before_head_commit_time_cutoff":
                is_untracked = path in untracked_line_counts
                # 漏审证据只来自 shell 命令解析（没有任何子代理 Edit/Write 这类精确工具证据）：这些行需要抽查误判率
                shell_derived_only = label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS and \
                    evidence.subagent_edit_or_write_count == 0
                missed_rows.append({
                    "missed_cause": "invisible_writer_bash_or_subagent" if label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS
                    else "edit_write_before_head_commit_time_cutoff",
                    "run_id": run_id,
                    "run_started_at_utc": run["run_started_at_utc"],
                    "host_kind": host_kind,
                    "trigger_kind": trigger_kind,
                    "kanban_task_id": row["kanban_task_id"],
                    "repository": repository,
                    "path": path,
                    "write_origin": label,
                    "main_session_shell_write_kinds": dict(evidence.main_session_shell_write_kinds),
                    "recoverable_by_rvf_existing_shell_regex": evidence.main_session_shell_write_recoverable_by_rvf_regex_count > 0,
                    "matched_only_by_unique_basename": evidence.basename_only_match_count > 0 and
                        evidence.precise_path_match_count == 0 and evidence.subagent_edit_or_write_count == 0,
                    "write_evidence_derived_only_from_shell_command_parsing": shell_derived_only,
                    "shell_write_kinds_all_origins": dict(evidence.shell_write_kinds_all_origins),
                    "shell_write_trigger_kind": evidence.first_shell_write_trigger_kind or None,
                    "shell_write_trigger_writer_origin": evidence.first_shell_write_trigger_writer_origin or None,
                    "shell_write_trigger_command_excerpt": evidence.first_shell_write_trigger_command_excerpt or None,
                    "shell_write_trigger_transcript_path": evidence.first_shell_write_trigger_transcript_path or None,
                    "shell_write_trigger_transcript_line_number": evidence.first_shell_write_trigger_transcript_line_number or None,
                    "subagent_descriptions": sorted(evidence.subagent_descriptions)[:3],
                    "is_untracked_new_file": is_untracked,
                    "changed_line_count": untracked_line_counts.get(path, 0) if is_untracked else
                        sum(tracked_line_counts.get(path, {}).values()),
                })
    row["uncovered_path_write_origin_counts"] = dict(origin_counts_uncovered)
    row["covered_path_write_origin_counts"] = dict(origin_counts_covered)
    row["counterexample_invisible_origin_but_covered_explanations"] = dict(counterexample_a_explanations)
    row["counterexample_edit_write_origin_but_uncovered_explanations"] = dict(counterexample_b_explanations)
    row["is_rvf_self_repository"] = Path(repository).name == "review-validate-fix"
    row["missed_session_written_path_count"] = len(missed_rows)
    row["missed_path_count_by_cause"] = dict(collections.Counter(item["missed_cause"] for item in missed_rows))
    row["missed_session_written_changed_line_count"] = sum(item["changed_line_count"] for item in missed_rows)
    row["missed_path_count_recoverable_by_rvf_existing_shell_regex"] = sum(
        1 for item in missed_rows if item["recoverable_by_rvf_existing_shell_regex"])
    row["session_written_candidate_path_count"] = sum(
        count for label, count in (origin_counts_covered + origin_counts_uncovered).items()
        if label not in ("not_written_by_this_session", "written_earlier_in_session_before_window"))

    # 回执 / 最终回复 / handoff
    if parsed is not None:
        start_receipts, row["start_receipt_binding_method"] = rvf_receipts_bound_to_run(
            [item for item in parsed.rvf_start_or_dispatch_receipts if "start_receipt_auto_scope_path_count" in item],
            run_id, run["run_started_at_utc"], run.get("next_run_started_at_utc_on_same_transcript", ""))
        if start_receipts:
            row["start_receipt_auto_scope_path_count"] = start_receipts[0]["start_receipt_auto_scope_path_count"]
            row["start_receipt_reviewable_unit_count"] = start_receipts[0].get("start_receipt_reviewable_unit_count")
        dispatch_receipts, row["dispatch_receipt_binding_method"] = rvf_receipts_bound_to_run(
            [item for item in parsed.rvf_start_or_dispatch_receipts if "dispatch_receipt_primary_files_count" in item],
            run_id, run["run_started_at_utc"], run.get("next_run_started_at_utc_on_same_transcript", ""))
        if dispatch_receipts:
            row["dispatch_receipt_primary_files_count"] = dispatch_receipts[0]["dispatch_receipt_primary_files_count"]
            row["dispatch_receipt_transcript_line_number"] = dispatch_receipts[0].get("result_line_number")
        replies = [item for item in parsed.final_replies_with_rvf_handoff_file if run_id in item["text"]]
        if replies:
            final_reply_text = replies[-1]["text"]
            # 两行摘要 = RVF_HANDOFF_FILE 行之后、第一个空段之后的那一段（模板固定结构），这里取前 5 行
            head_lines = "\n".join(line for line in final_reply_text.splitlines()[:5] if line.strip())
            row["final_reply_found"] = True
            row["final_reply_two_line_summary_gap_mention"] = detect_coverage_gap_mentions(head_lines)
            row["final_reply_anywhere_gap_mention"] = detect_coverage_gap_mentions(final_reply_text)
            row["final_reply_claimed_reviewed_file_counts"] = claimed_reviewed_file_counts(final_reply_text)
        else:
            row["final_reply_found"] = False
    handoff_text = read_text_file_or_empty(artifacts_directory / "handoff.md")
    row["handoff_found"] = bool(handoff_text)
    row["handoff_gap_mention"] = detect_coverage_gap_mentions(handoff_text) if handoff_text else {"level": "none", "snippets": []}
    row["handoff_claimed_reviewed_file_counts"] = claimed_reviewed_file_counts(handoff_text) if handoff_text else []

    # reviewer 有没有在漏审文件上提 issue / 在审计摘要里提到它们（背景文件 reviewer 能读到，只是不被要求主动审）
    reviewer_issue_paths, reviewer_audit_text = collect_reviewer_issue_paths_and_audit_text(artifacts_directory)
    for item in missed_rows:
        basename = item["path"].rsplit("/", 1)[-1]
        item["reviewer_raised_issue_on_this_path"] = any(
            value == item["path"] or value.endswith("/" + item["path"]) or value == basename for value in reviewer_issue_paths)
        item["reviewer_audit_summary_mentions_basename"] = basename in reviewer_audit_text
    row["missed_paths_with_reviewer_issue_count"] = sum(1 for item in missed_rows if item["reviewer_raised_issue_on_this_path"])
    # 基线：正式覆盖路径里被 reviewer 提了 issue 的比例
    row["covered_paths_with_reviewer_issue_count"] = sum(
        1 for path in covered_paths if any(value == path or value.endswith("/" + path) for value in reviewer_issue_paths))
    row["missed_paths_mentioned_in_reviewer_audit_summary_count"] = sum(
        1 for item in missed_rows if item["reviewer_audit_summary_mentions_basename"])

    # agent 有没有用 `rvf scope include` 去补（它只并进 primary_files，不进 primary_units / fix_allowlist）
    adjustment = scope_adjustments_by_run_id.get(run_id) or {}
    inclusions = [item for item in adjustment.get("inclusions") or [] if isinstance(item, dict)]
    included_paths = {str(item.get("path")) for item in inclusions}
    row["scope_include_path_count"] = len(included_paths)
    row["scope_include_reasons"] = sorted({str(item.get("reason") or "")[:160] for item in inclusions})[:3]
    row["scope_included_paths_still_outside_fix_allowlist_count"] = len(included_paths - fix_allowlist)
    row["scope_included_paths_still_in_protected_count"] = len(included_paths & protected_files)
    row["missed_paths_compensated_by_scope_include_count"] = sum(1 for item in missed_rows if item["path"] in included_paths)
    claimed_counts = row["handoff_claimed_reviewed_file_counts"] + row.get("final_reply_claimed_reviewed_file_counts", [])
    row["claimed_reviewed_file_count_exceeds_covered"] = any(count > row["covered_path_count"] for count in claimed_counts)
    row["dispatch_receipt_primary_files_exceeds_covered"] = (
        row.get("dispatch_receipt_primary_files_count") is not None and row["dispatch_receipt_primary_files_count"] > row["covered_path_count"])

    row.update(summarize_fix_attempt_elevations(artifacts_directory))

    # 后续 fix 提交（上界信号）
    if missed_rows and not arguments.skip_later_fix_commit_git_query:
        git_directory = resolve_git_directory_for_history_query(repository)
        row["later_commit_query_git_directory"] = git_directory
        if git_directory:
            # 同一 run 的已覆盖路径一并查询，作为「后续 fix 提交触及」的对照基线（热门文件本来就常被改）
            touched = later_commits_touching_paths(
                git_directory, run["run_started_at_utc"], sorted({item["path"] for item in missed_rows} | covered_paths))

            def later_fix_commits(path: str) -> list[dict[str, str]]:
                # 第一个触及它的提交通常就是落地这批改动本身，不算「后续修复」。
                return [commit for commit in touched.get(path, [])[1:]
                        if re.match(r"^(?:fix|revert)(?:\(|:|!)", commit["subject"])][:5]
            for item in missed_rows:
                item["later_commit_count_touching_path"] = len(touched.get(item["path"], []))
                item["later_fix_commits_after_first_landing_commit"] = later_fix_commits(item["path"])
            row["baseline_covered_path_count_with_git_history"] = len(covered_paths)
            row["baseline_covered_paths_touched_by_later_fix_commit_count"] = sum(
                1 for path in covered_paths if later_fix_commits(path))
        else:
            for item in missed_rows:
                item["later_commit_count_touching_path"] = None
    return row, missed_rows


def scan_declined_decision_reason_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for _, record in iterate_jsonl_records(ledger_path):
        reason = str(record.get("reason") or "")
        if ATTRIBUTION_BLINDNESS_DECLINE_REASON_PATTERN.search(reason):
            matches.append({
                "declined_at": record.get("declined_at"),
                "worktree": record.get("worktree"),
                "session_id": record.get("session_id"),
                "reason_excerpt": reason[:400],
            })
    return matches


# --------------------------------------------------------------------------------------------
# 汇总表
# --------------------------------------------------------------------------------------------


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def percentage(numerator: int, denominator: int) -> str:
    return f"{(100.0 * numerator / denominator):.0f}%" if denominator else "—"


def build_summary(result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    rows = result["run_rows"]
    missed = result["missed_path_rows"]
    analyzable = [row for row in rows if row["write_origin_analyzable"]]
    sections: list[str] = []
    summary: dict[str, Any] = {}

    shell_derived_missed = [item for item in missed if item.get("write_evidence_derived_only_from_shell_command_parsing")]
    summary["overall"] = {
        "statistics_generated_at_utc": result["generated_at_utc"],
        "run_directory_count": result.get("run_directory_count"),
        "run_with_scope_contract_count": result.get("run_with_scope_contract_count"),
        "scope_mode_counts_among_runs_with_scope_contract": result.get("scope_mode_counts_among_runs_with_scope_contract"),
        "session_owned_run_count": len(rows),
        "write_origin_analyzable_run_count": len(analyzable),
        "not_analyzable_run_count_by_reason": dict(collections.Counter(
            row.get("write_origin_not_analyzable_reason") or "unknown" for row in rows if not row["write_origin_analyzable"])),
        "runs_with_any_missed_session_written_path": sum(1 for row in analyzable if row["missed_session_written_path_count"] > 0),
        "missed_session_written_path_total": len(missed),
        "missed_path_count_with_write_evidence_only_from_shell_command_parsing": len(shell_derived_missed),
        "missed_path_count_with_subagent_edit_or_write_tool_evidence": sum(
            1 for item in missed if item["missed_cause"] == "invisible_writer_bash_or_subagent"
            and not item.get("write_evidence_derived_only_from_shell_command_parsing")),
        "missed_session_written_changed_line_total": sum(item["changed_line_count"] for item in missed),
        "missed_untracked_new_file_count": sum(1 for item in missed if item["is_untracked_new_file"]),
        "missed_path_count_by_cause": dict(collections.Counter(item["missed_cause"] for item in missed)),
        "runs_with_missed_by_cause": dict(collections.Counter(
            cause for row in analyzable for cause in row["missed_path_count_by_cause"])),
        "missed_changed_lines_by_cause": {cause: sum(item["changed_line_count"] for item in missed if item["missed_cause"] == cause)
                                          for cause in {item["missed_cause"] for item in missed}},
    }
    overall = summary["overall"]
    sections.append("## 总览\n\n" + markdown_table(["指标", "值"], [[key, value] for key, value in overall.items()]))

    # 分组：宿主 × 发起路径
    group_rows = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in analyzable:
        groups[(row["host_kind"], TRIGGER_KIND_DISPLAY_NAMES.get(row["trigger_kind"], row["trigger_kind"]))].append(row)
    for (host, trigger), members in sorted(groups.items(), key=lambda item: -len(item[1])):
        session_written = sum(member["session_written_candidate_path_count"] for member in members)
        missed_count = sum(member["missed_session_written_path_count"] for member in members)
        group_rows.append([host, trigger, len(members),
                           sum(1 for member in members if member["missed_session_written_path_count"] > 0),
                           session_written, missed_count, percentage(missed_count, session_written),
                           sum(member["missed_session_written_changed_line_count"] for member in members)])
    summary["by_host_and_trigger"] = group_rows
    sections.append("## 按宿主 × 发起路径（仅可判定的 run）\n\n" + markdown_table(
        ["宿主", "发起路径", "run 数", "有漏审的 run", "本会话写过的候选路径", "其中漏审", "漏审占比", "漏审行数"], group_rows))

    # 按月
    def invisible_origin_path_count(member: dict[str, Any]) -> int:
        merged = collections.Counter(member["covered_path_write_origin_counts"]) + collections.Counter(member["uncovered_path_write_origin_counts"])
        return sum(merged[label] for label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS)
    month_rows = []
    for month in sorted({row["run_month"] for row in analyzable}):
        members = [row for row in analyzable if row["run_month"] == month]
        session_written = sum(member["session_written_candidate_path_count"] for member in members)
        missed_count = sum(member["missed_path_count_by_cause"].get("invisible_writer_bash_or_subagent", 0) for member in members)
        invisible = sum(invisible_origin_path_count(member) for member in members)
        month_rows.append([month, len(members), sum(1 for member in members if member["missed_session_written_path_count"] > 0),
                           session_written, invisible, percentage(invisible, session_written), missed_count,
                           percentage(missed_count, invisible), percentage(missed_count, session_written)])
    summary["by_month"] = month_rows
    sections.append("## 按月（拆成「行为」×「机制」：只经 Bash/子代理写入的占比 × 这类路径落到覆盖外的比例）\n\n" + markdown_table(
        ["月份", "run 数", "有漏审的 run", "本会话写过的候选路径", "其中只经 Bash/子代理写", "占比（行为）",
         "漏审", "漏审/只经 Bash/子代理（机制）", "漏审/全部本会话写过"], month_rows))

    agentic_month_rows = []
    for month in sorted({row["run_month"] for row in analyzable if row["trigger_kind"] == "agentic-initiation-start"}):
        members = [row for row in analyzable if row["trigger_kind"] == "agentic-initiation-start" and row["run_month"] == month]
        agentic_month_rows.append([month, len(members), sum(1 for member in members if member["missed_session_written_path_count"] > 0)])
    summary["agentic_initiation_start_session_owned_runs_by_month"] = agentic_month_rows
    fallback_runs = result.get("agentic_initiation_runs_falling_back_to_manual_all_uncommitted_after_tracker_scope_restored") or []
    summary["agentic_initiation_manual_all_uncommitted_fallback_after_tracker_scope_restored"] = {
        "run_count": len(fallback_runs),
        "run_count_by_month": dict(sorted(collections.Counter(item["run_month"] for item in fallback_runs).items())),
        "run_count_by_primary_reason_recorded_in_scope_of_work": dict(collections.Counter(
            item["primary_reason_recorded_in_scope_of_work"] for item in fallback_runs).most_common()),
        "runs_with_more_than_one_reason_recorded": [
            [item["run_id"], item["all_reasons_recorded_in_scope_of_work"]] for item in fallback_runs
            if len(item["all_reasons_recorded_in_scope_of_work"]) > 1],
    }
    sections.append("## agent 自主发起：session-owned run 按月，及 0aac3ba（08-15）之后仍退回 manual-all-uncommitted 的 run\n\n"
                    + markdown_table(["月份", "session-owned run 数", "有漏审"], agentic_month_rows) + "\n\n"
                    + markdown_table(["指标", "值"], [[key, value] for key, value in
                                                     summary["agentic_initiation_manual_all_uncommitted_fallback_after_tracker_scope_restored"].items()]))

    # 按部署版本
    deploy_rows = []
    for deploy in sorted({row["rvf_plugin_deploy"] for row in analyzable}, key=lambda value: min(
            member["run_started_at_utc"] for member in analyzable if member["rvf_plugin_deploy"] == value)):
        members = [row for row in analyzable if row["rvf_plugin_deploy"] == deploy]
        missed_count = sum(member["missed_session_written_path_count"] for member in members)
        session_written = sum(member["session_written_candidate_path_count"] for member in members)
        deploy_rows.append([deploy, min(member["run_started_at_utc"] for member in members)[:10], len(members),
                            sum(1 for member in members if member["missed_session_written_path_count"] > 0),
                            missed_count, percentage(missed_count, session_written)])
    summary["by_deploy"] = deploy_rows
    sections.append("## 按部署版本\n\n" + markdown_table(["部署", "首个 run", "run 数", "有漏审的 run", "漏审路径", "漏审占比"], deploy_rows))

    # 路径级 条件 → 结果
    covered_counter: collections.Counter = collections.Counter()
    uncovered_counter: collections.Counter = collections.Counter()
    for row in analyzable:
        covered_counter.update(row["covered_path_write_origin_counts"])
        uncovered_counter.update(row["uncovered_path_write_origin_counts"])
    path_rows = []
    for label in sorted(set(covered_counter) | set(uncovered_counter)):
        total = covered_counter[label] + uncovered_counter[label]
        path_rows.append([label, total, covered_counter[label], uncovered_counter[label], percentage(uncovered_counter[label], total)])
    summary["path_level_condition_to_result"] = path_rows
    sections.append("## 路径级：写入来源（窗口内）→ 是否进入正式覆盖\n\n" + markdown_table(
        ["写入来源", "路径数", "进入覆盖", "未进入覆盖", "未覆盖占比"], path_rows))

    # run 级 条件 → 结果
    def has_invisible_writes(row: dict[str, Any]) -> bool:
        merged = collections.Counter(row["covered_path_write_origin_counts"]) + collections.Counter(row["uncovered_path_write_origin_counts"])
        return any(merged[label] > 0 for label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS)
    condition_rows = []
    for condition_value in (True, False):
        members = [row for row in analyzable if has_invisible_writes(row) == condition_value]
        with_missed = sum(1 for row in members if row["missed_session_written_path_count"] > 0)
        condition_rows.append(["有" if condition_value else "无", len(members), with_missed, len(members) - with_missed])
    summary["run_level_condition_to_result"] = condition_rows
    sections.append("## run 级：窗口内是否存在「只被 Bash / 子代理写过」的候选路径 → 是否漏审\n\n" + markdown_table(
        ["条件", "run 数", "有漏审", "无漏审"], condition_rows))

    # 反例
    counterexample_covered = []
    counterexample_uncovered_edit = []
    for row in analyzable:
        covered_invisible = sum(row["covered_path_write_origin_counts"].get(label, 0) for label in SESSION_WRITTEN_BUT_INVISIBLE_TO_RVF_ORIGINS)
        if covered_invisible:
            counterexample_covered.append([row["run_id"], row["host_kind"], covered_invisible, row["missed_session_written_path_count"]])
        uncovered_edit = row["uncovered_path_write_origin_counts"].get("main_session_edit_write_tool", 0)
        if uncovered_edit:
            counterexample_uncovered_edit.append([row["run_id"], row["host_kind"], uncovered_edit, row["covered_path_count"]])
    summary["counterexamples_invisible_origin_but_covered"] = counterexample_covered
    summary["counterexamples_edit_write_origin_but_uncovered"] = counterexample_uncovered_edit
    sections.append("## 反例 A：只被 Bash / 子代理写过、却进入了覆盖（前 30）\n\n" + markdown_table(
        ["run", "宿主", "这类被覆盖路径数", "同 run 漏审数"], counterexample_covered[:30]))
    sections.append("## 反例 B：主会话 Edit/Write 写过、却没进入覆盖（前 30）\n\n" + markdown_table(
        ["run", "宿主", "这类未覆盖路径数", "覆盖路径数"], counterexample_uncovered_edit[:30]))
    explanation_a: collections.Counter = collections.Counter()
    explanation_b: collections.Counter = collections.Counter()
    for row in analyzable:
        explanation_a.update(row["counterexample_invisible_origin_but_covered_explanations"])
        explanation_b.update(row["counterexample_edit_write_origin_but_uncovered_explanations"])
    summary["counterexample_a_explanations"] = dict(explanation_a)
    summary["counterexample_b_explanations"] = dict(explanation_b)
    summary["counterexample_a_unexplained_runs"] = [
        [row["run_id"], row["counterexample_invisible_origin_but_covered_explanations"]["unexplained"]]
        for row in analyzable if row["counterexample_invisible_origin_but_covered_explanations"].get("unexplained")]
    summary["counterexample_b_unexplained_runs"] = [
        [row["run_id"], row["counterexample_edit_write_origin_but_uncovered_explanations"]["unexplained"]]
        for row in analyzable if row["counterexample_edit_write_origin_but_uncovered_explanations"].get("unexplained")]
    sections.append("## 反例成因拆解\n\n" + markdown_table(
        ["反例", "成因", "路径数"],
        [["A 只经 Bash/子代理却被覆盖", key, value] for key, value in explanation_a.most_common()] +
        [["B Edit/Write 却未覆盖", key, value] for key, value in explanation_b.most_common()]) +
        "\n\n未解释的 run（A）：\n\n" + markdown_table(["run", "路径数"], summary["counterexample_a_unexplained_runs"]) +
        "\n\n未解释的 run（B）：\n\n" + markdown_table(["run", "路径数"], summary["counterexample_b_unexplained_runs"]))

    # 沟通
    with_missed = [row for row in analyzable if row["missed_session_written_path_count"] > 0]
    communication_rows = []
    level_display = {"names_attribution_blindness_as_cause": "点明归属失明", "says_some_files_not_reviewed": "只说有文件未审",
                     "none": "未提"}
    for row in with_missed:
        two_line = (row.get("final_reply_two_line_summary_gap_mention") or {}).get("level", "none")
        anywhere = (row.get("final_reply_anywhere_gap_mention") or {}).get("level", "none")
        handoff = (row.get("handoff_gap_mention") or {}).get("level", "none")
        if two_line != "none":
            prominence, level = "两行摘要里提到", two_line
        elif anywhere != "none":
            prominence, level = "最终回复其他位置提到", anywhere
        elif handoff != "none":
            prominence, level = "只在 handoff 正文提到", handoff
        else:
            prominence, level = "都没提", "none"
        row["coverage_gap_communication_prominence"] = prominence
        row["coverage_gap_communication_level"] = level_display[level]
        communication_rows.append(row)
    prominence_counter = collections.Counter(
        (row["coverage_gap_communication_prominence"], row["coverage_gap_communication_level"]) for row in communication_rows)
    summary["communication_prominence_among_runs_with_missed"] = {f"{key[0]}/{key[1]}": value for key, value in prominence_counter.items()}
    summary["communication_by_month"] = [
        [month, *[sum(1 for row in communication_rows if row["run_month"] == month and row["coverage_gap_communication_prominence"] == level)
                  for level in ("两行摘要里提到", "最终回复其他位置提到", "只在 handoff 正文提到", "都没提")]]
        for month in sorted({row["run_month"] for row in communication_rows})]
    summary["communication_snippets_for_manual_spot_check"] = [
        {"run_id": row["run_id"], "prominence": row["coverage_gap_communication_prominence"],
         "level": row["coverage_gap_communication_level"],
         "snippets": ((row.get("final_reply_anywhere_gap_mention") or {}).get("snippets") or []) +
                     ((row.get("handoff_gap_mention") or {}).get("snippets") or [])}
        for row in communication_rows if row["coverage_gap_communication_prominence"] != "都没提"]
    sections.append("## 沟通：有漏审的 run 里，覆盖缺口被提到的位置与程度\n\n" + markdown_table(
        ["位置", "程度", "run 数"], [[key[0], key[1], value] for key, value in prominence_counter.most_common()]) + "\n\n" + markdown_table(
        ["月份", "两行摘要", "最终回复其他位置", "只在 handoff", "都没提"], summary["communication_by_month"]) + "\n\n" +
        markdown_table(["run", "位置", "程度", "命中片段（【】为命中词）"],
                       [[item["run_id"], item["prominence"], item["level"], (item["snippets"][0] if item["snippets"] else "")[:200].replace("|", "/")]
                        for item in summary["communication_snippets_for_manual_spot_check"]]))
    summary["over_claim"] = {
        "runs_with_missed_and_dispatch_receipt_primary_files_exceeding_covered": sum(
            1 for row in with_missed if row["dispatch_receipt_primary_files_exceeds_covered"]),
        "runs_with_missed_and_dispatch_receipt_found": sum(1 for row in with_missed if row.get("dispatch_receipt_primary_files_count") is not None),
        "runs_with_missed_and_claimed_reviewed_file_count_exceeding_covered": sum(
            1 for row in with_missed if row["claimed_reviewed_file_count_exceeds_covered"]),
        "runs_with_missed_and_handoff_found": sum(1 for row in with_missed if row["handoff_found"]),
        "runs_with_missed_and_final_reply_found": sum(1 for row in with_missed if row.get("final_reply_found")),
        "dispatch_receipt_binding_method_counts_among_runs_with_missed": dict(collections.Counter(
            row.get("dispatch_receipt_binding_method") or "transcript_not_parsed" for row in with_missed)),
        "dispatch_receipt_binding_method_counts_among_all_analyzable_runs": dict(collections.Counter(
            row.get("dispatch_receipt_binding_method") or "transcript_not_parsed" for row in analyzable)),
    }
    sections.append("## 「看起来全审了」信号（有漏审的 run；dispatch 回执按 run id 绑定，无 run id 时限定在本 run → 同 transcript 下一 run 之间）\n\n"
                    + markdown_table(["指标", "值"], [[key, value] for key, value in summary["over_claim"].items()]))

    # shell 来源漏审的抽查样本（固定种子，便于复现；人工按摘录判定真伪，估计误判率）
    shell_kind_counter = collections.Counter(item.get("shell_write_trigger_kind") for item in shell_derived_missed)
    summary["shell_derived_missed_path_count_by_first_trigger_kind"] = dict(shell_kind_counter.most_common())
    summary["shell_derived_missed_path_count_matched_only_by_unique_basename"] = sum(
        1 for item in shell_derived_missed if item.get("matched_only_by_unique_basename"))
    spot_check_random_generator = random.Random(SHELL_DERIVED_MISSED_PATH_SPOT_CHECK_RANDOM_SEED)
    spot_check_sample = spot_check_random_generator.sample(
        shell_derived_missed, min(SHELL_DERIVED_MISSED_PATH_SPOT_CHECK_SAMPLE_SIZE, len(shell_derived_missed)))
    summary["shell_derived_missed_path_spot_check_sample"] = [
        {key: item.get(key) for key in (
            "run_id", "path", "write_origin", "shell_write_trigger_kind", "shell_write_trigger_writer_origin",
            "matched_only_by_unique_basename", "shell_write_trigger_transcript_path", "shell_write_trigger_transcript_line_number",
            "shell_write_trigger_command_excerpt")}
        for item in spot_check_sample]
    sections.append("## shell 来源漏审：按首个触发命令的写入种类\n\n" + markdown_table(
        ["写入种类", "漏审路径"], [[key, value] for key, value in shell_kind_counter.most_common()]) +
        f"\n\n其中只靠唯一 basename 命中：{summary['shell_derived_missed_path_count_matched_only_by_unique_basename']}")
    sections.append(f"## shell 来源漏审抽查样本（随机 {len(spot_check_sample)} 条，种子 {SHELL_DERIVED_MISSED_PATH_SPOT_CHECK_RANDOM_SEED}）\n\n"
                    + markdown_table(
                        ["#", "run", "路径", "写入种类", "来源", "transcript 行", "触发命令摘录"],
                        [[index + 1, item["run_id"][-8:], item["path"], item["shell_write_trigger_kind"],
                          item["shell_write_trigger_writer_origin"], item["shell_write_trigger_transcript_line_number"],
                          (item["shell_write_trigger_command_excerpt"] or "").replace("|", "/")]
                         for index, item in enumerate(summary["shell_derived_missed_path_spot_check_sample"])]))

    # elevation
    elevated = [(row["run_id"], attempt) for row in rows for attempt in row["elevated_attempts"]]
    summary["elevations"] = {
        "elevated_attempt_total": len(elevated),
        "elevated_attempt_caused_by_protected_or_out_of_allowlist_target": sum(
            1 for _, attempt in elevated if attempt["caused_by_protected_or_out_of_allowlist_target"]),
        "elevated_attempt_in_runs_with_missed": sum(
            1 for run_id, _ in elevated if run_id in {row["run_id"] for row in with_missed}),
        "elevated_attempt_in_runs_with_missed_caused_by_protected_or_out_of_allowlist_target": sum(
            1 for run_id, attempt in elevated if run_id in {row["run_id"] for row in with_missed}
            and attempt["caused_by_protected_or_out_of_allowlist_target"]),
        "elevated_attempts": [{"run_id": run_id, **attempt} for run_id, attempt in elevated],
    }
    sections.append("## fixer elevated\n\n" + markdown_table(["指标", "值"], [
        [key, value] for key, value in summary["elevations"].items() if key != "elevated_attempts"]) + "\n\n" + markdown_table(
        ["run", "issue", "是否因目标在 protected / allowlist 外", "摘录"],
        [[run_id, attempt["issue_id"], attempt["caused_by_protected_or_out_of_allowlist_target"],
          attempt["detail_excerpt"][:120].replace("|", "/")] for run_id, attempt in elevated]))

    # 改进候选可救回量
    def missed_where(predicate) -> tuple[int, int]:
        selected = [item for item in missed if predicate(item)]
        return len(selected), len({item["run_id"] for item in selected})
    kanban_runs = [row for row in analyzable if row["kanban_task_id"]]
    kanban_false_inclusion = sum(row["uncovered_path_write_origin_counts"].get("not_written_by_this_session", 0) for row in kanban_runs)
    recover_rows = [
        ["1 Kanban 单任务 worktree：unattributed 默认算本会话", *missed_where(lambda item: bool(item["kanban_task_id"])),
         f"误纳风险：Kanban worktree 里未覆盖且本会话没写过的路径 {kanban_false_inclusion} 个"],
        ["3 归属时读 subagents/agent-*.jsonl", *missed_where(lambda item: item["write_origin"] in ("subagent_only", "subagent_and_main_session_shell_only")), ""],
        ["4 Bash 前后快照（主会话；若子代理 Bash 也挂钩则含子代理）", *missed_where(lambda item: item["write_origin"] in ("main_session_shell_only", "codex_exec_shell_only", "subagent_and_main_session_shell_only")), ""],
        ["8 Claude Bash 也跑现有 command_path_candidates 正则", *missed_where(lambda item: item["recoverable_by_rvf_existing_shell_regex"]), ""],
    ]
    alert_runs = [row for row in analyzable if row["missed_session_written_path_count"] > 0]
    recover_rows.append(["2/5 覆盖率告警 + 确认闸（不救回，只让人看见）", len(missed), len(alert_runs), "以「有任一漏审路径」为点亮条件"])
    empty_attribution_runs = [row for row in analyzable if row["covered_path_count"] == 0 and row["candidate_path_count"] > 0]
    recover_rows.append(["9 归属为空 fail-loud", sum(row["missed_session_written_path_count"] for row in empty_attribution_runs),
                         len(empty_attribution_runs), "覆盖为 0 的 run"])
    summary["recoverability_by_improvement_candidate"] = recover_rows
    sections.append("## 改进候选在历史上可救回的漏审\n\n" + markdown_table(["候选", "漏审路径", "涉及 run", "备注"], recover_rows))

    # 影响：漏审最多的 run
    top_runs = sorted(with_missed, key=lambda row: -row["missed_session_written_changed_line_count"])[:25]
    summary["top_runs_by_missed_changed_lines"] = [[row["run_id"], row["host_kind"], row["kanban_task_id"], row["candidate_path_count"],
                                                    row["covered_path_count"], row["missed_session_written_path_count"],
                                                    row["missed_session_written_changed_line_count"],
                                                    row.get("coverage_gap_communication_prominence")] for row in top_runs]
    sections.append("## 漏审行数最多的 run（前 25）\n\n" + markdown_table(
        ["run", "宿主", "Kanban task", "候选", "覆盖", "漏审路径", "漏审行数", "缺口被提到的位置"], summary["top_runs_by_missed_changed_lines"]))

    later = [item for item in missed if item.get("later_commit_count_touching_path") is not None]
    summary["later_fix_commit_signal"] = {
        "missed_paths_with_git_history_available": len(later),
        "missed_paths_touched_by_later_fix_or_revert_commit_after_first_landing": sum(
            1 for item in later if item.get("later_fix_commits_after_first_landing_commit")),
        "baseline_covered_paths_in_same_runs_with_git_history": sum(
            row.get("baseline_covered_path_count_with_git_history", 0) for row in with_missed),
        "baseline_covered_paths_touched_by_later_fix_or_revert_commit_after_first_landing": sum(
            row.get("baseline_covered_paths_touched_by_later_fix_commit_count", 0) for row in with_missed),
    }
    sections.append("## 后续修复信号（上界，触及 ≠ 由漏审导致）\n\n" + markdown_table(
        ["指标", "值"], [[key, value] for key, value in summary["later_fix_commit_signal"].items()]))

    # 去重：同一 (仓库, 路径) 在同会话连续多个 run 里被重复计数
    unique_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for item in missed:
        key = (item["repository"], item["path"])
        previous = unique_pairs.get(key)
        if previous is None or item["changed_line_count"] > previous["changed_line_count"]:
            unique_pairs[key] = item
    summary["runs_with_missed_by_repository_name"] = dict(collections.Counter(
        Path(row["repository"]).name for row in with_missed).most_common())
    summary["deduplicated_missed"] = {
        "unique_repository_path_pairs": len(unique_pairs),
        "unique_changed_lines_max_per_pair": sum(item["changed_line_count"] for item in unique_pairs.values()),
        "unique_untracked_new_files": sum(1 for item in unique_pairs.values() if item["is_untracked_new_file"]),
        "unique_pairs_never_covered_in_any_later_session_owned_run": sum(
            1 for key in unique_pairs if not any(
                item.get("covered_in_a_later_session_owned_run") for item in missed if (item["repository"], item["path"]) == key)),
        "unique_repositories": len({key[0] for key in unique_pairs}),
        "unique_kanban_tasks": len({item["kanban_task_id"] for item in unique_pairs.values() if item["kanban_task_id"]}),
    }
    sections.append("## 去重后的漏审规模\n\n" + markdown_table(
        ["指标", "值"], [[key, value] for key, value in summary["deduplicated_missed"].items()]) +
        "\n\n有漏审的 run 按仓库：" + str(summary["runs_with_missed_by_repository_name"]))

    # reviewer 有没有顺手看漏审文件
    summary["reviewer_attention_on_missed_paths"] = {
        "missed_paths_total": len(missed),
        "missed_paths_with_reviewer_issue": sum(1 for item in missed if item.get("reviewer_raised_issue_on_this_path")),
        "missed_paths_named_in_reviewer_audit_summary": sum(1 for item in missed if item.get("reviewer_audit_summary_mentions_basename")),
        "runs_with_missed_where_any_reviewer_issue_hit_a_missed_path": sum(
            1 for row in with_missed if row.get("missed_paths_with_reviewer_issue_count")),
        "baseline_covered_paths_in_same_runs": sum(row["covered_path_count"] for row in with_missed),
        "baseline_covered_paths_with_reviewer_issue_in_same_runs": sum(
            row.get("covered_paths_with_reviewer_issue_count", 0) for row in with_missed),
    }
    sections.append("## reviewer 对漏审文件的实际关注（背景文件 reviewer 可读，但被告知不主动审）\n\n" + markdown_table(
        ["指标", "值"], [[key, value] for key, value in summary["reviewer_attention_on_missed_paths"].items()]))

    # agent 用 scope include 补救
    include_runs = [row for row in rows if row.get("scope_include_path_count")]
    summary["scope_include_compensation"] = [
        [row["run_id"], row["scope_include_path_count"], row["missed_paths_compensated_by_scope_include_count"],
         row["scope_included_paths_still_outside_fix_allowlist_count"], row["scope_included_paths_still_in_protected_count"],
         (row["scope_include_reasons"][0] if row["scope_include_reasons"] else "")[:90].replace("|", "/")]
        for row in include_runs]
    sections.append("## agent 用 `rvf scope include` 补救（include 只进 primary_files，不进 primary_units / fix_allowlist）\n\n" + markdown_table(
        ["run", "include 路径数", "其中属漏审", "仍不在 fix_allowlist", "仍在 protected", "理由摘录"], summary["scope_include_compensation"]))
    ledger_entries = result.get("scope_exception_issue_ledger_entries") or []
    summary["scope_exception_issue_ledger"] = dict(collections.Counter(entry.get("operation_type") for entry in ledger_entries))
    sections.append("## 范围异常账本（同源同类型 scope 例外累计 ≥ 阈值时自动写入的本地记录）\n\n" + markdown_table(
        ["首次记录", "类型", "路径数", "worktree"],
        [[entry.get("first_observed_at"), entry.get("operation_type"), entry.get("distinct_path_count"), entry.get("worktree")]
         for entry in ledger_entries]))

    summary["declined_reasons_mentioning_attribution_or_coverage_count"] = len(result["declined_decision_reasons_mentioning_attribution_or_coverage"])
    only_run_ids = result.get("only_run_id_filter_applied_after_window_and_coverage_index") or []
    filter_note = f"只输出 --only-run-id 指定的 {len(only_run_ids)} 个 run（窗口与覆盖索引仍用全部合同历史）。" if only_run_ids else ""
    markdown = (f"# RVF session-owned 归属覆盖缺口历史统计\n\n生成时间：{result['generated_at_utc']}（runs 目录持续增长，"
                f"数字只代表该时刻的快照）；数据源：`{result['run_logs_runs_directory']}`。{filter_note}\n\n"
                + "\n\n".join(sections) + "\n")
    return summary, markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-logs-runs-directory", default=str(DEFAULT_RVF_RUN_LOGS_RUNS_DIRECTORY))
    parser.add_argument("--declined-decision-reasons-ledger-path", default=str(DEFAULT_DECLINED_DECISION_REASONS_LEDGER_PATH))
    parser.add_argument("--agentic-scope-adjustments-directory", default=str(DEFAULT_AGENTIC_SCOPE_ADJUSTMENTS_DIRECTORY))
    parser.add_argument("--agentic-scope-issue-ledger-directory", default=str(DEFAULT_AGENTIC_SCOPE_ISSUE_LEDGER_DIRECTORY))
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--only-run-id", action="append")
    parser.add_argument("--skip-later-fix-commit-git-query", action="store_true")
    arguments = parser.parse_args()

    output_directory = Path(arguments.output_directory).expanduser().resolve()
    rvf_state_root = RVF_STATE_ROOT_DIRECTORY.resolve()
    if output_directory == rvf_state_root or rvf_state_root in output_directory.parents:
        parser.error("--output-directory 不得位于 ~/.rvf 内（本脚本对 RVF 运行态只读）")
    if path_is_inside_any_git_work_tree(output_directory):
        parser.error("--output-directory 不得位于 git 工作树内（否则输出会被某个会话的 RVF 当成改动）")

    result = analyze_runs(arguments)
    summary, markdown = build_summary(result)
    result["summary"] = summary
    output_directory.mkdir(parents=True, exist_ok=True)
    with open(output_directory / "per-run-and-per-missed-path-statistics.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
    with open(output_directory / "summary-tables.md", "w", encoding="utf-8") as handle:
        handle.write(markdown)
    print(markdown)
    print(f"[written] {output_directory}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
