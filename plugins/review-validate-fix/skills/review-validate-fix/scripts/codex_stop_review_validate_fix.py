#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import struct
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, log_root, start_run
from vibe_kanban_mcp_client import (
    DEFAULT_START_CMD as DEFAULT_VIBE_KANBAN_START_CMD,
    DEFAULT_START_TIMEOUT_SECONDS as DEFAULT_VIBE_KANBAN_START_TIMEOUT_SECONDS,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = SKILL_DIR / "state"
DEFAULT_APP_SERVER_CONTROL_SOCKET = (
    Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
)
DEFAULT_BRIDGE_SOCKET = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.sock"
DEFAULT_BRIDGE_LOG = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.log"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_SESSION_HOOK_STATE_DIR = SKILL_DIR / "state" / "session-hook"
DEFAULT_VIBE_KANBAN_MCP_CLIENT = SKILL_DIR / "scripts" / "vibe_kanban_mcp_client.py"
DEFAULT_VIBE_KANBAN_RUNNER = SKILL_DIR / "scripts" / "run_vibe_kanban_rvf.py"
DEFAULT_PREPARE_REVIEW_RUN = SKILL_DIR / "scripts" / "prepare_review_run.py"
DEFAULT_VIBE_KANBAN_MCP_CMD = "npx -y vibe-kanban@0.1.44 --mcp"
DEFAULT_CODEX_EXEC_ARGS = "exec --json --dangerously-bypass-approvals-and-sandbox"
DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS = 8.0
DEFAULT_OPEN_GUI_FORK_ATTEMPTS = 3
DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS = 5
DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY = "report"
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
SUPPRESS_STOP_HOOK_MARKER = "CODEX_RVF_SUPPRESS_STOP_HOOK=1"
DEFAULT_RVF_MODE = "fork"
DEFAULT_FORK_LAUNCH_MODE = "gui"
APP_SERVER_CLIENT_INFO = {
    "name": "review-validate-fix-stop-hook",
    "title": "review-validate-fix Stop hook",
    "version": "0.1.0",
}
SUPPRESS_ENV_NAMES = (
    "CODEX_RVF_SUPPRESS",
    "CODEX_RVF_SUPPRESS_STOP_HOOK",
)
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


@dataclass(frozen=True)
class GateResult:
    status: str
    repo: str | None
    output: str


class AppServerError(RuntimeError):
    pass


class AppServerSocketSelectionError(AppServerError):
    def __init__(self, message: str, socket_selection: dict[str, Any]) -> None:
        super().__init__(message)
        self.socket_selection = socket_selection


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def skip_payload(
    reason: str,
    ledger: RunLedger | None = None,
    reason_code: str = "skipped",
    **summary_fields: Any,
) -> dict[str, Any]:
    if ledger is not None:
        ledger.event(
            phase="gate",
            event="skipped",
            status="skipped",
            reason_code=reason_code,
            message=reason,
        )
        return ledger.hook_payload(
            status="skipped",
            reason_code=reason_code,
            message=reason,
            **summary_fields,
        )
    return {
        "continue": True,
        "systemMessage": f"review-validate-fix Stop hook 未创建 fork：{reason}",
    }


def state_dir() -> Path:
    return log_root()


def session_hook_state_dir() -> Path:
    explicit = os.environ.get("CODEX_RVF_SESSION_HOOK_STATE_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()

    state_root = os.environ.get("CODEX_RVF_STATE_DIR")
    if state_root and state_root.strip():
        return Path(state_root).expanduser() / "session-hook"

    return DEFAULT_SESSION_HOOK_STATE_DIR


def read_event() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return event


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_state_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return key[:180] if key else "unknown-session"


def source_marks_subagent(source: Any) -> bool:
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


def session_meta_marks_subagent(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return False
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict) and source_marks_subagent(payload.get("source")):
                    return True
    except OSError:
        return False
    return False


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def text_from_message_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def latest_user_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue

                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        if text:
                            latest = text
    except OSError:
        return None
    return latest


def user_messages_containing(path: Path, marker: str) -> list[str]:
    messages: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                text = ""
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    text = message if isinstance(message, str) else ""
                elif record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)

                if marker in text:
                    messages.append(text)
    except OSError:
        return []
    return messages


def latest_user_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_user_message")
    if isinstance(direct, str) and direct:
        return direct

    for path in event_session_paths(event):
        message = latest_user_message(path)
        if message:
            return message
    return None


def session_id_from_path(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    return payload["id"]
    except OSError:
        return None
    return None


def session_id_from_event(event: dict[str, Any]) -> str | None:
    value = event.get("session_id")
    if isinstance(value, str) and value:
        return value

    for path in event_session_paths(event):
        session_id = session_id_from_path(path)
        if session_id:
            return session_id
    return None


def parent_thread_path_from_event(event: dict[str, Any]) -> Path | None:
    for path in event_session_paths(event):
        expanded = path.expanduser()
        if expanded.exists() and session_id_from_path(expanded) is not None:
            return expanded.resolve()
    return None


def parent_thread_id_from_event(event: dict[str, Any]) -> str | None:
    for path in event_session_paths(event):
        session_id = session_id_from_path(path.expanduser())
        if session_id:
            return session_id

    env_value = os.environ.get("CODEX_THREAD_ID")
    if env_value and env_value.strip():
        return env_value.strip()

    for key in (
        "thread_id",
        "threadId",
        "conversation_id",
        "conversationId",
        "session_id",
    ):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return session_id_from_event(event)


def session_hook_state_path(session_id: str) -> Path:
    return session_hook_state_dir() / f"{safe_state_key(session_id)}.json"


def session_hook_id_from_event(event: dict[str, Any]) -> str | None:
    return session_id_from_event(event) or parent_thread_id_from_event(event)


def parse_session_hook_control(text: str | None) -> str | None:
    if not text:
        return None
    pattern = re.compile(
        rf"^\s*{re.escape(SESSION_HOOK_CONTROL_KEY)}\s*:\s*([A-Za-z_-]+)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().lower().replace("_", "-")
    if value in {"off", "disable", "disabled", "skip", "suppress"}:
        return "off"
    if value in {"on", "enable", "enabled", "resume"}:
        return "on"
    if value in {"status", "state"}:
        return "status"
    return None


def read_session_hook_state(session_id: str) -> dict[str, Any] | None:
    path = session_hook_state_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def session_hook_disabled(session_id: str) -> bool:
    state = read_session_hook_state(session_id)
    return state is not None and state.get("enabled") is False


def set_session_hook_enabled(
    *,
    session_id: str,
    enabled: bool,
    latest_user: str | None,
) -> Path | None:
    path = session_hook_state_path(session_id)
    if enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return path
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "enabled": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "control": SESSION_HOOK_CONTROL_KEY,
                "latest_user_message": latest_user,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def session_hook_control_payload(
    event: dict[str, Any],
    latest_user: str | None,
) -> dict[str, Any] | None:
    action = parse_session_hook_control(latest_user)
    if action is None:
        return None

    session_id = session_hook_id_from_event(event)
    if not session_id:
        return {
            "continue": True,
            "reason_code": "session_hook_gate_unknown_session",
            "systemMessage": (
                "review-validate-fix 无法记录当前 chat session 的 RVF 自动触发 gate："
                "Stop event 未暴露 session id。Stop hook 本身未因此关闭。"
            ),
        }

    if action == "status":
        status = "disabled" if session_hook_disabled(session_id) else "enabled"
        return {
            "continue": True,
            "reason_code": "session_hook_gate_status",
            "control_action": "status",
            "session_hook_gate_state": status,
            "systemMessage": (
                "当前 chat session 的 RVF 自动触发 gate 状态为 "
                f"{status}。这只表示本 session 后续 Stop 是否允许自动启动 RVF "
                "fork/continuation/review；不表示全局 Stop hook 是否安装或运行。"
                f"session_id={session_id}"
            ),
        }

    enabled = action == "on"
    state_path = set_session_hook_enabled(
        session_id=session_id,
        enabled=enabled,
        latest_user=latest_user,
    )
    status = "enabled" if enabled else "disabled"
    reason_code = "session_hook_gate_enabled" if enabled else "session_hook_gate_disabled"
    action_label = "允许" if enabled else "禁止"
    return {
        "continue": True,
        "reason_code": reason_code,
        "control_action": action,
        "session_hook_gate_state": status,
        "state_path": str(state_path) if state_path is not None else None,
        "systemMessage": (
            f"已记录当前 chat session 的 RVF 自动触发 gate 为 {status}，"
            f"即后续 Stop 将{action_label}自动启动 RVF fork/continuation/review。"
            "这不是关闭全局 Stop hook：dispatcher 仍会运行，dev sync 仍可能执行。"
            f"session_id={session_id}; state={state_path}。"
        ),
    }


def string_event_value(event: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def configured_reasoning_effort() -> str | None:
    env_value = os.environ.get("CODEX_RVF_FORK_REASONING_EFFORT")
    if env_value and env_value.strip():
        return env_value.strip()

    config_path = Path(os.environ.get("CODEX_RVF_CODEX_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        return None

    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        value = data.get("model_reasoning_effort")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    pattern = re.compile(r'^model_reasoning_effort\s*=\s*"([^"]+)"\s*$')
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    except OSError:
        return None
    return None


def reasoning_effort_for_fork(event: dict[str, Any]) -> str | None:
    return string_event_value(
        event,
        (
            "model_reasoning_effort",
            "reasoning_effort",
            "reasoningEffort",
        ),
    ) or configured_reasoning_effort()


def fork_experiment_prompt(parent_session_id: str, cwd: str | None) -> str:
    cwd_line = cwd or "<unknown cwd>"
    return (
        "Codex fork experiment sidecar session.\n\n"
        f"Parent session id: {parent_session_id}\n"
        f"Parent cwd: {cwd_line}\n\n"
        "请用中文简短回复：\n"
        "1. 你是否看起来是一个新 fork 出来的会话。\n"
        "2. 你能看到的当前工作目录是什么。\n"
        "3. 你是否看到了父会话的上下文。\n\n"
        "不要运行 $review-validate-fix，不要修改文件。"
    )


def fork_review_validate_fix_prompt(
    parent_session_id: str,
    parent_cwd: str | None,
    repo: str,
) -> str:
    cwd_line = parent_cwd or "<unknown cwd>"
    return (
        "$review-validate-fix\n\n"
        f"{RVF_FORK_MARKER}\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CWD: {cwd_line}\n"
        f"RVF_TARGET_REPO: {repo}\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后 fork 出来的 "
        "review-validate-fix 会话。请基于完整父会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}\n\n"
        "如果父会话历史里出现 `RVF_STOP_HOOK: off`、`RVF_STOP_HOOK: on` "
        "或 `RVF_STOP_HOOK: status` 这样的行，请只把它们视为 Stop hook "
        "会话控制元数据；不要把它们当成用户分配的代码任务、review issue、"
        "research 对象或 scope-of-work 内容。\n\n"
        "完成后按 skill 规范生成最终汇总和 <handoff-context>。不要在正文里提示"
        "用户复制 handoff；Stop hook 会在本会话结束时用 systemMessage 做程序化提示。"
    )


def parse_marker_value(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


def rvf_fork_context(latest_user: str | None) -> dict[str, str] | None:
    if not latest_user or RVF_FORK_MARKER not in latest_user:
        return None
    parent_session_id = parse_marker_value(latest_user, "RVF_PARENT_SESSION_ID")
    parent_cwd = parse_marker_value(latest_user, "RVF_PARENT_CWD")
    target_repo = parse_marker_value(latest_user, "RVF_TARGET_REPO")
    if not parent_session_id or not parent_cwd or not target_repo:
        return None
    return {
        "parent_session_id": parent_session_id,
        "parent_cwd": parent_cwd,
        "target_repo": target_repo,
    }


def rvf_fork_context_from_event(event: dict[str, Any]) -> dict[str, str] | None:
    for path in event_session_paths(event):
        for message in user_messages_containing(path.expanduser(), RVF_FORK_MARKER):
            context = rvf_fork_context(message)
            if context is not None:
                return context
    return None


def handoff_advisory(
    event: dict[str, Any],
    context: dict[str, str],
    ledger: RunLedger | None = None,
) -> dict[str, Any] | None:
    last_assistant_message = event.get("last_assistant_message")
    if (
        not isinstance(last_assistant_message, str)
        or "<handoff-context>" not in last_assistant_message
        or "</handoff-context>" not in last_assistant_message
    ):
        return None

    session_id = session_id_from_event(event) or "unknown-session"
    sdir = state_dir()
    marker_path = sdir / f"{session_id}.handoff-advised"
    marker_written = False
    marker_error: dict[str, str] | None = None
    try:
        if marker_path.exists():
            return None

        sdir.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "context": context,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        marker_written = True
    except OSError as exc:
        marker_error = {
            "kind": "log_unavailable",
            "operation": "handoff_marker",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if ledger is not None:
            ledger._diagnose("handoff_marker", exc)

    parent = context.get("parent_session_id") or "<unknown>"
    parent_cwd = context.get("parent_cwd") or "<unknown>"
    target_repo = context.get("target_repo") or "<unknown>"
    message = (
        "review-validate-fix fork 已结束。请复制本 fork 会话最终回复中的 "
        "<handoff-context> 块，并粘贴回原始 chat session。"
    )
    if ledger is not None:
        ledger.event(
            phase="handoff",
            event="advisory_created" if marker_written else "advisory_marker_unavailable",
            status="completed" if marker_written else "warning",
            reason_code="handoff_context_ready" if marker_written else "log_unavailable",
            level="info" if marker_written else "warn",
            session_id=session_id,
            parent_thread_id=parent,
            paths={"marker": str(marker_path)},
            error=marker_error,
        )
        return ledger.hook_payload(
            status="handoff-advisory",
            reason_code="handoff_context_ready",
            message=message,
            parent_session_id=parent,
            parent_cwd=parent_cwd,
            target_repo=target_repo,
            marker_path=str(marker_path),
            marker_written=marker_written,
            marker_error=marker_error,
        )
    return {
        "continue": True,
        "systemMessage": (
            f"{message} parent_session_id={parent}; parent_cwd={parent_cwd}; "
            f"target_repo={target_repo}"
        ),
    }


def vibe_kanban_script_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    if value and value.strip():
        return Path(value).expanduser()
    return default


def vibe_kanban_issue_description(
    *,
    status: str,
    cwd: str | None,
    parent_session_id: str,
    parent_thread_path: Path | None,
    ledger: RunLedger,
    prompt_path: str | None,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    lines = [
        f"status: {status}",
        f"target repo: {cwd or '<unknown>'}",
        f"parent session id: {parent_session_id}",
        f"transcript path: {transcript}",
        f"run_dir: {ledger.run_dir}",
        f"events.jsonl: {ledger.events_path}",
        f"summary.json: {ledger.summary_path}",
        f"review-env.sh: {ledger.artifacts_dir / 'review-env.sh'}",
        f"review-agent-context.md: {ledger.artifacts_dir / 'review-agent-context.md'}",
        f"fork prompt: {prompt_path or '<unavailable>'}",
        "handoff-context: pending",
    ]
    return "\n".join(lines)


def startup_scope_text(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    ledger: RunLedger,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    return (
        "# Scope of Work: Vibe-Kanban headless RVF startup\n\n"
        "本文件由 Stop hook 在启动 headless runner 前生成，用于冻结 runner 启动时的 review 输入。\n\n"
        f"- 目标仓库：`{cwd}`\n"
        f"- parent session id：`{parent_session_id}`\n"
        f"- parent transcript path：`{transcript}`\n"
        f"- run id：`{ledger.run_id}`\n"
        f"- run dir：`{ledger.run_dir}`\n"
        f"- fork prompt：`{prompt_path}`\n\n"
        "headless 子进程必须以本 run artifacts 中已经生成的 review packet、session manifest "
        "和 workspace snapshot 作为启动时 scope anchor；不要在排队后用实时 worktree 重新定义 scope。"
    )


def freeze_vibe_kanban_startup_artifacts(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    scope_path = ledger.artifact(
        "headless-startup-scope-of-work.md",
        startup_scope_text(
            cwd=cwd,
            parent_session_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            prompt_path=prompt_path,
            ledger=ledger,
        ),
    )
    if not scope_path:
        raise RuntimeError("failed to write headless startup scope artifact")
    command = [
        sys.executable,
        str(DEFAULT_PREPARE_REVIEW_RUN),
        "--repo",
        cwd,
        "--session-context",
        scope_path,
        "--rvf-run-id",
        ledger.run_id,
        "--rvf-run-dir",
        str(ledger.run_dir),
    ]
    if parent_thread_path is not None:
        command.extend(["--transcript", str(parent_thread_path)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "vibe-kanban-startup-prepare-command.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "failed to freeze Vibe-Kanban startup review artifacts"
        )
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid startup prepare JSON: {completed.stdout!r}") from exc
    metadata_path = ledger.artifact("vibe-kanban-startup-prepare.json", metadata)
    ledger.event(
        phase="prepare",
        event="vibe_kanban_startup_artifacts_frozen",
        status="completed",
        reason_code="startup_artifacts_frozen",
        repo=cwd,
        cwd=cwd,
        paths={
            "metadata": metadata_path,
            "scope_of_work": metadata.get("scope_of_work_file"),
            "session_manifest": metadata.get("session_manifest_file"),
            "review_packet": metadata.get("review_packet"),
            "snapshot": metadata.get("before_workspace_snapshot"),
            "review_env": metadata.get("review_env_file"),
            "review_agent_context": metadata.get("review_agent_context_file"),
        },
    )
    return {"metadata_path": metadata_path, "metadata": metadata}


def create_vibe_kanban_issue(
    *,
    project_id: str,
    backend_url: str | None,
    title: str,
    description: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = vibe_kanban_script_path("CODEX_RVF_VK_MCP_CLIENT", DEFAULT_VIBE_KANBAN_MCP_CLIENT)
    mcp_cmd = os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_VIBE_KANBAN_MCP_CMD)
    command = [
        sys.executable,
        str(client),
        "create",
        "--mcp-cmd",
        mcp_cmd,
        "--project-id",
        project_id,
        "--title",
        title,
        "--description",
        description,
    ]
    if backend_url:
        command.extend(["--backend-url", backend_url])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "vibe-kanban-create-issue.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Vibe-Kanban issue creation failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Vibe-Kanban create issue JSON: {completed.stdout!r}") from exc
    issue_id = payload.get("issue_id") or payload.get("id")
    if not isinstance(issue_id, str) or not issue_id.strip():
        raise RuntimeError(f"Vibe-Kanban create issue response did not include issue_id: {payload!r}")
    return payload


def create_vibe_kanban_workspace(
    *,
    cwd: str,
    backend_url: str | None,
    title: str,
    description: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = vibe_kanban_script_path("CODEX_RVF_VK_MCP_CLIENT", DEFAULT_VIBE_KANBAN_MCP_CLIENT)
    mcp_cmd = os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_VIBE_KANBAN_MCP_CMD)
    start_cmd = os.environ.get("CODEX_RVF_VK_START_CMD", DEFAULT_VIBE_KANBAN_START_CMD)
    start_timeout = os.environ.get(
        "CODEX_RVF_VK_START_TIMEOUT",
        str(DEFAULT_VIBE_KANBAN_START_TIMEOUT_SECONDS),
    )
    tmux_session = os.environ.get("CODEX_RVF_VK_TMUX_SESSION", "rvf-vibe-kanban")
    command = [
        sys.executable,
        str(client),
        "create-workspace",
        "--mcp-cmd",
        mcp_cmd,
        "--start-cmd",
        start_cmd,
        "--start-timeout",
        start_timeout,
        "--tmux-session",
        tmux_session,
        "--repo",
        cwd,
        "--start-if-needed",
        "--title",
        title,
        "--description",
        description,
        "--status",
        "queued",
    ]
    if backend_url:
        command.extend(["--backend-url", backend_url])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "vibe-kanban-create-workspace.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Vibe-Kanban workspace creation failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Vibe-Kanban create workspace JSON: {completed.stdout!r}") from exc
    workspace_id = payload.get("workspace_id") or payload.get("workspaceId") or payload.get("id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise RuntimeError(f"Vibe-Kanban create workspace response did not include workspace_id: {payload!r}")
    payload["workspace_id"] = workspace_id
    return payload


def update_vibe_kanban_workspace(
    *,
    workspace_id: str,
    backend_url: str,
    title: str,
    description: str,
    status: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = vibe_kanban_script_path("CODEX_RVF_VK_MCP_CLIENT", DEFAULT_VIBE_KANBAN_MCP_CLIENT)
    command = [
        sys.executable,
        str(client),
        "update-workspace",
        "--workspace-id",
        workspace_id,
        "--backend-url",
        backend_url,
        "--title",
        title,
        "--description",
        description,
        "--status",
        status,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        f"vibe-kanban-update-workspace-{status}.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Vibe-Kanban workspace update failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Vibe-Kanban update workspace JSON: {completed.stdout!r}") from exc
    payload["workspace_id"] = payload.get("workspace_id") or payload.get("workspaceId") or payload.get("id") or workspace_id
    return payload


def update_vibe_kanban_issue(
    *,
    project_id: str,
    issue_id: str,
    backend_url: str | None,
    title: str,
    description: str,
    status: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = vibe_kanban_script_path("CODEX_RVF_VK_MCP_CLIENT", DEFAULT_VIBE_KANBAN_MCP_CLIENT)
    mcp_cmd = os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_VIBE_KANBAN_MCP_CMD)
    command = [
        sys.executable,
        str(client),
        "update",
        "--mcp-cmd",
        mcp_cmd,
        "--project-id",
        project_id,
        "--issue-id",
        issue_id,
        "--title",
        title,
        "--description",
        description,
        "--status",
        status,
    ]
    if backend_url:
        command.extend(["--backend-url", backend_url])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        f"vibe-kanban-update-issue-{status}.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Vibe-Kanban issue update failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Vibe-Kanban update issue JSON: {completed.stdout!r}") from exc
    payload["issue_id"] = payload.get("issue_id") or payload.get("id") or issue_id
    return payload


def resolve_vibe_kanban_project(
    *,
    cwd: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = vibe_kanban_script_path("CODEX_RVF_VK_MCP_CLIENT", DEFAULT_VIBE_KANBAN_MCP_CLIENT)
    mcp_cmd = os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_VIBE_KANBAN_MCP_CMD)
    start_cmd = os.environ.get("CODEX_RVF_VK_START_CMD", DEFAULT_VIBE_KANBAN_START_CMD)
    start_timeout = os.environ.get(
        "CODEX_RVF_VK_START_TIMEOUT",
        str(DEFAULT_VIBE_KANBAN_START_TIMEOUT_SECONDS),
    )
    tmux_session = os.environ.get("CODEX_RVF_VK_TMUX_SESSION", "rvf-vibe-kanban")
    command = [
        sys.executable,
        str(client),
        "resolve-project",
        "--mcp-cmd",
        mcp_cmd,
        "--start-cmd",
        start_cmd,
        "--start-timeout",
        start_timeout,
        "--tmux-session",
        tmux_session,
        "--repo",
        cwd,
        "--start-if-needed",
        "--create-if-missing",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "vibe-kanban-resolve-project.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Vibe-Kanban project resolution failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Vibe-Kanban resolve project JSON: {completed.stdout!r}") from exc
    project_id = payload.get("project_id") or payload.get("projectId") or payload.get("id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError(f"Vibe-Kanban project resolution did not include project_id: {payload!r}")
    payload["project_id"] = project_id
    return payload


def start_vibe_kanban_runner(
    *,
    cwd: str,
    prompt_path: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    ledger: RunLedger,
    project_id: str | None,
    issue_id: str | None,
    workspace_id: str | None,
    backend_url: str | None,
    issue_title: str,
    model: str | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    runner = vibe_kanban_script_path("CODEX_RVF_VK_RUNNER", DEFAULT_VIBE_KANBAN_RUNNER)
    startup_prepare = freeze_vibe_kanban_startup_artifacts(
        cwd=cwd,
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        prompt_path=prompt_path,
        ledger=ledger,
    )
    stdout_path = ledger.artifact_path("vibe-kanban-runner.stdout.txt")
    stderr_path = ledger.artifact_path("vibe-kanban-runner.stderr.txt")
    mcp_cmd = os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_VIBE_KANBAN_MCP_CMD)
    codex_exec_args = os.environ.get("CODEX_RVF_CODEX_EXEC_ARGS", DEFAULT_CODEX_EXEC_ARGS)
    command = [
        sys.executable,
        str(runner),
        "--repo",
        cwd,
        "--prompt-file",
        prompt_path,
        "--run-id",
        ledger.run_id,
        "--run-dir",
        str(ledger.run_dir),
        "--parent-session-id",
        parent_session_id,
        "--issue-title",
        issue_title,
        "--mcp-cmd",
        mcp_cmd,
        "--codex-exec-args",
        codex_exec_args,
    ]
    if startup_prepare.get("metadata_path"):
        command.extend(["--startup-prepare-metadata", str(startup_prepare["metadata_path"])])
    if project_id:
        command.extend(["--vibe-project-id", project_id])
    if issue_id:
        command.extend(["--vibe-issue-id", issue_id])
    if workspace_id:
        command.extend(["--vibe-workspace-id", workspace_id])
    if backend_url:
        command.extend(["--backend-url", backend_url])
    if parent_thread_path is not None:
        command.extend(["--parent-transcript-path", str(parent_thread_path)])
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--reasoning-effort", reasoning_effort])
    env = os.environ.copy()
    env.update(ledger.env())
    env["CODEX_RVF_SUPPRESS_STOP_HOOK"] = "1"
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=env,
            start_new_session=True,
        )
    return {
        "runner_pid": process.pid,
        "runner_command": command,
        "runner_stdout_path": str(stdout_path),
        "runner_stderr_path": str(stderr_path),
        "startup_prepare_metadata_path": startup_prepare.get("metadata_path"),
    }


def run_codex_fork(
    *,
    parent_session_id: str,
    cwd: str | None,
    prompt: str,
    log_prefix: str,
    mode_env_name: str = "CODEX_RVF_FORK_MODE",
    suppress_child_stop_hook: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
    parent_thread_path: Path | None = None,
    fallback_failure_reason: str | None = None,
    allow_desktop_unavailable_report: bool = True,
    ledger: RunLedger | None = None,
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE).strip().lower()
    ledger = ledger or start_run("stop-hook", repo=cwd, cwd=cwd)

    if not parent_session_id:
        return skip_payload(
            "Stop event did not expose a parent thread id.",
            ledger,
            "missing_parent_thread_id",
            log_prefix=log_prefix,
            cwd=cwd,
        )

    effective_prompt = prompt
    if suppress_child_stop_hook and SUPPRESS_STOP_HOOK_MARKER not in effective_prompt:
        effective_prompt = (
            f"{effective_prompt.rstrip()}\n\n"
            "Stop hook child-session metadata:\n"
            f"{SUPPRESS_STOP_HOOK_MARKER}\n"
            "当前 fork 结束时请跳过 review-validate-fix Stop hook。"
        )

    prompt_path = ledger.artifact("fork.prompt.txt", effective_prompt)
    ledger.event(
        phase="fork",
        event="started",
        status="started",
        reason_code="fork_started",
        parent_thread_id=parent_session_id,
        paths={"prompt": prompt_path} if prompt_path else {},
        mode=mode,
        log_prefix=log_prefix,
    )

    result: dict[str, Any] = {
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "cwd": cwd,
        "prompt_path": prompt_path,
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }

    if mode in {"manual", "prepare", "prepared", "log-only"}:
        result["status"] = "manual-prepared"
    elif mode == "dry-run":
        result["status"] = "dry-run"
        app_server_requests = app_server_fork_requests(
            parent_thread_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=effective_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        request_path = ledger.artifact("app-server-requests.json", app_server_requests)
        result["app_server_requests_path"] = request_path
    elif mode in {"vibe-kanban", "vibe-kanban-managed", "vk"}:
        project_id = os.environ.get("CODEX_RVF_VK_PROJECT_ID", "").strip()
        management_mode = os.environ.get("CODEX_RVF_VK_MANAGEMENT_MODE", "local-workspace").strip().lower()
        result["vibe_project_id"] = project_id or None
        result["vibe_management_mode"] = management_mode
        if not cwd:
            result.update(
                {
                    "status": "vibe-kanban-unconfigured",
                    "error": "CODEX_RVF_FORK_MODE=vibe-kanban requires a target repo cwd.",
                }
            )
        elif not prompt_path:
            result.update(
                {
                    "status": "vibe-kanban-unavailable",
                    "error": "fork prompt artifact is unavailable; Vibe-Kanban runner was not started.",
                }
            )
        elif management_mode not in {"local", "local-workspace", "workspace", "remote-project", "remote-issue", "project", "issue"}:
            result.update(
                {
                    "status": "vibe-kanban-unconfigured",
                    "error": (
                        f"Unsupported CODEX_RVF_VK_MANAGEMENT_MODE={management_mode!r}. "
                        "Use local-workspace or remote-project."
                    ),
                }
            )
        elif (
            management_mode in {"remote-project", "remote-issue", "project", "issue"}
            and not project_id
            and os.environ.get("CODEX_RVF_VK_PROJECT_AUTO", "1").strip().lower()
            in {"0", "false", "no", "off"}
        ):
            result.update(
                {
                    "status": "vibe-kanban-unconfigured",
                    "error": "CODEX_RVF_VK_PROJECT_ID is required when CODEX_RVF_VK_PROJECT_AUTO=0.",
                }
            )
        else:
            issue_title = f"RVF {Path(cwd).name} {ledger.run_id}"
            backend_url = os.environ.get("CODEX_RVF_VK_BACKEND_URL") or os.environ.get("VIBE_BACKEND_URL")
            created_workspace_id: str | None = None
            created_project_id: str | None = None
            created_issue_id: str | None = None
            try:
                if management_mode in {"local", "local-workspace", "workspace"}:
                    workspace_payload = create_vibe_kanban_workspace(
                        cwd=cwd,
                        backend_url=backend_url,
                        title=issue_title,
                        description=vibe_kanban_issue_description(
                            status="queued",
                            cwd=cwd,
                            parent_session_id=parent_session_id,
                            parent_thread_path=parent_thread_path,
                            ledger=ledger,
                            prompt_path=prompt_path,
                        ),
                        ledger=ledger,
                    )
                    workspace_id = str(workspace_payload["workspace_id"])
                    created_workspace_id = workspace_id
                    if isinstance(workspace_payload.get("backend_url"), str):
                        backend_url = str(workspace_payload["backend_url"])
                    if backend_url:
                        result["vibe_backend_url"] = backend_url
                    runner_payload = start_vibe_kanban_runner(
                        cwd=cwd,
                        prompt_path=prompt_path,
                        parent_session_id=parent_session_id,
                        parent_thread_path=parent_thread_path,
                        ledger=ledger,
                        project_id=None,
                        issue_id=None,
                        workspace_id=workspace_id,
                        backend_url=backend_url,
                        issue_title=issue_title,
                        model=model,
                        reasoning_effort=reasoning_effort,
                    )
                    result.update(
                        {
                            "status": "vibe-kanban-started",
                            "issue_title": issue_title,
                            "vibe_workspace_id": workspace_id,
                            "vibe_backend_url": backend_url,
                            "vibe_workspace": workspace_payload,
                            **runner_payload,
                        }
                    )
                else:
                    if not project_id:
                        project_resolution = resolve_vibe_kanban_project(cwd=cwd, ledger=ledger)
                        project_id = str(project_resolution["project_id"])
                        bootstrap = project_resolution.get("bootstrap")
                        if isinstance(bootstrap, dict) and isinstance(bootstrap.get("backend_url"), str):
                            backend_url = str(bootstrap["backend_url"])
                        result["vibe_project_id"] = project_id
                        if backend_url:
                            result["vibe_backend_url"] = backend_url
                        result["vibe_project_resolution"] = project_resolution
                    created_project_id = project_id
                    issue_payload = create_vibe_kanban_issue(
                        project_id=project_id,
                        backend_url=backend_url,
                        title=issue_title,
                        description=vibe_kanban_issue_description(
                            status="queued",
                            cwd=cwd,
                            parent_session_id=parent_session_id,
                            parent_thread_path=parent_thread_path,
                            ledger=ledger,
                            prompt_path=prompt_path,
                        ),
                        ledger=ledger,
                    )
                    issue_id = str(issue_payload["issue_id"])
                    created_issue_id = issue_id
                    runner_payload = start_vibe_kanban_runner(
                        cwd=cwd,
                        prompt_path=prompt_path,
                        parent_session_id=parent_session_id,
                        parent_thread_path=parent_thread_path,
                        ledger=ledger,
                        project_id=project_id,
                        issue_id=issue_id,
                        workspace_id=None,
                        backend_url=backend_url,
                        issue_title=issue_title,
                        model=model,
                        reasoning_effort=reasoning_effort,
                    )
                    result.update(
                        {
                            "status": "vibe-kanban-started",
                            "issue_title": issue_title,
                            "vibe_issue_id": issue_id,
                            "vibe_backend_url": backend_url,
                            "vibe_issue": issue_payload,
                            **runner_payload,
                        }
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failure_update: dict[str, Any] = {}
                failed_description = (
                    vibe_kanban_issue_description(
                        status="failed",
                        cwd=cwd,
                        parent_session_id=parent_session_id,
                        parent_thread_path=parent_thread_path,
                        ledger=ledger,
                        prompt_path=prompt_path,
                    )
                    + f"\nerror: {error}"
                )
                if created_workspace_id and backend_url:
                    failure_update["vibe_workspace_id"] = created_workspace_id
                    failure_update["vibe_backend_url"] = backend_url
                    try:
                        failure_update["vibe_workspace_failed_update"] = update_vibe_kanban_workspace(
                            workspace_id=created_workspace_id,
                            backend_url=backend_url,
                            title=issue_title,
                            description=failed_description,
                            status="failed",
                            ledger=ledger,
                        )
                    except Exception as update_exc:
                        failure_update["vibe_failure_update_error"] = f"{type(update_exc).__name__}: {update_exc}"
                elif created_project_id and created_issue_id:
                    failure_update["vibe_project_id"] = created_project_id
                    failure_update["vibe_issue_id"] = created_issue_id
                    if backend_url:
                        failure_update["vibe_backend_url"] = backend_url
                    try:
                        failure_update["vibe_issue_failed_update"] = update_vibe_kanban_issue(
                            project_id=created_project_id,
                            issue_id=created_issue_id,
                            backend_url=backend_url,
                            title=issue_title,
                            description=failed_description,
                            status="failed",
                            ledger=ledger,
                        )
                    except Exception as update_exc:
                        failure_update["vibe_failure_update_error"] = f"{type(update_exc).__name__}: {update_exc}"
                result.update(
                    {
                        "status": "vibe-kanban-unavailable",
                        "error": error,
                        **failure_update,
                    }
                )
    elif mode in {"gui", "app-server", "appserver", "auto"}:
        try:
            result.update(
                run_app_server_fork(
                    parent_thread_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    cwd=cwd,
                    prompt=effective_prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    log_path=ledger.summary_path,
                )
            )
        except Exception as exc:
            failure: dict[str, Any] = {
                "status": "app-server-failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            socket_selection = getattr(exc, "socket_selection", None)
            if isinstance(socket_selection, dict):
                failure["socket_selection"] = socket_selection
                bridge_policy = socket_selection.get("bridge_policy")
                if bridge_policy == "report":
                    if allow_desktop_unavailable_report:
                        failure["status"] = "desktop-control-unavailable-report"
                        failure["report_reason"] = (
                            fallback_failure_reason
                            or "Codex Desktop control socket unavailable; GUI fork was not created."
                        )
                    else:
                        failure["status"] = "manual-prepared"
                        failure["desktop_control_unavailable_fallback"] = "manual"
                elif bridge_policy == "manual":
                    failure["status"] = "manual-prepared"
                elif bridge_policy == "fail":
                    failure["status"] = "desktop-control-unavailable-fail"
                    failure["report_reason"] = failure["error"]
            result.update(failure)
    else:
        result.update(
            {
                "status": "unsupported-mode",
                "error": (
                    f"Unsupported {mode_env_name}={mode!r}. Use gui, vibe-kanban, dry-run, "
                    "or manual. Terminal/CLI fork launch is intentionally disabled."
                ),
            }
        )

    status = result.get("status", "unknown")
    reason_code = str(status).replace("_", "-")
    if status == "desktop-control-unavailable-report":
        reason_code = "desktop_control_unavailable_continuation_disabled"
    elif status == "desktop-control-unavailable-fail":
        reason_code = "desktop_control_unavailable_fail_policy"
    elif status == "app-server-failed":
        reason_code = "app_server_fork_failed"
    elif status == "manual-prepared":
        reason_code = "manual_prepared"
    elif status == "dry-run":
        reason_code = "dry_run"
    elif status == "app-server-started":
        reason_code = "fork_started"
    elif status == "vibe-kanban-started":
        reason_code = "vibe_kanban_runner_started"
    elif status == "vibe-kanban-unconfigured":
        reason_code = "vibe_kanban_unconfigured"
    elif status == "vibe-kanban-unavailable":
        reason_code = "vibe_kanban_unavailable"

    event_paths: dict[str, Any] = {}
    if prompt_path:
        event_paths["prompt"] = prompt_path
    if result.get("app_server_requests_path"):
        event_paths["app_server_requests"] = result["app_server_requests_path"]
    if result.get("runner_stdout_path"):
        event_paths["runner_stdout"] = result["runner_stdout_path"]
    if result.get("runner_stderr_path"):
        event_paths["runner_stderr"] = result["runner_stderr_path"]
    if status == "app-server-started":
        ledger.event(
            phase="fork",
            event="completed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            fork_thread_id=result.get("fork_thread_id") if isinstance(result.get("fork_thread_id"), str) else None,
            paths=event_paths,
            socket_source=result.get("socket_source"),
            gui_visibility=result.get("gui_visibility"),
        )
    elif status == "vibe-kanban-started":
        ledger.event(
            phase="fork",
            event="completed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            mode=mode,
            vibe_management_mode=result.get("vibe_management_mode"),
            vibe_issue_id=result.get("vibe_issue_id"),
            vibe_workspace_id=result.get("vibe_workspace_id"),
            runner_pid=result.get("runner_pid"),
            runner_command=result.get("runner_command"),
        )
    elif status in {"dry-run", "manual-prepared"}:
        ledger.event(
            phase="fork",
            event="prepared",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            mode=mode,
        )
    else:
        ledger.event(
            phase="fork",
            event="failed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            error=result.get("error") or result.get("report_reason"),
        )

    if status == "manual-prepared":
        message = (
            "manual fork prompt prepared; no Terminal was launched and no "
            "current-chat continuation was submitted."
        )
    elif status == "app-server-started":
        message = "Codex GUI/app-server fork was started."
    elif status == "vibe-kanban-started":
        message = "Vibe-Kanban managed RVF runner was started."
    elif status == "vibe-kanban-unconfigured":
        message = str(result.get("error") or "Vibe-Kanban RVF mode is not configured.")
    elif status == "vibe-kanban-unavailable":
        message = str(result.get("error") or "Vibe-Kanban management plane is unavailable; runner was not started.")
    elif status in {"desktop-control-unavailable-report", "desktop-control-unavailable-fail"}:
        report_reason = result.get("report_reason")
        message = report_reason if isinstance(report_reason, str) else "Codex GUI fork unavailable."
    else:
        message = f"{log_prefix} triggered: {status}."

    summary_fields = dict(result)
    summary_fields.pop("status", None)
    if extra_summary:
        summary_fields.update(extra_summary)
    return ledger.hook_payload(
        status=str(status),
        reason_code=reason_code,
        message=message,
        **summary_fields,
    )


def app_server_fork_requests(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
) -> list[dict[str, Any]]:
    fork_params: dict[str, Any] = {
        "threadId": parent_thread_id,
        "cwd": cwd,
        "excludeTurns": True,
        "persistExtendedHistory": True,
    }
    if parent_thread_path is not None:
        fork_params["path"] = str(parent_thread_path)
    if model:
        fork_params["model"] = model

    turn_params: dict[str, Any] = {
        "threadId": "<fork_thread_id>",
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": cwd,
        "summary": "auto",
        "personality": None,
        "outputSchema": None,
    }
    if model:
        turn_params["model"] = model
    if reasoning_effort:
        turn_params["effort"] = reasoning_effort

    return [
        {"method": "thread/fork", "params": fork_params},
        {"method": "turn/start", "params": turn_params},
    ]


class AppServerWebSocket:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(15)
        self.socket.connect(str(socket_path))
        self.next_id = 1
        self.notifications: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        elif len(data) < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(data))
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", len(data))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.socket.sendall(header + mask + masked)

    def recv_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.socket.recv(remaining)
            if not chunk:
                raise AppServerError("app-server websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_json(self) -> dict[str, Any]:
        first, second = self.recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.recv_exact(8))[0]

        mask = self.recv_exact(4) if second & 0x80 else None
        payload = self.recv_exact(length)
        if mask is not None:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        if opcode == 0x8:
            raise AppServerError("app-server websocket closed")
        if opcode == 0x9:
            self.send_pong(payload)
            return self.recv_json()
        if opcode != 0x1:
            raise AppServerError(f"unsupported websocket opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def send_pong(self, payload: bytes) -> None:
        if len(payload) >= 126:
            return
        self.socket.sendall(bytes([0x8A, len(payload)]) + payload)

    def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_json(payload)
        while True:
            response = self.recv_json()
            if response.get("id") != request_id:
                self.notifications.append(response)
                continue
            error = response.get("error")
            if error:
                raise AppServerError(json.dumps(error, ensure_ascii=False))
            result = response.get("result")
            return result if isinstance(result, dict) else {}


def can_connect_app_server_socket(socket_path: Path) -> bool:
    return bool(probe_app_server_socket(socket_path).get("connect_ok"))


def probe_app_server_socket(socket_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(socket_path),
        "exists": socket_path.exists(),
        "parent_exists": socket_path.parent.exists(),
        "is_socket": False,
        "connect_ok": False,
        "reason": None,
    }
    try:
        if socket_path.exists():
            result["is_socket"] = socket_path.is_socket()
    except OSError as exc:
        result.update(
            {
                "reason": "stat-error",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result

    if not result["exists"]:
        result["reason"] = "missing"
        return result
    if not result["is_socket"]:
        result["reason"] = "not-a-socket"
        return result

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(socket_path))
        result["connect_ok"] = True
        result["reason"] = "connect-ok"
        return result
    except OSError as exc:
        result.update(
            {
                "reason": "connect-failed",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result
    finally:
        probe.close()


def bridge_socket_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_SOCKET")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_SOCKET.resolve()


def bridge_log_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_LOG")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_LOG.resolve()


def select_app_server_socket() -> tuple[Path, str, dict[str, Any]]:
    explicit = os.environ.get("CODEX_RVF_APP_SERVER_SOCKET")
    if explicit and explicit.strip():
        socket_path = Path(explicit).expanduser().resolve()
        return socket_path, "explicit", {"explicit": probe_app_server_socket(socket_path)}

    desktop_probe = probe_app_server_socket(DEFAULT_APP_SERVER_CONTROL_SOCKET)
    if desktop_probe.get("connect_ok"):
        return DEFAULT_APP_SERVER_CONTROL_SOCKET, "desktop-control", {
            "desktop_control": desktop_probe,
        }

    bridge_policy = bridge_gui_unverified_policy()
    if bridge_policy != "bridge":
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
            "bridge_policy": bridge_policy,
        }
        raise AppServerSocketSelectionError(
            "desktop-control unavailable; bridge fallback disabled by "
            f"CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY={bridge_policy}",
            socket_selection,
        )

    try:
        socket_path = ensure_bridge_app_server()
    except Exception as exc:
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
        }
        raise AppServerSocketSelectionError(
            f"desktop-control unavailable and bridge fallback failed: {exc}",
            socket_selection,
        ) from exc
    return socket_path, "bridge", {
        "desktop_control": desktop_probe,
        "bridge": probe_app_server_socket(socket_path),
        "bridge_policy": bridge_policy,
    }


def bridge_gui_unverified_policy() -> str:
    if is_truthy(os.environ.get("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER")):
        return "bridge"
    raw = os.environ.get(
        "CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY",
        DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY,
    )
    value = raw.strip().lower() if raw else DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY
    if value in {"bridge", "allow", "allowed", "fork", "app-server", "appserver"}:
        return "bridge"
    if value in {"manual", "prepare", "prepared", "log-only"}:
        return "manual"
    if value in {"fail", "error"}:
        return "fail"
    return "report"


def ensure_bridge_app_server() -> Path:
    socket_path = bridge_socket_path()
    if socket_path.exists() and can_connect_app_server_socket(socket_path):
        return socket_path

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    log_path = bridge_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    codex_bin = os.environ.get("CODEX_RVF_CODEX_BIN", "codex")
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [
                codex_bin,
                "app-server",
                "--listen",
                f"unix://{socket_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if socket_path.exists() and can_connect_app_server_socket(socket_path):
            return socket_path
        time.sleep(0.1)
    raise AppServerError(f"app-server bridge socket did not become ready: {socket_path}")


def maybe_open_fork_in_codex(fork_thread_id: str) -> bool:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if sys.platform != "darwin":
        return False
    url = f"codex://local/{fork_thread_id}"
    try:
        subprocess.Popen(
            ["open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def open_gui_fork_unavailable_reason() -> str | None:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return "disabled"
    if sys.platform != "darwin":
        return "unsupported-platform"
    return None


def open_gui_fork_attempts() -> int:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS


def open_gui_fork_retry_delay_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS


def open_fork_in_codex_with_retries(fork_thread_id: str) -> dict[str, Any]:
    max_attempts = open_gui_fork_attempts()
    retry_delay = open_gui_fork_retry_delay_seconds()
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()
    unavailable_reason = open_gui_fork_unavailable_reason()
    if unavailable_reason is not None:
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": 1,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        return {
            "opened": opened,
            "attempts": attempts,
            "retry_delay_seconds": retry_delay,
            "skipped_retries_reason": unavailable_reason,
        }
    for attempt in range(1, max_attempts + 1):
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": attempt,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if opened:
            break
        if attempt < max_attempts:
            time.sleep(retry_delay)
    return {
        "opened": any(item["opened"] for item in attempts),
        "attempts": attempts,
        "retry_delay_seconds": retry_delay,
    }


def fork_visibility_timeout_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def notification_thread_path(
    notifications: list[dict[str, Any]],
    thread_id: str,
) -> str | None:
    for notification in reversed(notifications):
        if notification.get("method") != "thread/started":
            continue
        params = notification.get("params")
        thread = params.get("thread") if isinstance(params, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            continue
        path = thread.get("path")
        if isinstance(path, str) and path:
            return path
    return None


def fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
) -> dict[str, Any]:
    active_paths: list[str] = []
    hinted = Path(hinted_path).expanduser() if hinted_path else None
    hinted_exists = False
    if hinted is not None:
        try:
            hinted_exists = hinted.exists()
        except OSError:
            hinted_exists = False
        if hinted_exists and path_is_relative_to(hinted, DEFAULT_CODEX_SESSIONS_DIR):
            active_paths.append(str(hinted))

    if not active_paths and DEFAULT_CODEX_SESSIONS_DIR.exists():
        active_paths.extend(
            str(path)
            for path in DEFAULT_CODEX_SESSIONS_DIR.rglob(f"*{thread_id}*.jsonl")
        )

    location = "active" if active_paths else "missing"

    return {
        "thread_id": thread_id,
        "hinted_path": str(hinted) if hinted is not None else None,
        "hinted_exists": hinted_exists,
        "location": location,
        "active_paths": active_paths,
    }


def wait_for_fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    timeout = fork_visibility_timeout_seconds() if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    checks = 0
    while True:
        checks += 1
        visibility = fork_session_visibility(thread_id, hinted_path)
        visibility["checks"] = checks
        visibility["timeout_seconds"] = timeout
        if visibility["location"] != "missing" or time.monotonic() >= deadline:
            return visibility
        time.sleep(0.1)


def compact_app_server_thread(thread: dict[str, Any]) -> dict[str, Any]:
    status = thread.get("status")
    return {
        "id": thread.get("id"),
        "name": thread.get("name"),
        "path": thread.get("path"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "status": status if isinstance(status, dict) else None,
    }


def request_app_server_diagnostic(
    client: AppServerWebSocket,
    method: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        return {"ok": True, "result": client.request(method, params)}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def app_server_thread_visibility_diagnostics(
    client: AppServerWebSocket,
    thread_id: str,
    cwd: str | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"thread_id": thread_id}

    read_probe = request_app_server_diagnostic(
        client,
        "thread/read",
        {"threadId": thread_id, "includeTurns": False},
    )
    if read_probe.get("ok"):
        result = read_probe.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        read_probe = {
            "ok": True,
            "contains_thread": isinstance(thread, dict) and thread.get("id") == thread_id,
            "thread": compact_app_server_thread(thread) if isinstance(thread, dict) else None,
        }
    diagnostics["thread_read"] = read_probe

    list_params: dict[str, Any] = {
        "limit": 50,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "archived": False,
        "useStateDbOnly": False,
    }
    if cwd:
        list_params["cwd"] = cwd
    list_probe = request_app_server_diagnostic(client, "thread/list", list_params)
    if list_probe.get("ok"):
        result = list_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        threads = data if isinstance(data, list) else []
        matches = [
            compact_app_server_thread(thread)
            for thread in threads
            if isinstance(thread, dict) and thread.get("id") == thread_id
        ]
        list_probe = {
            "ok": True,
            "params": list_params,
            "contains_thread": bool(matches),
            "matches": matches,
            "returned": len(threads),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_list"] = list_probe

    loaded_probe = request_app_server_diagnostic(
        client,
        "thread/loaded/list",
        {"limit": 200},
    )
    if loaded_probe.get("ok"):
        result = loaded_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        loaded_ids = (
            [item for item in data if isinstance(item, str)]
            if isinstance(data, list)
            else []
        )
        loaded_probe = {
            "ok": True,
            "contains_thread": thread_id in loaded_ids,
            "returned": len(loaded_ids),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_loaded_list"] = loaded_probe

    return diagnostics


def run_app_server_fork(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    log_path: Path,
) -> dict[str, Any]:
    socket_path, socket_source, socket_selection = select_app_server_socket()
    client = AppServerWebSocket(socket_path)
    try:
        client.request(
            "initialize",
            {
                "clientInfo": APP_SERVER_CLIENT_INFO,
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [],
                },
            },
        )
        requests = app_server_fork_requests(
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        fork_result = client.request("thread/fork", requests[0]["params"])
        fork_thread = fork_result.get("thread")
        if not isinstance(fork_thread, dict) or not isinstance(fork_thread.get("id"), str):
            raise AppServerError("thread/fork did not return a fork thread id")
        fork_thread_id = fork_thread["id"]
        fork_thread_path = (
            fork_thread.get("path") if isinstance(fork_thread.get("path"), str) else None
        )
        turn_params = dict(requests[1]["params"])
        turn_params["threadId"] = fork_thread_id
        turn_result = client.request("turn/start", turn_params)
        turn = turn_result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        session_hint = fork_thread_path or notification_thread_path(
            client.notifications,
            fork_thread_id,
        )
        session_visibility = wait_for_fork_session_visibility(fork_thread_id, session_hint)
        app_server_visibility = app_server_thread_visibility_diagnostics(
            client,
            fork_thread_id,
            cwd,
        )
        open_result = open_fork_in_codex_with_retries(fork_thread_id)
        session_location = session_visibility.get("location")
        gui_visibility = "unverified-bridge-only"
        if socket_source == "desktop-control":
            gui_visibility = (
                "verified"
                if session_location == "active"
                else f"unverified-session-{session_location or 'unknown'}"
            )
        return {
            "status": "app-server-started",
            "socket_path": str(socket_path),
            "socket_source": socket_source,
            "socket_selection": socket_selection,
            "fork_thread_id": fork_thread_id,
            "fork_thread_path": fork_thread_path,
            "turn_id": turn_id,
            "session_visibility": session_visibility,
            "app_server_visibility": app_server_visibility,
            "gui_visibility": gui_visibility,
            "opened_gui_deeplink": open_result["opened"],
            "open_gui_deeplink": open_result,
            "notifications": client.notifications[-20:],
        }
    finally:
        client.close()


def run_fork_experiment(
    event: dict[str, Any],
    latest_user: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any]:
    session_id = parent_thread_id_from_event(event)
    session_path = parent_thread_path_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    prompt = fork_experiment_prompt(session_id or "", cwd)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    payload = run_codex_fork(
        parent_session_id=session_id or "",
        cwd=cwd,
        prompt=prompt,
        log_prefix="fork-experiment",
        mode_env_name="CODEX_RVF_FORK_EXPERIMENT_MODE",
        suppress_child_stop_hook=True,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=session_path,
        allow_desktop_unavailable_report=False,
        ledger=ledger,
        extra_summary={
            "marker": os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER),
            "latest_user_message_path": (
                ledger.artifact("latest-user-message.txt", latest_user)
                if ledger is not None
                else None
            ),
        },
    )
    return payload


def should_run_fork_experiment(event: dict[str, Any]) -> tuple[bool, str | None]:
    marker = os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER)
    latest_user = latest_user_message_from_event(event)
    if latest_user and marker in latest_user:
        return True, latest_user
    return False, latest_user


def rvf_mode() -> str:
    mode = os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE).strip().lower()
    if mode in {"continuation", "continue", "block"}:
        return "report"
    if mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    return "fork"


def fork_cwd_for_event(event: dict[str, Any], repo: str) -> str:
    cwd_value = event.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value.strip():
        return repo

    try:
        cwd_path = Path(cwd_value).expanduser().resolve()
        repo_path = Path(repo).expanduser().resolve()
    except OSError:
        return repo

    if cwd_path == repo_path or path_is_relative_to(cwd_path, repo_path):
        return str(cwd_path)
    return repo


def fork_review_validate_fix(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any]:
    parent_session_id = parent_thread_id_from_event(event) or ""
    parent_thread_path = parent_thread_path_from_event(event)
    cwd = fork_cwd_for_event(event, repo)
    prompt = fork_review_validate_fix_prompt(parent_session_id, cwd, repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=parent_session_id,
        cwd=cwd,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=parent_thread_path,
        fallback_failure_reason=fork_failure_report(repo),
        ledger=ledger,
    )


def review_validate_fix_dispatch(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any] | None:
    mode = rvf_mode()
    if mode == "off":
        return skip_payload(
            "CODEX_RVF_MODE=off",
            ledger,
            "mode_off",
            repo=repo,
        )
    if mode == "report":
        report = fork_failure_report(repo)
        if ledger is not None:
            ledger.event(
                phase="fork",
                event="skipped",
                status="skipped",
                reason_code="continuation_disabled",
                repo=repo,
                message=report,
            )
            return ledger.hook_payload(
                status="skipped",
                reason_code="continuation_disabled",
                message=report,
                repo=repo,
            )
        return {"continue": True, "systemMessage": report}
    return fork_review_validate_fix(event, repo, ledger)


def should_suppress(event: dict[str, Any], latest_user: str | None = None) -> bool:
    if explicit_suppress_requested(event, latest_user):
        return True

    if source_marks_subagent(event.get("source")):
        return True

    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def explicit_suppress_requested(event: dict[str, Any], latest_user: str | None = None) -> bool:
    if any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES):
        return True

    if latest_user and SUPPRESS_STOP_HOOK_MARKER in latest_user:
        return True

    if event.get("suppress_review_validate_fix") is True:
        return True
    if event.get("review_validate_fix_suppressed") is True:
        return True

    return False


def suppressed_without_ledger_payload() -> dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": "review-validate-fix: skipped; reason=suppressed",
    }


def event_marks_subagent(event: dict[str, Any]) -> bool:
    if source_marks_subagent(event.get("source")):
        return True
    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def run_gate(repo: str) -> GateResult:
    gate = Path(os.environ.get("CODEX_RVF_GATE", str(DEFAULT_GATE)))
    try:
        completed = subprocess.run(
            ["bash", str(gate), repo],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return GateResult("ERROR", None, str(exc))

    output = completed.stdout.strip()
    first_line = output.splitlines()[0] if output else ""
    parts = first_line.split(maxsplit=1)
    status = parts[0] if parts else "ERROR"
    resolved_repo = parts[1] if len(parts) > 1 else None
    return GateResult(status, resolved_repo, output)


def fork_failure_report(repo: str) -> str:
    return (
        "review-validate-fix Stop hook 未运行：无法创建 Codex GUI fork，"
        "且 Stop continuation prompt 已禁用，因为它不会创建真正的新用户 prompt，"
        "只会作为 hook system context 出现在当前轨迹中。"
        f" target_repo={repo}。请检查 Codex Desktop control socket / app-server fork 能力；"
        "修复前需要用户手动触发 $review-validate-fix。"
    )


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    latest_user = latest_user_message_from_event(event)
    if explicit_suppress_requested(event, latest_user) and parse_session_hook_control(latest_user) is None:
        emit(suppressed_without_ledger_payload())
        return 0

    cwd_value = event.get("cwd")
    ledger = start_run(
        "stop-hook",
        repo=str(cwd_value) if isinstance(cwd_value, str) else None,
        cwd=str(cwd_value) if isinstance(cwd_value, str) else None,
    )
    stop_event_path = ledger.artifact("stop-event.json", event)
    ledger.event(
        phase="gate",
        event="stop_event_received",
        status="started",
        reason_code="stop_event_received",
        session_id=session_id_from_event(event),
        paths={"stop_event": stop_event_path} if stop_event_path else {},
    )

    if event.get("stop_hook_active") is True:
        emit(
            skip_payload(
                "检测到 stop_hook_active=true，为避免递归已跳过。",
                ledger,
                "stop_hook_active",
                detail="Codex 已在执行 Stop hook，RVF 跳过以避免递归",
            )
        )
        return 0

    fork_context = rvf_fork_context(latest_user) or rvf_fork_context_from_event(event)
    if fork_context is not None:
        advisory = handoff_advisory(event, fork_context, ledger)
        if advisory is not None:
            emit(advisory)
        else:
            emit(
                skip_payload(
                    "当前会话已是 review-validate-fix fork，会等待最终 <handoff-context>，不会再次 fork。",
                    ledger,
                    "already_rvf_fork",
                )
            )
        return 0

    if event_marks_subagent(event):
        emit(
            skip_payload(
                "Stop event 来自 Codex subagent，post-work review 只允许主会话触发。",
                ledger,
                "subagent_stop_event",
            )
        )
        return 0

    session_control = session_hook_control_payload(event, latest_user)
    if session_control is not None:
        session_control_reason = (
            session_control.get("reason_code")
            if isinstance(session_control.get("reason_code"), str)
            else "session_hook_control"
        )
        session_control_message = (
            session_control.get("systemMessage")
            if isinstance(session_control.get("systemMessage"), str)
            else None
        )
        ledger.event(
            phase="gate",
            event="session_hook_control",
            status="completed",
            reason_code=session_control_reason,
            session_id=session_hook_id_from_event(event),
            control_action=session_control.get("control_action"),
            session_hook_gate_state=session_control.get("session_hook_gate_state"),
            state_path=session_control.get("state_path"),
        )
        ledger.summary(
            status="session-hook-control",
            reason_code=session_control_reason,
            message=session_control_message,
            session_id=session_hook_id_from_event(event),
            control_action=session_control.get("control_action"),
            session_hook_gate_state=session_control.get("session_hook_gate_state"),
            state_path=session_control.get("state_path"),
        )
        session_control = ledger.hook_payload(
            status="session-hook-control",
            reason_code=session_control_reason,
            message=session_control_message,
            session_id=session_hook_id_from_event(event),
            control_action=session_control.get("control_action"),
            session_hook_gate_state=session_control.get("session_hook_gate_state"),
            state_path=session_control.get("state_path"),
        )
        emit(session_control)
        return 0

    session_id = session_hook_id_from_event(event)
    if session_id and session_hook_disabled(session_id):
        emit(
            skip_payload(
                "当前 chat session 已禁用 RVF_STOP_HOOK；"
                "只跳过 RVF fork/continuation/review gate，"
                f"不控制 dispatcher 的 dev sync。session_id={session_id}",
                ledger,
                "session_hook_disabled",
                session_id=session_id,
            )
        )
        return 0

    should_experiment, latest_user = should_run_fork_experiment(event)
    if should_experiment and latest_user is not None:
        emit(run_fork_experiment(event, latest_user, ledger))
        return 0

    if should_suppress(event, latest_user):
        emit(skip_payload("检测到 suppress 标记或环境变量。", ledger, "suppressed"))
        return 0

    cwd = event.get("cwd")
    cwd_result: GateResult | None = None
    if isinstance(cwd, str) and cwd:
        cwd_result = run_gate(cwd)
        ledger.event(
            phase="gate",
            event="dirty_gate_completed",
            status=cwd_result.status.lower(),
            reason_code=f"gate_{cwd_result.status.lower()}",
            repo=cwd_result.repo,
            cwd=cwd,
            gate_output_path=ledger.artifact("gate-output.txt", cwd_result.output) if cwd_result.output else None,
        )
        if cwd_result.status == "DIRTY" and cwd_result.repo:
            payload = review_validate_fix_dispatch(event, cwd_result.repo, ledger)
            if payload is not None:
                emit(payload)
            return 0
        if cwd_result.status == "CLEAN":
            emit(
                skip_payload(
                    f"当前 cwd 仓库是 clean。repo={cwd_result.repo or cwd}",
                    ledger,
                    "clean_repo",
                    repo=cwd_result.repo or cwd,
                )
            )
            return 0

    if cwd_result is not None:
        emit(
            skip_payload(
                "当前 cwd 不在 git repo/worktree 内，未自动选择目标仓库。"
                f"cwd gate={cwd_result.status}; cwd={cwd}。"
                "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
                ledger,
                "cwd_not_git_repo",
                cwd=cwd,
                gate_status=cwd_result.status,
            )
        )
    else:
        emit(
            skip_payload(
                "Stop event 未提供可检查的 cwd，未自动选择目标仓库。"
                "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
                ledger,
                "missing_cwd",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
