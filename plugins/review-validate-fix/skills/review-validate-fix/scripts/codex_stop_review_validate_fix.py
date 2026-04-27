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
DEFAULT_SESSION_HOOK_STATE_DIR = SKILL_DIR / "state" / "session-hook"
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
            f"{status}。session_id={session_id}; state={state_path}。"
            "该开关只控制 RVF fork/continuation/review gate，"
            "不控制 dispatcher 的 dev sync。"
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
    fallback_failure_reason: str | None = None,
    allow_desktop_unavailable_report: bool = True,
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

    effective_prompt = prompt
    if suppress_child_stop_hook and SUPPRESS_STOP_HOOK_MARKER not in effective_prompt:
        effective_prompt = (
            f"{effective_prompt.rstrip()}\n\n"
            "Stop hook child-session metadata:\n"
            f"{SUPPRESS_STOP_HOOK_MARKER}\n"
            "当前 fork 结束时请跳过 review-validate-fix Stop hook。"
        )

    prompt_path.write_text(effective_prompt, encoding="utf-8")

    result: dict[str, Any] = {
        "timestamp": timestamp,
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "cwd": cwd,
        "prompt": effective_prompt,
        "prompt_path": str(prompt_path),
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    return_payload: dict[str, Any] | None = None

    if mode in {"manual", "prepare", "prepared", "log-only"}:
        result["status"] = "manual-prepared"
    elif mode == "dry-run":
        result["status"] = "dry-run"
        result["app_server_requests"] = app_server_fork_requests(
            parent_thread_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=effective_prompt,
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
                    prompt=effective_prompt,
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
                    f"Unsupported {mode_env_name}={mode!r}. Use gui, dry-run, "
                    "or manual. Terminal/CLI fork launch is intentionally disabled."
                ),
            }
        )

    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if return_payload is not None:
        return return_payload

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
        target_name = "Codex GUI/app-server"
        if socket_source == "bridge":
            target_name = "Codex app-server bridge"
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
        gui_visibility = result.get("gui_visibility")
        if isinstance(gui_visibility, str) and gui_visibility != "verified":
            socket_note += f"; gui_visibility={gui_visibility}"
        message = (
            f"{log_prefix} forked in {target_name}{socket_note}: "
            f"fork_thread_id={result.get('fork_thread_id')}; "
            f"parent_thread_id={parent_session_id}; log={log_path}"
        )
    elif status in {"desktop-control-unavailable-report", "desktop-control-unavailable-fail"}:
        report_reason = result.get("report_reason")
        reason = report_reason if isinstance(report_reason, str) else "Codex GUI fork unavailable."
        message = (
            f"{reason} parent_thread_id={parent_session_id}; log={log_path}"
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
        allow_desktop_unavailable_report=False,
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


def fork_review_validate_fix(event: dict[str, Any], repo: str) -> dict[str, Any]:
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
    )


def review_validate_fix_dispatch(event: dict[str, Any], repo: str) -> dict[str, Any] | None:
    mode = rvf_mode()
    if mode == "off":
        return {
            "continue": True,
            "systemMessage": f"review-validate-fix Stop hook 已跳过：CODEX_RVF_MODE=off。repo={repo}",
        }
    if mode == "report":
        return {"continue": True, "systemMessage": fork_failure_report(repo)}
    return fork_review_validate_fix(event, repo)


def should_suppress(event: dict[str, Any], latest_user: str | None = None) -> bool:
    if any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES):
        return True

    if latest_user and SUPPRESS_STOP_HOOK_MARKER in latest_user:
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
        emit(
            skip_payload(
                "当前 chat session 已禁用 RVF_STOP_HOOK；"
                "只跳过 RVF fork/continuation/review gate，"
                f"不控制 dispatcher 的 dev sync。session_id={session_id}"
            )
        )
        return 0

    should_experiment, latest_user = should_run_fork_experiment(event)
    if should_experiment and latest_user is not None:
        emit(run_fork_experiment(event, latest_user))
        return 0

    if should_suppress(event, latest_user):
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

    if cwd_result is not None:
        emit(
            skip_payload(
                "当前 cwd 不在 git repo/worktree 内，未自动选择目标仓库。"
                f"cwd gate={cwd_result.status}; cwd={cwd}。"
                "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。"
            )
        )
    else:
        emit(
            skip_payload(
                "Stop event 未提供可检查的 cwd，未自动选择目标仓库。"
                "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。"
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
