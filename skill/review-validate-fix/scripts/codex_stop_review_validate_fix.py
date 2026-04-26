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


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = SKILL_DIR / "state" / "fork-experiment"
DEFAULT_APP_SERVER_CONTROL_SOCKET = (
    Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
)
DEFAULT_BRIDGE_SOCKET = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.sock"
DEFAULT_BRIDGE_LOG = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.log"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_CODEX_ARCHIVED_SESSIONS_DIR = Path.home() / ".codex" / "archived_sessions"
DEFAULT_SESSION_HOOK_STATE_DIR = SKILL_DIR / "state" / "session-hook"
DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS = 8.0
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
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


def skip_payload(reason: str) -> dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": f"review-validate-fix Stop hook 未创建 fork：{reason}",
    }


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_RVF_STATE_DIR", str(DEFAULT_STATE_DIR)))


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
            "systemMessage": (
                "review-validate-fix Stop hook 无法更新当前 chat session 状态："
                "Stop event 未暴露 session id。"
            ),
        }

    if action == "status":
        status = "disabled" if session_hook_disabled(session_id) else "enabled"
        return {
            "continue": True,
            "systemMessage": (
                "review-validate-fix Stop hook 当前 chat session 状态："
                f"{status}。session_id={session_id}"
            ),
        }

    enabled = action == "on"
    state_path = set_session_hook_enabled(
        session_id=session_id,
        enabled=enabled,
        latest_user=latest_user,
    )
    status = "enabled" if enabled else "disabled"
    return {
        "continue": True,
        "systemMessage": (
            "review-validate-fix Stop hook 已为当前 chat session 设置为 "
            f"{status}。session_id={session_id}; state={state_path}"
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


def handoff_advisory(event: dict[str, Any], context: dict[str, str]) -> dict[str, Any] | None:
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

    parent = context.get("parent_session_id") or "<unknown>"
    parent_cwd = context.get("parent_cwd") or "<unknown>"
    target_repo = context.get("target_repo") or "<unknown>"
    return {
        "continue": True,
        "systemMessage": (
            "review-validate-fix fork 已结束。请复制本 fork 会话最终回复中的 "
            "<handoff-context> 块，并粘贴回原始 chat session。"
            f" parent_session_id={parent}; parent_cwd={parent_cwd}; target_repo={target_repo}"
        ),
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
) -> dict[str, Any]:
    mode = os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE).strip().lower()

    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = sdir / f"{timestamp}.{log_prefix}.json"
    latest_path = sdir / "latest.json"
    prompt_path = sdir / f"{timestamp}.{log_prefix}.prompt.txt"

    if not parent_session_id:
        payload = {
            "continue": True,
            "systemMessage": (
                f"{log_prefix} skipped: Stop event did not expose a parent thread id."
            ),
        }
        log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    prompt_path.write_text(prompt, encoding="utf-8")

    result: dict[str, Any] = {
        "timestamp": timestamp,
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "cwd": cwd,
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }

    if mode in {"manual", "prepare", "prepared", "log-only"}:
        result["status"] = "manual-prepared"
    elif mode == "dry-run":
        result["status"] = "dry-run"
        result["app_server_requests"] = app_server_fork_requests(
            parent_thread_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif mode in {"gui", "app-server", "appserver", "auto"}:
        try:
            result.update(
                run_app_server_fork(
                    parent_thread_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    cwd=cwd,
                    prompt=prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    log_path=log_path,
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
            result.update(failure)
    else:
        result.update(
            {
                "status": "unsupported-mode",
                "error": (
                    f"Unsupported {mode_env_name}={mode!r}. Use gui, dry-run, "
                    "or manual. Terminal/CLI fork launch is intentionally disabled."
                ),
            }
        )

    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    status = result.get("status", "unknown")
    if status == "manual-prepared":
        message = (
            f"{log_prefix} prepared: no Terminal was launched and no current-chat "
            f"continuation was submitted. parent_thread_id={parent_session_id}. "
            f"prompt={prompt_path}. log={log_path}"
        )
    elif status == "app-server-started":
        socket_source = result.get("socket_source")
        socket_note = f" via {socket_source}" if isinstance(socket_source, str) else ""
        if socket_source == "bridge":
            socket_selection = result.get("socket_selection")
            desktop_probe = (
                socket_selection.get("desktop_control")
                if isinstance(socket_selection, dict)
                else None
            )
            if isinstance(desktop_probe, dict):
                reason = desktop_probe.get("reason") or "unknown"
                socket_note += f"; desktop_control_unavailable={reason}"
        session_visibility = result.get("session_visibility")
        if isinstance(session_visibility, dict):
            location = session_visibility.get("location")
            if isinstance(location, str) and location != "active":
                socket_note += f"; session_visibility={location}"
        message = (
            f"{log_prefix} forked in Codex GUI/app-server{socket_note}: "
            f"fork_thread_id={result.get('fork_thread_id')}; "
            f"parent_thread_id={parent_session_id}; log={log_path}"
        )
    else:
        if status == "app-server-failed":
            socket_selection = result.get("socket_selection")
            desktop_probe = (
                socket_selection.get("desktop_control")
                if isinstance(socket_selection, dict)
                else None
            )
            if isinstance(desktop_probe, dict):
                reason = desktop_probe.get("reason") or "unknown"
                status = f"{status}; desktop_control_unavailable={reason}"
        message = (
            f"{log_prefix} triggered: "
            f"{status}. parent_thread_id={parent_session_id}. log={log_path}"
        )
    return {
        "continue": True,
        "systemMessage": message,
    }


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
    }


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
    archived_paths: list[str] = []
    hinted = Path(hinted_path).expanduser() if hinted_path else None
    hinted_exists = False
    if hinted is not None:
        try:
            hinted_exists = hinted.exists()
        except OSError:
            hinted_exists = False
        if hinted_exists:
            target = archived_paths if path_is_relative_to(
                hinted,
                DEFAULT_CODEX_ARCHIVED_SESSIONS_DIR,
            ) else active_paths
            target.append(str(hinted))

    if not active_paths and DEFAULT_CODEX_SESSIONS_DIR.exists():
        active_paths.extend(
            str(path)
            for path in DEFAULT_CODEX_SESSIONS_DIR.rglob(f"*{thread_id}*.jsonl")
        )
    if not archived_paths and DEFAULT_CODEX_ARCHIVED_SESSIONS_DIR.exists():
        archived_paths.extend(
            str(path)
            for path in DEFAULT_CODEX_ARCHIVED_SESSIONS_DIR.glob(f"*{thread_id}*.jsonl")
        )

    location = "missing"
    if active_paths:
        location = "active"
    elif archived_paths:
        location = "archived"

    return {
        "thread_id": thread_id,
        "hinted_path": str(hinted) if hinted is not None else None,
        "hinted_exists": hinted_exists,
        "location": location,
        "active_paths": active_paths,
        "archived_paths": archived_paths,
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
        opened = maybe_open_fork_in_codex(fork_thread_id)
        return {
            "status": "app-server-started",
            "socket_path": str(socket_path),
            "socket_source": socket_source,
            "socket_selection": socket_selection,
            "fork_thread_id": fork_thread_id,
            "fork_thread_path": fork_thread_path,
            "turn_id": turn_id,
            "session_visibility": session_visibility,
            "opened_gui_deeplink": opened,
            "notifications": client.notifications[-20:],
        }
    finally:
        client.close()


def run_fork_experiment(event: dict[str, Any], latest_user: str) -> dict[str, Any]:
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
    )
    latest_path = state_dir() / "latest.json"
    try:
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        data["marker"] = os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER)
        data["latest_user_message"] = latest_user
        latest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
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
        return "continuation"
    if mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    return "fork"


def fork_review_validate_fix(event: dict[str, Any], repo: str) -> dict[str, Any]:
    parent_session_id = parent_thread_id_from_event(event) or ""
    parent_thread_path = parent_thread_path_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else repo
    prompt = fork_review_validate_fix_prompt(parent_session_id, cwd, repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=parent_session_id,
        cwd=repo,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=parent_thread_path,
    )


def review_validate_fix_dispatch(event: dict[str, Any], repo: str) -> dict[str, Any] | None:
    mode = rvf_mode()
    if mode == "off":
        return {
            "continue": True,
            "systemMessage": f"review-validate-fix Stop hook 已跳过：CODEX_RVF_MODE=off。repo={repo}",
        }
    if mode == "continuation":
        return continuation(repo)
    return fork_review_validate_fix(event, repo)


def should_suppress(event: dict[str, Any]) -> bool:
    if any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES):
        return True

    if event.get("suppress_review_validate_fix") is True:
        return True
    if event.get("review_validate_fix_suppressed") is True:
        return True

    if source_marks_subagent(event.get("source")):
        return True

    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


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


def parse_trusted_projects(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []

    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        projects = data.get("projects", {})
        if isinstance(projects, dict):
            return [
                str(path)
                for path, settings in projects.items()
                if isinstance(settings, dict)
                and settings.get("trust_level") == "trusted"
            ]
    except Exception:
        pass

    trusted: list[str] = []
    current_path: str | None = None
    current_trusted = False
    section_re = re.compile(r'^\[projects\."(.*)"\]\s*$')

    def flush() -> None:
        if current_path and current_trusted:
            trusted.append(current_path)

    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = section_re.match(stripped)
        if match:
            flush()
            current_path = match.group(1).replace(r"\"", '"')
            current_trusted = False
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            flush()
            current_path = None
            current_trusted = False
            continue
        if current_path and stripped == 'trust_level = "trusted"':
            current_trusted = True
    flush()
    return trusted


def trusted_dirty_repos() -> tuple[list[str], list[str]]:
    config_path = Path(os.environ.get("CODEX_RVF_CONFIG", str(DEFAULT_CONFIG)))
    dirty: list[str] = []
    errors: list[str] = []

    for project in parse_trusted_projects(config_path):
        result = run_gate(project)
        if result.status == "DIRTY" and result.repo:
            if result.repo not in dirty:
                dirty.append(result.repo)
        elif result.status == "ERROR":
            errors.append(f"{project}: {result.output}")

    return dirty, errors


def continuation(repo: str) -> dict[str, Any]:
    reason = (
        "$review-validate-fix\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后自动提交的 "
        "continuation prompt。请基于完整会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}\n\n"
        "如果会话历史里出现 `RVF_STOP_HOOK: off`、`RVF_STOP_HOOK: on` "
        "或 `RVF_STOP_HOOK: status` 这样的行，请只把它们视为 Stop hook "
        "会话控制元数据；不要把它们当成用户分配的代码任务、review issue、"
        "research 对象或 scope-of-work 内容。"
    )
    return {"decision": "block", "reason": reason}


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    if event.get("stop_hook_active") is True:
        emit(skip_payload("检测到 stop_hook_active=true，为避免递归已跳过。"))
        return 0

    latest_user = latest_user_message_from_event(event)
    fork_context = rvf_fork_context(latest_user) or rvf_fork_context_from_event(event)
    if fork_context is not None:
        advisory = handoff_advisory(event, fork_context)
        if advisory is not None:
            emit(advisory)
        else:
            emit(skip_payload("当前会话已是 review-validate-fix fork，会等待最终 <handoff-context>，不会再次 fork。"))
        return 0

    if event_marks_subagent(event):
        emit(skip_payload("Stop event 来自 Codex subagent，post-work review 只允许主会话触发。"))
        return 0

    session_control = session_hook_control_payload(event, latest_user)
    if session_control is not None:
        emit(session_control)
        return 0

    session_id = session_hook_id_from_event(event)
    if session_id and session_hook_disabled(session_id):
        emit(skip_payload(f"当前 chat session 已禁用 RVF_STOP_HOOK。session_id={session_id}"))
        return 0

    should_experiment, latest_user = should_run_fork_experiment(event)
    if should_experiment and latest_user is not None:
        emit(run_fork_experiment(event, latest_user))
        return 0

    if should_suppress(event):
        emit(skip_payload("检测到 suppress 标记或环境变量。"))
        return 0

    cwd = event.get("cwd")
    cwd_result: GateResult | None = None
    if isinstance(cwd, str) and cwd:
        cwd_result = run_gate(cwd)
        if cwd_result.status == "DIRTY" and cwd_result.repo:
            payload = review_validate_fix_dispatch(event, cwd_result.repo)
            if payload is not None:
                emit(payload)
            return 0
        if cwd_result.status == "CLEAN":
            emit(skip_payload(f"当前 cwd 仓库是 clean。repo={cwd_result.repo or cwd}"))
            return 0

    dirty_repos, _errors = trusted_dirty_repos()
    if len(dirty_repos) == 1:
        payload = review_validate_fix_dispatch(event, dirty_repos[0])
        if payload is not None:
            emit(payload)
        return 0
    if len(dirty_repos) > 1:
        joined = ", ".join(dirty_repos)
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Stop hook 已跳过：发现多个 dirty trusted repo，"
                    f"为避免审错仓库未自动触发。候选: {joined}"
                ),
            }
        )
    elif cwd_result is not None:
        emit(
            skip_payload(
                f"当前 cwd gate={cwd_result.status}，且没有唯一 dirty trusted repo。cwd={cwd}"
            )
        )
    else:
        emit(skip_payload("Stop event 未提供可检查的 cwd，且没有唯一 dirty trusted repo。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
