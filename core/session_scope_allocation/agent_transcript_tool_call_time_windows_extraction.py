"""从 agent 会话 transcript 抽取每次工具调用的起止时间窗——session 归属第 2、3 层证据的共同积木。

为什么需要时间窗（08ac4 调查与 2026-10-05 的决定，见调查报告 §5.5）：
session-owned 归属原先只认 Edit/Write/apply_patch。Bash / Codex exec / 子代理写的文件没有任何
可直接读出的路径，于是整批落进 protected/background。本模块给出每次工具调用「从什么时候开始、
到什么时候结束、是谁（主会话还是哪个子代理）调用的、调用了什么」，供两类判定使用：

- 第 2 层（命令文本）：本会话（含子代理）的 shell 命令原文，交给
  ``shell_command_named_write_target_paths_extraction`` 解析写入目标；
- 第 3 层（时间窗）：文件改动时刻是否落在本会话某次可能写文件的调用期间，且同一时刻同一
  worktree 没有别的 agent 会话在调用工具。

各宿主的时间来源（均为实测，见各抽取函数的 docstring）：

- Claude Code：``tool_use``（assistant 记录）与 ``tool_result``（user 记录）的 ``timestamp``，按
  ``tool_use_id`` 配对；后台 Bash / 后台 Agent 立即返回 tool_result，真正的结束时刻取 host 合成的
  ``<task-notification>``（queue-operation / attachment.queued_command / user 记录里都带
  ``<tool-use-id>``）；子代理在 ``<transcript stem>/subagents/agent-*.jsonl``。
- Codex：新格式 ``item_completed`` 信封自带 ``started_at_ms`` / ``completed_at_ms``（CommandExecution =
  一次 shell 进程、FileChange = 一次补丁落盘）；旧格式按 ``call_id`` 配对 function_call 与
  function_call_output，进程跨轮询时沿 ``write_stdin(session_id)`` 串到「已退出」；子代理是独立
  rollout，由 ``collab_agent_spawn_end.new_thread_id`` / ``SubAgentActivity.agent_thread_id`` 找到。
  没有 item_completed 信封的 ``custom_tool_call(name=exec)`` 世代：补丁取成功的
  ``event_msg.patch_apply_end``（与 FileChange 同为 ``exec-<uuid>`` 命名空间，按 call_id 去重），
  shell 取 exec JS 文本里内嵌的 ``tools.exec_command({cmd, workdir})``，起止取 exec 调用 → 其输出。
- Kimi：``wire.jsonl`` 的 ``tool.call`` / ``tool.result`` 按 ``toolCallId`` 配对，时间是记录顶层
  ``time``（epoch 毫秒）；其它 agent 在同一 session 目录的 ``agents/<id>/wire.jsonl``。

全部时间统一为 UTC epoch 毫秒。纯读文件、不 spawn 任何进程（满足 ``core/`` 的 host-free 约束）。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.transcript.patch_parsing import synthesize_apply_patch_text_from_patch_apply_end

HOST_KIND_CLAUDE_CODE = "claude_code"
HOST_KIND_CODEX = "codex"
HOST_KIND_KIMI = "kimi"
HOST_KIND_UNKNOWN = "unknown_host_transcript_format"

TOOL_CALL_SOURCE_MAIN_SESSION = "main_session"
TOOL_CALL_SOURCE_SUBAGENT_PREFIX = "subagent:"

CLAUDE_FILE_WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
CLAUDE_SHELL_TOOL_NAMES = frozenset({"Bash"})
# MCP 写工具：按参数里的 ``relative_path`` 取写入路径（相对会话 cwd）。用户的全局指令推荐用 Serena 做符号级改写。
CLAUDE_MCP_FILE_WRITE_TOOL_NAMES_WITH_RELATIVE_PATH_ARGUMENT = frozenset({
    "mcp__serena__replace_content", "mcp__serena__replace_symbol_body", "mcp__serena__insert_after_symbol",
    "mcp__serena__insert_before_symbol", "mcp__serena__rename_symbol", "mcp__serena__safe_delete_symbol",
    "mcp__serena__create_text_file", "mcp__serena__replace_in_files",
})
CLAUDE_SUBAGENT_LAUNCH_TOOL_NAMES = frozenset({"Agent", "Task"})
# EXPERIMENT_NODE: 哪些工具算「不可能改仓库文件」——名单越宽，时间窗层把文件判给本会话的机会越少（漏判），越窄则越容易把巧合当证据（误判）。
CLAUDE_TOOL_NAMES_THAT_CANNOT_WRITE_REPOSITORY_FILES = frozenset({
    "Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "TodoWrite", "TodoRead", "ToolSearch",
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode", "Skill", "TaskOutput", "TaskStop", "TaskList",
    "TaskGet", "TaskCreate", "TaskUpdate", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "ReadMcpResourceDirTool", "SendMessage", "ListAgents", "ScheduleWakeup", "CronList", "Monitor",
    "PushNotification", "SendFeedback", "Artifact", "ArtifactComments", "ArtifactData", "ReportFindings",
})
CLAUDE_MCP_TOOL_NAME_PATTERNS_THAT_CANNOT_WRITE_REPOSITORY_FILES = (
    re.compile(r"^mcp__codegraph__"),
    re.compile(r"^mcp__claude-historian__"),
    re.compile(r"^mcp__context7__"),
    re.compile(r"^mcp__sequential-thinking__"),
    re.compile(r"^mcp__serena__(?:find|get|list|read|search|initial_instructions|onboarding|check|think)"),
    re.compile(r"^mcp__claude-in-chrome__"),
)
# 后台任务 tool_result 的立即返回文案（任务尚未结束）。
CLAUDE_BACKGROUND_TASK_IMMEDIATE_TOOL_RESULT_PATTERN = re.compile(
    r"Command running in background with ID|moved to the background|Async agent launched successfully|"
    r"is running in the background|running in background"
)
CLAUDE_TASK_NOTIFICATION_TOOL_USE_ID_PATTERN = re.compile(r"<tool-use-id>(?P<tool_use_id>[A-Za-z0-9_\-]+)</tool-use-id>")

CODEX_SHELL_FUNCTION_TOOL_NAMES = frozenset({"exec_command", "shell", "local_shell", "container.exec"})
CODEX_ITEM_TYPES_THAT_ARE_NOT_TOOL_CALLS = frozenset({
    "Reasoning", "AgentMessage", "UserMessage", "Plan", "SubAgentActivity", "CollabAgentToolCall",
    "ContextCompaction", "HookPrompt", "WebSearch", "ImageView", "TurnAborted", "Error",
})
CODEX_FUNCTION_TOOL_NAMES_THAT_ARE_NOT_FILE_WRITERS = frozenset({
    "exec", "wait", "wait_agent", "spawn_agent", "send_message", "close_agent", "resume_agent", "list_agents",
    "followup_task", "update_plan", "view_image", "request_user_input", "web_search", "image_generation",
})
CODEX_PROCESS_STILL_RUNNING_OUTPUT_PATTERN =re.compile(r"Process running with session ID (?P<session_id>\d+)")
CODEX_PROCESS_EXITED_OUTPUT_PATTERN = re.compile(r"Process exited with code (?P<exit_code>-?\d+)")
CODEX_SUBAGENT_THREAD_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CODEX_APPLY_PATCH_TEXT_FILE_HEADER_PATH_PATTERN = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE
)
# 新 Codex（gpt-5.6-sol 世代）的 custom_tool_call name=exec：input 是一段 JS，内含
# ``tools.exec_command({cmd: "…", workdir: "…"})``（JS 字符串，换行是 ``\n`` 转义）。
CODEX_EXEC_JAVASCRIPT_EMBEDDED_EXEC_COMMAND_CALL_PATTERN = re.compile(r"tools\s*\.\s*exec_command\s*\(")
CODEX_EXEC_JAVASCRIPT_EMBEDDED_ANY_TOOL_CALL_PATTERN = re.compile(r"tools\s*\.\s*[A-Za-z_]\w*\s*\(")
JAVASCRIPT_QUOTED_STRING_LITERAL_PATTERN = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`', re.DOTALL
)
JAVASCRIPT_OBJECT_STRING_LITERAL_PROPERTY_PATTERN_TEMPLATE = (
    r"(?:\b|[\"']){name}[\"']?\s*:\s*(?P<literal>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)"
)
JAVASCRIPT_OBJECT_IDENTIFIER_OR_SHORTHAND_PROPERTY_PATTERN_TEMPLATE = (
    r"(?:\b|[\"']){name}[\"']?\s*:\s*(?P<identifier>[A-Za-z_$][\w$]*)\b|[{{,]\s*(?P<shorthand>{name})\s*[,}}]"
)
JAVASCRIPT_STRING_LITERAL_SINGLE_CHARACTER_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}

KIMI_FILE_WRITE_TOOL_NAMES = frozenset({"Write", "Edit"})
KIMI_SHELL_TOOL_NAMES = frozenset({"Bash"})
KIMI_TOOL_NAMES_THAT_CANNOT_WRITE_REPOSITORY_FILES = frozenset({
    "Read", "ReadMediaFile", "Grep", "Glob", "WebFetch", "WebSearch", "SearchWeb", "FetchURL", "Think", "SetTodoList",
})


@dataclass(frozen=True)
class AgentToolCallTimeWindow:
    """一次工具调用的起止时间窗。``ended_at_epoch_ms is None`` 表示到解析时仍未结束（后台任务没有完成通知）。"""

    host_kind: str
    session_id: str | None
    tool_call_source: str  # main_session | subagent:<agent id>
    transcript_path: str
    transcript_line_number: int
    call_id: str | None
    tool_name: str
    started_at_epoch_ms: int
    ended_at_epoch_ms: int | None
    shell_command_text: str | None = None
    shell_working_directory: str | None = None
    file_write_paths: tuple[str, ...] = ()
    tool_call_cannot_write_repository_files: bool = False
    tool_call_ran_in_background: bool = False
    # 没找到结束记录，结束时刻取「同一 transcript 下一条带时间的记录」——会话已经往下走了，说明调用已结束。
    ended_at_is_estimated_from_next_transcript_record: bool = False
    # 子代理容器调用（Agent / spawn）：子代理自己的调用另有时间窗，容器本身不算写入窗。
    tool_call_is_subagent_container_with_own_transcript: bool = False
    tool_call_failed: bool | None = None

    @property
    def is_main_session_tool_call(self) -> bool:
        return self.tool_call_source == TOOL_CALL_SOURCE_MAIN_SESSION

    def ended_at_epoch_ms_or(self, open_window_end_epoch_ms: int) -> int:
        return self.ended_at_epoch_ms if self.ended_at_epoch_ms is not None else open_window_end_epoch_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_kind": self.host_kind,
            "session_id": self.session_id,
            "tool_call_source": self.tool_call_source,
            "transcript_path": self.transcript_path,
            "transcript_line_number": self.transcript_line_number,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "started_at_epoch_ms": self.started_at_epoch_ms,
            "ended_at_epoch_ms": self.ended_at_epoch_ms,
            "tool_call_ran_in_background": self.tool_call_ran_in_background,
            "ended_at_is_estimated_from_next_transcript_record": self.ended_at_is_estimated_from_next_transcript_record,
        }


@dataclass
class AgentSessionToolCallTimeWindows:
    host_kind: str
    session_id: str | None
    main_transcript_path: str
    working_directories_seen_in_transcript: set[str] = field(default_factory=set)
    windows: list[AgentToolCallTimeWindow] = field(default_factory=list)
    subagent_transcript_paths_read: list[str] = field(default_factory=list)
    first_activity_epoch_ms: int | None = None
    last_activity_epoch_ms: int | None = None
    transcript_parse_problems: list[str] = field(default_factory=list)
    transcript_byte_size_exceeded_limit: bool = False

    def note_activity(self, epoch_ms: int | None) -> None:
        if epoch_ms is None:
            return
        if self.first_activity_epoch_ms is None or epoch_ms < self.first_activity_epoch_ms:
            self.first_activity_epoch_ms = epoch_ms
        if self.last_activity_epoch_ms is None or epoch_ms > self.last_activity_epoch_ms:
            self.last_activity_epoch_ms = epoch_ms

    @property
    def call_ids(self) -> set[str]:
        return {window.call_id for window in self.windows if window.call_id}

    def possibly_writing_windows(self) -> list[AgentToolCallTimeWindow]:
        return [
            window
            for window in self.windows
            if not window.tool_call_cannot_write_repository_files
            and not window.tool_call_is_subagent_container_with_own_transcript
        ]


# --------------------------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------------------------


def iso_timestamp_to_epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def epoch_ms_to_iso_timestamp(epoch_ms: int | None) -> str | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def _iterate_jsonl_lines_with_records(path: Path) -> Iterator[tuple[int, str, dict[str, Any]]]:
    """逐行读 JSONL：(行号, 原始行文本, 记录)。非法行跳过（宿主正在写的半行很常见）。"""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield line_number, line, record


def _transcript_exceeds_byte_limit(path: Path, maximum_transcript_bytes: int | None) -> bool:
    if maximum_transcript_bytes is None:
        return False
    try:
        return path.stat().st_size > maximum_transcript_bytes
    except OSError:
        return False


def _codex_shell_command_text_from_arguments(command: Any) -> str | None:
    """Codex 的 cmd / command：字符串原样；``["bash", "-lc", "…"]`` 取真正的命令串。"""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command:
        parts = [str(part) for part in command]
        if len(parts) >= 3 and os.path.basename(parts[0]) in ("bash", "zsh", "sh") and parts[1] in ("-lc", "-c"):
            return parts[2]
        return " ".join(parts)
    return None


def _strip_file_url_scheme(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[len("file://"):] if value.startswith("file://") else value


def detect_agent_transcript_host_kind(transcript_path: Path) -> str:
    """按路径布局与首批记录形态判断宿主。判不出 → ``HOST_KIND_UNKNOWN``。"""
    path_text = str(transcript_path)
    if "/.claude/projects/" in path_text:
        return HOST_KIND_CLAUDE_CODE
    if "/.codex/" in path_text and transcript_path.name.startswith("rollout-"):
        return HOST_KIND_CODEX
    if transcript_path.name == "wire.jsonl":
        return HOST_KIND_KIMI
    try:
        for index, (_line_number, _line, record) in enumerate(_iterate_jsonl_lines_with_records(transcript_path)):
            if record.get("type") == "session_meta" or isinstance(record.get("payload"), dict):
                return HOST_KIND_CODEX
            if isinstance(record.get("sessionId"), str) or isinstance(record.get("message"), dict):
                return HOST_KIND_CLAUDE_CODE
            if record.get("type") == "context.append_loop_event" or isinstance(record.get("time"), int):
                return HOST_KIND_KIMI
            if index >= 20:
                break
    except OSError:
        return HOST_KIND_UNKNOWN
    return HOST_KIND_UNKNOWN


# --------------------------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------------------------


def claude_tool_name_cannot_write_repository_files(tool_name: str) -> bool:
    if tool_name in CLAUDE_TOOL_NAMES_THAT_CANNOT_WRITE_REPOSITORY_FILES:
        return True
    return any(pattern.match(tool_name) for pattern in CLAUDE_MCP_TOOL_NAME_PATTERNS_THAT_CANNOT_WRITE_REPOSITORY_FILES)


def _claude_tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


def _claude_subagent_transcripts_by_launching_tool_use_id(main_transcript_path: Path) -> dict[str, tuple[Path, str]]:
    """``<stem>/subagents/agent-*.jsonl`` → {父会话 Agent 调用的 tool_use_id: (子代理 transcript, agent id)}。
    ``.meta.json`` 缺失 / 无 ``toolUseId`` 的子代理用空串键收进来（仍会被读，只是挂不到父调用上）。"""
    subagents_directory = main_transcript_path.parent / main_transcript_path.stem / "subagents"
    mapping: dict[str, tuple[Path, str]] = {}
    if not subagents_directory.is_dir():
        return mapping
    for index, subagent_transcript in enumerate(sorted(subagents_directory.glob("agent-*.jsonl"))):
        agent_id = subagent_transcript.stem[len("agent-"):]
        launching_tool_use_id = ""
        try:
            meta = json.loads(subagent_transcript.with_suffix(".meta.json").read_text(encoding="utf-8"))
            if isinstance(meta, dict) and isinstance(meta.get("toolUseId"), str):
                launching_tool_use_id = meta["toolUseId"]
        except (OSError, json.JSONDecodeError):
            pass
        mapping[launching_tool_use_id or f"unlinked-subagent-{index}"] = (subagent_transcript, agent_id)
    return mapping


def _extract_claude_transcript_file_tool_call_time_windows(
    transcript_path: Path,
    *,
    tool_call_source: str,
    collected: AgentSessionToolCallTimeWindows,
    tool_use_ids_with_own_subagent_transcript: set[str],
) -> None:
    pending_tool_uses: dict[str, dict[str, Any]] = {}
    tool_use_order: list[str] = []
    completion_epoch_ms_by_tool_use_id: dict[str, int] = {}
    failed_by_tool_use_id: dict[str, bool] = {}
    background_tool_use_ids: set[str] = set()
    task_notification_epoch_ms_by_tool_use_id: dict[str, int] = {}
    timestamps_in_line_order: list[tuple[int, int]] = []

    for line_number, line, record in _iterate_jsonl_lines_with_records(transcript_path):
        epoch_ms = iso_timestamp_to_epoch_ms(record.get("timestamp"))
        if epoch_ms is not None:
            timestamps_in_line_order.append((line_number, epoch_ms))
            collected.note_activity(epoch_ms)
        if collected.session_id is None and isinstance(record.get("sessionId"), str):
            collected.session_id = record["sessionId"]
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            collected.working_directories_seen_in_transcript.add(cwd)
        if "<tool-use-id>" in line and epoch_ms is not None:
            for match in CLAUDE_TASK_NOTIFICATION_TOOL_USE_ID_PATTERN.finditer(line):
                tool_use_id = match.group("tool_use_id")
                previous = task_notification_epoch_ms_by_tool_use_id.get(tool_use_id)
                if previous is None or epoch_ms < previous:
                    task_notification_epoch_ms_by_tool_use_id[tool_use_id] = epoch_ms
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
            if block_type == "tool_use" and isinstance(block.get("name"), str) and epoch_ms is not None:
                tool_use_id = str(block.get("id") or f"line-{line_number}-{len(tool_use_order)}")
                if tool_use_id in pending_tool_uses:
                    continue  # 同一调用被复制的记录（resume / fork 复制历史）只算一次
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                pending_tool_uses[tool_use_id] = {
                    "line_number": line_number,
                    "started_at_epoch_ms": epoch_ms,
                    "tool_name": block["name"],
                    "tool_input": tool_input,
                    "cwd": cwd if isinstance(cwd, str) else None,
                }
                tool_use_order.append(tool_use_id)
                if tool_input.get("run_in_background") is True:
                    background_tool_use_ids.add(tool_use_id)
            elif block_type == "tool_result" and epoch_ms is not None:
                tool_use_id = str(block.get("tool_use_id") or "")
                if not tool_use_id or tool_use_id in completion_epoch_ms_by_tool_use_id:
                    continue
                completion_epoch_ms_by_tool_use_id[tool_use_id] = epoch_ms
                failed_by_tool_use_id[tool_use_id] = bool(block.get("is_error"))
                if CLAUDE_BACKGROUND_TASK_IMMEDIATE_TOOL_RESULT_PATTERN.search(_claude_tool_result_text(block)[:600]):
                    background_tool_use_ids.add(tool_use_id)

    for tool_use_id in tool_use_order:
        pending = pending_tool_uses[tool_use_id]
        tool_name = pending["tool_name"]
        tool_input = pending["tool_input"]
        started_at = pending["started_at_epoch_ms"]
        ran_in_background = tool_use_id in background_tool_use_ids
        ended_estimated = False
        if ran_in_background:
            ended_at: int | None = task_notification_epoch_ms_by_tool_use_id.get(tool_use_id)
        else:
            ended_at = completion_epoch_ms_by_tool_use_id.get(tool_use_id)
            if ended_at is None:
                ended_at = next(
                    (stamp for line, stamp in timestamps_in_line_order if line > pending["line_number"] and stamp >= started_at),
                    started_at,
                )
                ended_estimated = True
        shell_command_text: str | None = None
        file_write_paths: tuple[str, ...] = ()
        if tool_name in CLAUDE_SHELL_TOOL_NAMES and isinstance(tool_input.get("command"), str):
            shell_command_text = tool_input["command"]
        elif tool_name in CLAUDE_FILE_WRITE_TOOL_NAMES:
            file_path = tool_input.get("notebook_path" if tool_name == "NotebookEdit" else "file_path")
            if isinstance(file_path, str) and file_path:
                file_write_paths = (file_path,)
        elif tool_name in CLAUDE_MCP_FILE_WRITE_TOOL_NAMES_WITH_RELATIVE_PATH_ARGUMENT:
            relative_path = tool_input.get("relative_path")
            if isinstance(relative_path, str) and relative_path.strip():
                file_write_paths = (os.path.join(pending["cwd"], relative_path) if pending["cwd"] else relative_path,)
        is_subagent_container = tool_name in CLAUDE_SUBAGENT_LAUNCH_TOOL_NAMES and tool_use_id in tool_use_ids_with_own_subagent_transcript
        collected.windows.append(
            AgentToolCallTimeWindow(
                host_kind=HOST_KIND_CLAUDE_CODE,
                session_id=collected.session_id,
                tool_call_source=tool_call_source,
                transcript_path=str(transcript_path),
                transcript_line_number=pending["line_number"],
                call_id=tool_use_id,
                tool_name=tool_name,
                started_at_epoch_ms=started_at,
                ended_at_epoch_ms=ended_at,
                shell_command_text=shell_command_text,
                shell_working_directory=pending["cwd"],
                file_write_paths=file_write_paths,
                tool_call_cannot_write_repository_files=claude_tool_name_cannot_write_repository_files(tool_name),
                tool_call_ran_in_background=ran_in_background,
                ended_at_is_estimated_from_next_transcript_record=ended_estimated,
                tool_call_is_subagent_container_with_own_transcript=is_subagent_container,
                tool_call_failed=failed_by_tool_use_id.get(tool_use_id),
            )
        )


def extract_claude_code_session_tool_call_time_windows(
    transcript_path: Path,
    *,
    include_subagent_transcripts: bool = True,
    maximum_transcript_bytes: int | None = None,
) -> AgentSessionToolCallTimeWindows:
    collected = AgentSessionToolCallTimeWindows(
        host_kind=HOST_KIND_CLAUDE_CODE, session_id=None, main_transcript_path=str(transcript_path)
    )
    if _transcript_exceeds_byte_limit(transcript_path, maximum_transcript_bytes):
        collected.transcript_byte_size_exceeded_limit = True
        return collected
    subagents = _claude_subagent_transcripts_by_launching_tool_use_id(transcript_path) if include_subagent_transcripts else {}
    try:
        _extract_claude_transcript_file_tool_call_time_windows(
            transcript_path,
            tool_call_source=TOOL_CALL_SOURCE_MAIN_SESSION,
            collected=collected,
            tool_use_ids_with_own_subagent_transcript=set(subagents),
        )
    except OSError as exc:
        collected.transcript_parse_problems.append(f"main_transcript_unreadable: {exc}")
        return collected
    for subagent_transcript, agent_id in subagents.values():
        if _transcript_exceeds_byte_limit(subagent_transcript, maximum_transcript_bytes):
            collected.transcript_byte_size_exceeded_limit = True
            continue
        try:
            _extract_claude_transcript_file_tool_call_time_windows(
                subagent_transcript,
                tool_call_source=TOOL_CALL_SOURCE_SUBAGENT_PREFIX + agent_id,
                collected=collected,
                tool_use_ids_with_own_subagent_transcript=set(),
            )
            collected.subagent_transcript_paths_read.append(str(subagent_transcript))
        except OSError as exc:
            collected.transcript_parse_problems.append(f"subagent_transcript_unreadable: {subagent_transcript}: {exc}")
    return collected


# --------------------------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------------------------


def codex_sessions_root_directory() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "sessions"


def _find_codex_subagent_rollout(thread_id: str, *, parent_rollout_path: Path) -> Path | None:
    """先在父 rollout 同一天 / 后一天的目录里找 ``rollout-*-<thread id>.jsonl``，找不到再全量 glob。"""
    pattern = f"rollout-*-{thread_id}.jsonl"
    nearby_directories = [parent_rollout_path.parent]
    try:
        day = datetime.strptime("/".join(parent_rollout_path.parent.parts[-3:]), "%Y/%m/%d")
        next_day = day + timedelta(days=1)
        nearby_directories.append(parent_rollout_path.parents[3] / f"{next_day:%Y}" / f"{next_day:%m}" / f"{next_day:%d}")
    except (ValueError, IndexError):
        pass
    for directory in nearby_directories:
        if directory.is_dir():
            matches = sorted(directory.glob(pattern))
            if matches:
                return matches[-1]
    root = codex_sessions_root_directory()
    if root.is_dir():
        matches = sorted(root.glob(f"**/{pattern}"))
        if matches:
            return matches[-1]
    return None


def decode_javascript_quoted_string_literal_to_text(literal: str) -> str:
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
        if escaped in JAVASCRIPT_STRING_LITERAL_SINGLE_CHARACTER_ESCAPES:
            decoded.append(JAVASCRIPT_STRING_LITERAL_SINGLE_CHARACTER_ESCAPES[escaped])
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


def _javascript_identifier_string_constant_assignments(javascript_text: str) -> dict[str, str]:
    """``const name = "…"`` / ``let`` / ``var`` 形式的字符串常量，供 ``cmd: name`` / ``{cmd}`` 回查。"""
    assignments: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<literal>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)",
        javascript_text,
        re.DOTALL,
    ):
        assignments[match.group("name")] = decode_javascript_quoted_string_literal_to_text(match.group("literal"))
    return assignments


def _javascript_object_string_property_value(
    call_argument_text: str, property_name: str, identifier_string_constant_assignments: dict[str, str]
) -> str | None:
    literal_match = re.search(
        JAVASCRIPT_OBJECT_STRING_LITERAL_PROPERTY_PATTERN_TEMPLATE.format(name=property_name), call_argument_text, re.DOTALL
    )
    if literal_match:
        return decode_javascript_quoted_string_literal_to_text(literal_match.group("literal"))
    identifier_match = re.search(
        JAVASCRIPT_OBJECT_IDENTIFIER_OR_SHORTHAND_PROPERTY_PATTERN_TEMPLATE.format(name=property_name), call_argument_text
    )
    if identifier_match:
        identifier = identifier_match.group("identifier") or identifier_match.group("shorthand")
        return identifier_string_constant_assignments.get(identifier)
    return None


def extract_codex_exec_javascript_embedded_exec_command_calls(
    javascript_text: str,
) -> tuple[list[tuple[str, str | None]], bool]:
    """从新 Codex exec 的 JS 文本里抽出 ([(exec_command 的 cmd, workdir)], 是否有 exec_command 调用抽不出 cmd)。

    移植自 08ac4 调查的只读统计脚本 ``extract_codex_exec_javascript_tool_calls``（只保留 exec_command 部分：
    exec 内嵌的 apply_patch 以 ``patch_apply_end`` 为权威源，与 ``session_change_manifest`` 同口径）。"""
    identifier_string_constant_assignments = _javascript_identifier_string_constant_assignments(javascript_text)
    exec_commands: list[tuple[str, str | None]] = []
    has_exec_command_call_without_extractable_cmd = False
    call_starts = [match.start() for match in CODEX_EXEC_JAVASCRIPT_EMBEDDED_ANY_TOOL_CALL_PATTERN.finditer(javascript_text)]
    for match in CODEX_EXEC_JAVASCRIPT_EMBEDDED_EXEC_COMMAND_CALL_PATTERN.finditer(javascript_text):
        # 本次调用的参数文本：从调用处到下一个 tools.xxx( 调用之前
        following_starts = [start for start in call_starts if start > match.start()]
        call_argument_text = javascript_text[match.end():following_starts[0] if following_starts else len(javascript_text)]
        command = _javascript_object_string_property_value(call_argument_text, "cmd", identifier_string_constant_assignments)
        if command is None:
            command = _javascript_object_string_property_value(call_argument_text, "command", identifier_string_constant_assignments)
        workdir = _javascript_object_string_property_value(call_argument_text, "workdir", identifier_string_constant_assignments)
        if command is None:
            # cmd 来自数组 / 循环变量（``for (const [label, cmd] of commands)``）：退回「本段 JS 里所有像命令的
            # 字符串字面量」——这些数组元素就是实际执行的命令；一条都没有时才记为抽不出。
            fallback_commands = [
                decoded
                for decoded in (
                    decode_javascript_quoted_string_literal_to_text(literal.group(0))
                    for literal in JAVASCRIPT_QUOTED_STRING_LITERAL_PATTERN.finditer(javascript_text)
                )
                if " " in decoded.strip() and decoded != workdir
            ]
            if not fallback_commands:
                has_exec_command_call_without_extractable_cmd = True
                continue
            exec_commands.extend(
                (fallback_command, workdir)
                for fallback_command in dict.fromkeys(fallback_commands)
                if (fallback_command, workdir) not in exec_commands
            )
            continue
        exec_commands.append((command, workdir))
    return exec_commands, has_exec_command_call_without_extractable_cmd


def codex_patch_apply_end_successful_file_write_paths(payload: dict[str, Any]) -> tuple[str, ...]:
    """成功的 ``patch_apply_end`` 写到的路径（含 ``move_path`` 目标）；不成功 / 无可用 changes → 空。

    成功判据直接复用 ``synthesize_apply_patch_text_from_patch_apply_end``（``success is True`` 且 changes
    非空 dict、至少一条 update/add/delete），路径从合成的补丁文本头取——与 ``session_change_manifest`` 同口径。"""
    synthesized_patch_text = synthesize_apply_patch_text_from_patch_apply_end(payload)
    if synthesized_patch_text is None:
        return ()
    return tuple(
        dict.fromkeys(
            (match.group(1) or match.group(2)).strip()
            for match in CODEX_APPLY_PATCH_TEXT_FILE_HEADER_PATH_PATTERN.finditer(synthesized_patch_text)
        )
    )


def _extract_codex_rollout_file_tool_call_time_windows(
    rollout_path: Path,
    *,
    tool_call_source: str,
    collected: AgentSessionToolCallTimeWindows,
    subagent_thread_ids: list[str],
) -> None:
    item_windows: list[AgentToolCallTimeWindow] = []
    legacy_calls: dict[str, dict[str, Any]] = {}
    legacy_call_order: list[str] = []
    legacy_call_end_epoch_ms: dict[str, int] = {}
    legacy_call_still_running_process_session_id: dict[str, str] = {}
    legacy_process_session_last_output_epoch_ms: dict[str, int] = {}
    legacy_process_session_exited: set[str] = set()
    legacy_write_stdin_session_by_call_id: dict[str, str] = {}
    session_working_directory: str | None = None
    timestamps_in_line_order: list[tuple[int, int]] = []
    # 没有 item_completed 信封的 exec 世代：exec 单元 + patch_apply_end（RVF-004）。
    rollout_has_command_execution_or_file_change_item = False
    file_change_item_ids: set[str] = set()
    codex_exec_javascript_units_by_call_id: dict[str, dict[str, Any]] = {}
    codex_exec_javascript_unit_call_order: list[str] = []
    codex_exec_javascript_unit_output_epoch_ms_by_call_id: dict[str, int] = {}
    successful_patch_apply_end_events: list[dict[str, Any]] = []

    for line_number, _line, record in _iterate_jsonl_lines_with_records(rollout_path):
        epoch_ms = iso_timestamp_to_epoch_ms(record.get("timestamp"))
        if epoch_ms is not None:
            timestamps_in_line_order.append((line_number, epoch_ms))
            collected.note_activity(epoch_ms)
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        record_type = record.get("type")
        if record_type == "session_meta":
            if tool_call_source == TOOL_CALL_SOURCE_MAIN_SESSION and isinstance(payload.get("id"), str):
                collected.session_id = collected.session_id or payload["id"]
            if isinstance(payload.get("cwd"), str):
                session_working_directory = payload["cwd"]
                collected.working_directories_seen_in_transcript.add(payload["cwd"])
            continue
        if record_type == "turn_context" and isinstance(payload.get("cwd"), str):
            session_working_directory = payload["cwd"]
            collected.working_directories_seen_in_transcript.add(payload["cwd"])
            continue
        payload_type = payload.get("type")
        if payload_type == "collab_agent_spawn_end" and isinstance(payload.get("new_thread_id"), str):
            subagent_thread_ids.append(payload["new_thread_id"])
            continue
        if payload_type == "patch_apply_end":
            patch_file_write_paths = codex_patch_apply_end_successful_file_write_paths(payload)
            if patch_file_write_paths and epoch_ms is not None:
                # 能对上的 exec 调用：最近一次 exec 单元且此刻还没有输出（patch_apply_end 落在 exec 调用 → 输出之间）。
                latest_exec_call_id = codex_exec_javascript_unit_call_order[-1] if codex_exec_javascript_unit_call_order else None
                successful_patch_apply_end_events.append(
                    {
                        "line_number": line_number,
                        "recorded_at_epoch_ms": epoch_ms,
                        "call_id": payload["call_id"] if isinstance(payload.get("call_id"), str) and payload["call_id"] else None,
                        "file_write_paths": patch_file_write_paths,
                        "working_directory": session_working_directory,
                        "enclosing_exec_call_id": (
                            latest_exec_call_id
                            if latest_exec_call_id is not None
                            and latest_exec_call_id not in codex_exec_javascript_unit_output_epoch_ms_by_call_id
                            else None
                        ),
                    }
                )
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        if payload_type == "item_completed" and item is not None:
            item_type = item.get("type")
            if item_type == "SubAgentActivity" and isinstance(item.get("agent_thread_id"), str):
                subagent_thread_ids.append(item["agent_thread_id"])
                continue
            if item_type in CODEX_ITEM_TYPES_THAT_ARE_NOT_TOOL_CALLS:
                continue
            if item_type in ("CommandExecution", "FileChange"):
                rollout_has_command_execution_or_file_change_item = True
                if item_type == "FileChange" and item.get("id"):
                    file_change_item_ids.add(str(item["id"]))
            started_at = payload.get("started_at_ms") if isinstance(payload.get("started_at_ms"), int) else epoch_ms
            completed_at = payload.get("completed_at_ms") if isinstance(payload.get("completed_at_ms"), int) else epoch_ms
            if started_at is None:
                continue
            shell_command_text: str | None = None
            working_directory = session_working_directory
            file_write_paths: tuple[str, ...] = ()
            tool_name = str(item_type)
            tool_failed: bool | None = None
            if item_type == "CommandExecution":
                tool_name = "exec_command"
                shell_command_text = _codex_shell_command_text_from_arguments(item.get("command"))
                working_directory = _strip_file_url_scheme(item.get("cwd")) or session_working_directory
                exit_code = item.get("exit_code")
                tool_failed = (exit_code != 0) if isinstance(exit_code, int) else None
            elif item_type == "FileChange":
                tool_name = "apply_patch"
                changes = item.get("changes")
                if isinstance(changes, dict):
                    file_write_paths = tuple(str(path) for path in changes)
            elif item_type in ("McpToolCall", "DynamicToolCall"):
                tool_name = f"{item_type}:{item.get('tool') or item.get('name') or ''}"
            if working_directory:
                collected.working_directories_seen_in_transcript.add(working_directory)
            item_windows.append(
                AgentToolCallTimeWindow(
                    host_kind=HOST_KIND_CODEX,
                    session_id=collected.session_id,
                    tool_call_source=tool_call_source,
                    transcript_path=str(rollout_path),
                    transcript_line_number=line_number,
                    call_id=str(item.get("id")) if item.get("id") else None,
                    tool_name=tool_name,
                    started_at_epoch_ms=int(started_at),
                    ended_at_epoch_ms=int(completed_at) if completed_at is not None else None,
                    shell_command_text=shell_command_text,
                    shell_working_directory=working_directory,
                    file_write_paths=file_write_paths,
                    tool_call_failed=tool_failed,
                )
            )
            continue
        if epoch_ms is None:
            continue
        # 旧格式：function_call / custom_tool_call ↔ *_output，按 call_id 配对。
        if payload_type in ("function_call", "custom_tool_call") and isinstance(payload.get("call_id"), str):
            name = str(payload.get("name") or "")
            call_id = payload["call_id"]
            arguments_text = payload.get("arguments") if isinstance(payload.get("arguments"), str) else payload.get("input")
            if name == "write_stdin":
                try:
                    arguments = json.loads(arguments_text) if isinstance(arguments_text, str) else {}
                except json.JSONDecodeError:
                    arguments = {}
                process_session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
                if process_session_id is not None:
                    legacy_write_stdin_session_by_call_id[call_id] = str(process_session_id)
                continue
            if name == "exec" and payload_type == "custom_tool_call":
                # 有 item_completed 信封时 exec 单元由 CommandExecution / FileChange 记录；没有时（RVF-004）
                # 读完整个 rollout 后再把 JS 里内嵌的 tools.exec_command 抽成 shell 窗口。
                if call_id not in codex_exec_javascript_units_by_call_id:
                    codex_exec_javascript_units_by_call_id[call_id] = {
                        "line_number": line_number,
                        "started_at_epoch_ms": epoch_ms,
                        "javascript_text": arguments_text if isinstance(arguments_text, str) else "",
                        "working_directory": session_working_directory,
                    }
                    codex_exec_javascript_unit_call_order.append(call_id)
                continue
            if name in CODEX_FUNCTION_TOOL_NAMES_THAT_ARE_NOT_FILE_WRITERS:
                # 新格式的 exec 单元由 item_completed 记录；其余是编排 / 只读工具。
                continue
            shell_command_text = None
            working_directory = session_working_directory
            file_write_paths = ()
            if name in CODEX_SHELL_FUNCTION_TOOL_NAMES:
                try:
                    arguments = json.loads(arguments_text) if isinstance(arguments_text, str) else {}
                except json.JSONDecodeError:
                    arguments = {}
                if isinstance(arguments, dict):
                    shell_command_text = _codex_shell_command_text_from_arguments(arguments.get("cmd") or arguments.get("command"))
                    if isinstance(arguments.get("workdir"), str):
                        working_directory = arguments["workdir"]
            elif name == "apply_patch" and isinstance(arguments_text, str):
                file_write_paths = tuple(
                    (match.group(1) or match.group(2)).strip()
                    for match in CODEX_APPLY_PATCH_TEXT_FILE_HEADER_PATH_PATTERN.finditer(arguments_text)
                )
            legacy_calls[call_id] = {
                "line_number": line_number,
                "started_at_epoch_ms": epoch_ms,
                "tool_name": name,
                "shell_command_text": shell_command_text,
                "working_directory": working_directory,
                "file_write_paths": file_write_paths,
            }
            legacy_call_order.append(call_id)
            continue
        if payload_type in ("function_call_output", "custom_tool_call_output") and isinstance(payload.get("call_id"), str):
            call_id = payload["call_id"]
            if call_id in codex_exec_javascript_units_by_call_id:
                codex_exec_javascript_unit_output_epoch_ms_by_call_id.setdefault(call_id, epoch_ms)
                continue
            output = payload.get("output")
            output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False) if output is not None else ""
            running = CODEX_PROCESS_STILL_RUNNING_OUTPUT_PATTERN.search(output_text)
            exited = CODEX_PROCESS_EXITED_OUTPUT_PATTERN.search(output_text)
            process_session_id = legacy_write_stdin_session_by_call_id.get(call_id)
            if process_session_id is not None:
                legacy_process_session_last_output_epoch_ms[process_session_id] = epoch_ms
                if exited or not running:
                    legacy_process_session_exited.add(process_session_id)
                continue
            if call_id in legacy_calls and call_id not in legacy_call_end_epoch_ms:
                legacy_call_end_epoch_ms[call_id] = epoch_ms
                if running and not exited:
                    legacy_call_still_running_process_session_id[call_id] = running.group("session_id")
            continue

    for call_id in legacy_call_order:
        call = legacy_calls[call_id]
        ended_at: int | None = legacy_call_end_epoch_ms.get(call_id)
        ended_estimated = False
        process_session_id = legacy_call_still_running_process_session_id.get(call_id)
        if process_session_id is not None:
            if process_session_id in legacy_process_session_exited:
                ended_at = legacy_process_session_last_output_epoch_ms.get(process_session_id, ended_at)
            else:
                ended_at = None  # 进程到解析时都没看到退出
        elif ended_at is None:
            ended_at = next(
                (stamp for line, stamp in timestamps_in_line_order if line > call["line_number"] and stamp >= call["started_at_epoch_ms"]),
                call["started_at_epoch_ms"],
            )
            ended_estimated = True
        item_windows.append(
            AgentToolCallTimeWindow(
                host_kind=HOST_KIND_CODEX,
                session_id=collected.session_id,
                tool_call_source=tool_call_source,
                transcript_path=str(rollout_path),
                transcript_line_number=call["line_number"],
                call_id=call_id,
                tool_name=call["tool_name"],
                started_at_epoch_ms=call["started_at_epoch_ms"],
                ended_at_epoch_ms=ended_at,
                shell_command_text=call["shell_command_text"],
                shell_working_directory=call["working_directory"],
                file_write_paths=call["file_write_paths"],
                ended_at_is_estimated_from_next_transcript_record=ended_estimated,
            )
        )

    # 成功的 patch_apply_end：同一补丁已有 FileChange item（同为 ``exec-<uuid>``）或旧式 apply_patch 调用时不重复计。
    for patch_event in successful_patch_apply_end_events:
        patch_call_id = patch_event["call_id"]
        if patch_call_id is not None and (patch_call_id in file_change_item_ids or patch_call_id in legacy_calls):
            continue
        patch_started_at = patch_event["recorded_at_epoch_ms"]
        patch_ended_at = patch_started_at
        enclosing_exec_call_id = patch_event["enclosing_exec_call_id"]
        if enclosing_exec_call_id is not None:
            patch_started_at = min(
                patch_started_at, codex_exec_javascript_units_by_call_id[enclosing_exec_call_id]["started_at_epoch_ms"]
            )
            enclosing_exec_output_epoch_ms = codex_exec_javascript_unit_output_epoch_ms_by_call_id.get(enclosing_exec_call_id)
            if enclosing_exec_output_epoch_ms is not None:
                patch_ended_at = max(patch_ended_at, enclosing_exec_output_epoch_ms)
        item_windows.append(
            AgentToolCallTimeWindow(
                host_kind=HOST_KIND_CODEX,
                session_id=collected.session_id,
                tool_call_source=tool_call_source,
                transcript_path=str(rollout_path),
                transcript_line_number=patch_event["line_number"],
                call_id=patch_call_id,
                tool_name="apply_patch",
                started_at_epoch_ms=patch_started_at,
                ended_at_epoch_ms=patch_ended_at,
                shell_working_directory=patch_event["working_directory"],
                file_write_paths=patch_event["file_write_paths"],
                tool_call_failed=False,
            )
        )

    # 没有任何 CommandExecution / FileChange item 的 rollout：exec 单元里内嵌的 tools.exec_command 抽成 shell 窗口
    # （有 item 的 rollout 由 item 记录，不重复）。起止取 exec 调用 → 其输出；命令 yield 后仍在跑、靠后续 exec 里的
    # write_stdin 轮询到退出的情形不跟进（与旧格式的进程会话链不同），结束时刻会偏早。
    if not rollout_has_command_execution_or_file_change_item:
        for exec_call_id in codex_exec_javascript_unit_call_order:
            exec_unit = codex_exec_javascript_units_by_call_id[exec_call_id]
            exec_commands, has_exec_command_call_without_extractable_cmd = (
                extract_codex_exec_javascript_embedded_exec_command_calls(exec_unit["javascript_text"])
            )
            if not exec_commands and not has_exec_command_call_without_extractable_cmd:
                continue
            exec_started_at = exec_unit["started_at_epoch_ms"]
            exec_ended_at: int | None = codex_exec_javascript_unit_output_epoch_ms_by_call_id.get(exec_call_id)
            exec_ended_estimated = False
            if exec_ended_at is None:
                exec_ended_at = next(
                    (stamp for line, stamp in timestamps_in_line_order if line > exec_unit["line_number"] and stamp >= exec_started_at),
                    exec_started_at,
                )
                exec_ended_estimated = True
            # 抽不出 cmd 的 exec_command 仍记一个命令文本未知的 shell 窗口：时间窗层照样知道这段时间可能在写。
            commands_with_working_directory: list[tuple[str | None, str | None]] = list(exec_commands)
            if has_exec_command_call_without_extractable_cmd:
                commands_with_working_directory.append((None, None))
            for command_text, command_working_directory in commands_with_working_directory:
                working_directory = command_working_directory or exec_unit["working_directory"]
                if working_directory:
                    collected.working_directories_seen_in_transcript.add(working_directory)
                item_windows.append(
                    AgentToolCallTimeWindow(
                        host_kind=HOST_KIND_CODEX,
                        session_id=collected.session_id,
                        tool_call_source=tool_call_source,
                        transcript_path=str(rollout_path),
                        transcript_line_number=exec_unit["line_number"],
                        call_id=exec_call_id,
                        tool_name="exec_command",
                        started_at_epoch_ms=exec_started_at,
                        ended_at_epoch_ms=exec_ended_at,
                        shell_command_text=command_text,
                        shell_working_directory=working_directory,
                        ended_at_is_estimated_from_next_transcript_record=exec_ended_estimated,
                    )
                )
    collected.windows.extend(item_windows)


def extract_codex_rollout_tool_call_time_windows(
    rollout_path: Path,
    *,
    include_subagent_rollouts: bool = True,
    maximum_transcript_bytes: int | None = None,
) -> AgentSessionToolCallTimeWindows:
    collected = AgentSessionToolCallTimeWindows(
        host_kind=HOST_KIND_CODEX, session_id=None, main_transcript_path=str(rollout_path)
    )
    if _transcript_exceeds_byte_limit(rollout_path, maximum_transcript_bytes):
        collected.transcript_byte_size_exceeded_limit = True
        return collected
    subagent_thread_ids: list[str] = []
    try:
        _extract_codex_rollout_file_tool_call_time_windows(
            rollout_path,
            tool_call_source=TOOL_CALL_SOURCE_MAIN_SESSION,
            collected=collected,
            subagent_thread_ids=subagent_thread_ids,
        )
    except OSError as exc:
        collected.transcript_parse_problems.append(f"main_transcript_unreadable: {exc}")
        return collected
    if not include_subagent_rollouts:
        return collected
    seen_thread_ids: set[str] = set()
    pending_thread_ids = list(subagent_thread_ids)
    while pending_thread_ids:
        thread_id = pending_thread_ids.pop(0)
        if thread_id in seen_thread_ids or not CODEX_SUBAGENT_THREAD_ID_PATTERN.match(thread_id):
            continue
        seen_thread_ids.add(thread_id)
        subagent_rollout = _find_codex_subagent_rollout(thread_id, parent_rollout_path=rollout_path)
        if subagent_rollout is None or subagent_rollout.resolve() == rollout_path.resolve():
            continue
        if _transcript_exceeds_byte_limit(subagent_rollout, maximum_transcript_bytes):
            collected.transcript_byte_size_exceeded_limit = True
            continue
        nested_thread_ids: list[str] = []
        try:
            _extract_codex_rollout_file_tool_call_time_windows(
                subagent_rollout,
                tool_call_source=TOOL_CALL_SOURCE_SUBAGENT_PREFIX + thread_id,
                collected=collected,
                subagent_thread_ids=nested_thread_ids,
            )
            collected.subagent_transcript_paths_read.append(str(subagent_rollout))
        except OSError as exc:
            collected.transcript_parse_problems.append(f"subagent_transcript_unreadable: {subagent_rollout}: {exc}")
        pending_thread_ids.extend(nested_thread_ids)
    return collected


# --------------------------------------------------------------------------------------------
# Kimi
# --------------------------------------------------------------------------------------------


def _extract_kimi_wire_file_tool_call_time_windows(
    wire_path: Path,
    *,
    tool_call_source: str,
    session_working_directory: str | None,
    collected: AgentSessionToolCallTimeWindows,
) -> None:
    calls: dict[str, dict[str, Any]] = {}
    call_order: list[str] = []
    result_epoch_ms: dict[str, int] = {}
    timestamps_in_line_order: list[tuple[int, int]] = []
    for line_number, _line, record in _iterate_jsonl_lines_with_records(wire_path):
        epoch_ms = record.get("time") if isinstance(record.get("time"), int) else None
        if epoch_ms is not None:
            timestamps_in_line_order.append((line_number, epoch_ms))
            collected.note_activity(epoch_ms)
        event = record.get("event") if isinstance(record.get("event"), dict) else None
        if record.get("type") != "context.append_loop_event" or event is None or epoch_ms is None:
            continue
        tool_call_id = event.get("toolCallId")
        if not isinstance(tool_call_id, str):
            continue
        if event.get("type") == "tool.call" and tool_call_id not in calls:
            arguments = event.get("args") if isinstance(event.get("args"), dict) else {}
            calls[tool_call_id] = {
                "line_number": line_number,
                "started_at_epoch_ms": epoch_ms,
                "tool_name": str(event.get("name") or ""),
                "arguments": arguments,
            }
            call_order.append(tool_call_id)
        elif event.get("type") == "tool.result" and tool_call_id not in result_epoch_ms:
            result_epoch_ms[tool_call_id] = epoch_ms
    for tool_call_id in call_order:
        call = calls[tool_call_id]
        tool_name = call["tool_name"]
        arguments = call["arguments"]
        ended_at = result_epoch_ms.get(tool_call_id)
        ended_estimated = False
        if ended_at is None:
            ended_at = next(
                (stamp for line, stamp in timestamps_in_line_order if line > call["line_number"] and stamp >= call["started_at_epoch_ms"]),
                call["started_at_epoch_ms"],
            )
            ended_estimated = True
        shell_command_text = arguments.get("command") if tool_name in KIMI_SHELL_TOOL_NAMES and isinstance(arguments.get("command"), str) else None
        file_write_paths: tuple[str, ...] = ()
        if tool_name in KIMI_FILE_WRITE_TOOL_NAMES and isinstance(arguments.get("path"), str):
            file_write_paths = (arguments["path"],)
        collected.windows.append(
            AgentToolCallTimeWindow(
                host_kind=HOST_KIND_KIMI,
                session_id=collected.session_id,
                tool_call_source=tool_call_source,
                transcript_path=str(wire_path),
                transcript_line_number=call["line_number"],
                call_id=tool_call_id,
                tool_name=tool_name,
                started_at_epoch_ms=call["started_at_epoch_ms"],
                ended_at_epoch_ms=ended_at,
                shell_command_text=shell_command_text,
                shell_working_directory=session_working_directory,
                file_write_paths=file_write_paths,
                tool_call_cannot_write_repository_files=tool_name in KIMI_TOOL_NAMES_THAT_CANNOT_WRITE_REPOSITORY_FILES,
                ended_at_is_estimated_from_next_transcript_record=ended_estimated,
            )
        )


def extract_kimi_session_tool_call_time_windows(
    main_agent_wire_path: Path,
    *,
    include_other_agents: bool = True,
    maximum_transcript_bytes: int | None = None,
) -> AgentSessionToolCallTimeWindows:
    """``<session dir>/agents/main/wire.jsonl``：同一 session 目录下其它 agent 当作子代理一并读。"""
    collected = AgentSessionToolCallTimeWindows(
        host_kind=HOST_KIND_KIMI, session_id=None, main_transcript_path=str(main_agent_wire_path)
    )
    if _transcript_exceeds_byte_limit(main_agent_wire_path, maximum_transcript_bytes):
        collected.transcript_byte_size_exceeded_limit = True
        return collected
    agents_directory = main_agent_wire_path.parent.parent
    session_directory = agents_directory.parent
    session_working_directory: str | None = None
    try:
        state = json.loads((session_directory / "state.json").read_text(encoding="utf-8"))
        if isinstance(state, dict):
            collected.session_id = state.get("id") if isinstance(state.get("id"), str) else session_directory.name
            session_working_directory = state.get("cwd") or state.get("workDir")
    except (OSError, json.JSONDecodeError):
        collected.session_id = session_directory.name
    if isinstance(session_working_directory, str) and session_working_directory:
        collected.working_directories_seen_in_transcript.add(session_working_directory)
    else:
        session_working_directory = None
    try:
        _extract_kimi_wire_file_tool_call_time_windows(
            main_agent_wire_path,
            tool_call_source=TOOL_CALL_SOURCE_MAIN_SESSION,
            session_working_directory=session_working_directory,
            collected=collected,
        )
    except OSError as exc:
        collected.transcript_parse_problems.append(f"main_transcript_unreadable: {exc}")
        return collected
    if include_other_agents and agents_directory.is_dir():
        for other_wire in sorted(agents_directory.glob("*/wire.jsonl")):
            if other_wire.resolve() == main_agent_wire_path.resolve():
                continue
            if _transcript_exceeds_byte_limit(other_wire, maximum_transcript_bytes):
                collected.transcript_byte_size_exceeded_limit = True
                continue
            try:
                _extract_kimi_wire_file_tool_call_time_windows(
                    other_wire,
                    tool_call_source=TOOL_CALL_SOURCE_SUBAGENT_PREFIX + other_wire.parent.name,
                    session_working_directory=session_working_directory,
                    collected=collected,
                )
                collected.subagent_transcript_paths_read.append(str(other_wire))
            except OSError as exc:
                collected.transcript_parse_problems.append(f"subagent_transcript_unreadable: {other_wire}: {exc}")
    return collected


# --------------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------------


def extract_agent_session_tool_call_time_windows(
    transcript_path: Path,
    *,
    include_subagent_transcripts: bool = True,
    maximum_transcript_bytes: int | None = None,
    host_kind: str | None = None,
) -> AgentSessionToolCallTimeWindows:
    """按宿主分派。宿主判不出时返回空结果并在 ``transcript_parse_problems`` 里说明。"""
    resolved_host_kind = host_kind or detect_agent_transcript_host_kind(transcript_path)
    if resolved_host_kind == HOST_KIND_CLAUDE_CODE:
        return extract_claude_code_session_tool_call_time_windows(
            transcript_path,
            include_subagent_transcripts=include_subagent_transcripts,
            maximum_transcript_bytes=maximum_transcript_bytes,
        )
    if resolved_host_kind == HOST_KIND_CODEX:
        return extract_codex_rollout_tool_call_time_windows(
            transcript_path,
            include_subagent_rollouts=include_subagent_transcripts,
            maximum_transcript_bytes=maximum_transcript_bytes,
        )
    if resolved_host_kind == HOST_KIND_KIMI:
        return extract_kimi_session_tool_call_time_windows(
            transcript_path,
            include_other_agents=include_subagent_transcripts,
            maximum_transcript_bytes=maximum_transcript_bytes,
        )
    collected = AgentSessionToolCallTimeWindows(
        host_kind=HOST_KIND_UNKNOWN, session_id=None, main_transcript_path=str(transcript_path)
    )
    collected.transcript_parse_problems.append("unknown_host_transcript_format")
    return collected
