#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, start_run


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STABLE_STOP_HOOK = SKILL_DIR / "scripts" / "codex_stop_hook_dispatcher.py"
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
SESSION_HOOK_CHANNEL_CONTROL_KEY = "RVF_STOP_HOOK_CHANNEL"
VALID_CHANNELS = {"stable", "dev"}
# 绝对兜底；正常默认走 ``default_channel()``，dev terms 满足时优先 dev。
DEFAULT_CHANNEL = "stable"


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def read_input() -> tuple[str, dict[str, Any] | None]:
    raw = sys.stdin.read()
    if not raw.strip():
        return raw, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    return raw, data if isinstance(data, dict) else None


def coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def safe_state_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return key[:180] if key else "unknown-session"


def state_dir() -> Path:
    explicit = os.environ.get("CODEX_RVF_SESSION_HOOK_STATE_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    root = os.environ.get("CODEX_RVF_STATE_DIR") or os.environ.get("CODEX_RVF_LOG_ROOT")
    if root and root.strip():
        return Path(root).expanduser() / "session-hook"
    return SKILL_DIR / "state" / "session-hook"


def state_path(session_id: str) -> Path:
    return state_dir() / f"{safe_state_key(session_id)}.json"


def read_state(session_id: str) -> dict[str, Any]:
    path = state_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(session_id: str, state: dict[str, Any]) -> Path:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def maybe_remove_empty_state(session_id: str, state: dict[str, Any]) -> Path:
    path = state_path(session_id)
    if set(state) <= {"session_id"}:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path
    return write_state(session_id, state)


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def session_meta_from_path(path: Path) -> dict[str, Any]:
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return {}
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeDecodeError):
        return {}
    return {}


def session_id_from_path(path: Path) -> str | None:
    value = session_meta_from_path(path).get("id")
    return value if isinstance(value, str) and value else None


def session_id_from_event(event: dict[str, Any]) -> str | None:
    for key in ("session_id", "thread_id", "conversation_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for path in event_session_paths(event):
        session_id = session_id_from_path(path)
        if session_id:
            return session_id
    return None


def latest_user_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
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
                if record.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                content = payload.get("content")
                if isinstance(content, str):
                    latest = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            parts.append(item["text"])
                    if parts:
                        latest = "\n".join(parts)
    except (OSError, UnicodeDecodeError):
        return None
    return latest


def latest_user_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_user_message")
    if isinstance(direct, str) and direct:
        return direct
    for path in event_session_paths(event):
        message = latest_user_message(path)
        if message:
            return message
    return None


def parse_channel_control(text: str | None) -> str | None:
    if not text:
        return None
    pattern = re.compile(
        rf"^\s*{re.escape(SESSION_HOOK_CHANNEL_CONTROL_KEY)}\s*:\s*([A-Za-z_-]+)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().lower().replace("_", "-")
    if value in {"stable", "release", "prod", "production"}:
        return "stable"
    if value in {"dev", "development"}:
        return "dev"
    if value in {"default", "reset", "clear"}:
        return "default"
    if value in {"status", "state"}:
        return "status"
    return None


def default_channel() -> tuple[str, str]:
    """选 dev 当且仅当 dev terms 满足；否则 stable。

    "dev terms 满足" = ``target_for_channel("dev")`` 能解析出一个真实存在的
    target file（即 ``CODEX_RVF_DEV_STOP_HOOK`` 或 ``CODEX_RVF_DEV_REPO``
    其中之一配置正确）。session 显式 channel marker（``RVF_STOP_HOOK_CHANNEL:
    stable|dev``）仍可覆盖本默认。

    返回 ``(channel, source)``：source ∈ {``"dev-default"``,
    ``"stable-default"``}，用于 ledger / 诊断可视化。
    """
    target, _ = target_for_channel("dev")
    if target is not None and target.is_file():
        return "dev", "dev-default"
    return DEFAULT_CHANNEL, "stable-default"


def channel_from_state(session_id: str | None) -> tuple[str, str]:
    if not session_id:
        return default_channel()
    value = read_state(session_id).get("channel")
    if isinstance(value, str) and value in VALID_CHANNELS:
        return value, "session-marker"
    return default_channel()


def set_channel(session_id: str, action: str, latest_user: str | None) -> tuple[str, Path]:
    state = read_state(session_id)
    if action == "default":
        state.pop("channel", None)
        state.pop("channel_control", None)
        state.pop("channel_latest_user_message", None)
        state.pop("channel_updated_at", None)
        if set(state) > {"session_id"}:
            state["channel_updated_at"] = datetime.now(timezone.utc).isoformat()
        resolved, _ = default_channel()
        return resolved, maybe_remove_empty_state(session_id, state)

    state.update(
        {
            "channel": action,
            "channel_control": SESSION_HOOK_CHANNEL_CONTROL_KEY,
            "channel_latest_user_message": latest_user,
            "channel_updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return action, write_state(session_id, state)


def gate_status(session_id: str | None) -> str:
    if not session_id:
        return "unknown"
    state = read_state(session_id)
    return "disabled" if state.get("enabled") is False else "enabled"


def target_for_channel(channel: str) -> tuple[Path | None, str | None]:
    if channel == "stable":
        explicit = os.environ.get("CODEX_RVF_STABLE_STOP_HOOK")
        return (
            Path(explicit).expanduser() if explicit and explicit.strip() else DEFAULT_STABLE_STOP_HOOK,
            None,
        )

    explicit = os.environ.get("CODEX_RVF_DEV_STOP_HOOK")
    if explicit and explicit.strip():
        return Path(explicit).expanduser(), None

    dev_repo = os.environ.get("CODEX_RVF_DEV_REPO")
    if dev_repo and dev_repo.strip():
        return (
            Path(dev_repo).expanduser()
            / "plugins"
            / "review-validate-fix"
            / "skills"
            / "review-validate-fix"
            / "scripts"
            / "codex_stop_hook_dispatcher.py",
            None,
        )
    return None, "dev channel requested but CODEX_RVF_DEV_STOP_HOOK/CODEX_RVF_DEV_REPO is not configured"


def target_timeout() -> float:
    value = os.environ.get("CODEX_RVF_STOP_HOOK_ROUTER_TIMEOUT")
    if value and value.strip():
        try:
            return max(1.0, float(value))
        except ValueError:
            pass
    return 300.0


def emit_terminal_payload(
    ledger: RunLedger,
    *,
    status: str,
    reason_code: str,
    message: str,
    detail: str | None = None,
    **summary_fields: Any,
) -> int:
    emit(
        ledger.hook_payload(
            status=status,
            reason_code=reason_code,
            continue_=True,
            message=message,
            detail=detail,
            **summary_fields,
        )
    )
    return 0


def route_to_target(
    raw_input: str,
    *,
    channel: str,
    target: Path,
    ledger: RunLedger,
) -> int:
    current = Path(__file__).resolve()
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target
    if resolved_target == current:
        ledger.summary(
            status="failed",
            reason_code="router_target_recursion",
            message=f"RVF Stop hook router target points back to itself: {target}",
            channel=channel,
            target=str(target),
        )
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="router_target_recursion",
            message=f"RVF Stop hook router target points back to itself: {target}",
            detail="router target recursion",
            channel=channel,
            target=str(target),
        )
    if not target.is_file():
        ledger.summary(
            status="failed",
            reason_code="router_target_missing",
            message=f"RVF Stop hook router target is missing: {target}",
            channel=channel,
            target=str(target),
        )
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="router_target_missing",
            message=f"RVF Stop hook router target is missing: {target}",
            detail="router target missing",
            channel=channel,
            target=str(target),
        )

    env = os.environ.copy()
    env.update(ledger.env())
    env["CODEX_RVF_SELECTED_CHANNEL"] = channel
    env["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = str(state_dir())
    # Channel routing owns stable/dev selection. Stable must never be updated
    # implicitly by Stop hook activity. Dev may run repository checks, but it
    # must not copy dev code into the stable installed plugin.
    env["CODEX_RVF_DEV_SYNC"] = "1" if channel == "dev" else "0"
    if channel == "dev":
        env["CODEX_RVF_DEV_SYNC_INSTALL"] = "0"
    ledger.event(
        phase="router",
        event="target_started",
        status="started",
        reason_code="route_to_channel",
        channel=channel,
        target=str(target),
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable or "python3", str(target)],
            input=raw_input,
            capture_output=True,
            text=True,
            env=env,
            timeout=target_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        ledger.summary(
            status="failed",
            reason_code="router_target_timeout",
            message=f"RVF Stop hook router target timed out after {target_timeout()} seconds",
            channel=channel,
            target=str(target),
            stderr=coerce_text(exc.stderr),
        )
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="router_target_timeout",
            message=f"RVF Stop hook router target timed out after {target_timeout()} seconds",
            detail="router target timeout",
            channel=channel,
            target=str(target),
        )
    except OSError as exc:
        ledger.summary(
            status="failed",
            reason_code="router_target_exec_failed",
            message=f"failed to run RVF Stop hook router target {target}: {exc}",
            channel=channel,
            target=str(target),
        )
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="router_target_exec_failed",
            message=f"failed to run RVF Stop hook router target {target}: {exc}",
            detail="router target exec failed",
            channel=channel,
            target=str(target),
        )

    ledger.event(
        phase="router",
        event="target_completed",
        status="completed" if completed.returncode == 0 else "failed",
        reason_code="target_completed" if completed.returncode == 0 else "target_failed",
        channel=channel,
        target=str(target),
        returncode=completed.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if completed.returncode == 0:
        sys.stdout.write(completed.stdout)
        return 0

    paths: dict[str, str] = {}
    if completed.stdout:
        stdout_path = ledger.artifact("router-target.stdout.txt", completed.stdout)
        if stdout_path:
            paths["stdout"] = stdout_path
    if completed.stderr:
        stderr_path = ledger.artifact("router-target.stderr.txt", completed.stderr)
        if stderr_path:
            paths["stderr"] = stderr_path
    ledger.summary(
        status="failed",
        reason_code="router_target_failed",
        message=f"RVF Stop hook router target failed with exit code {completed.returncode}",
        channel=channel,
        target=str(target),
        returncode=completed.returncode,
        paths=paths,
    )
    return emit_terminal_payload(
        ledger,
        status="failed",
        reason_code="router_target_failed",
        message=f"RVF Stop hook router target failed with exit code {completed.returncode}",
        detail="router target failed",
        channel=channel,
        target=str(target),
        returncode=completed.returncode,
    )


def main() -> int:
    raw_input, event = read_input()
    cwd = event.get("cwd") if isinstance(event, dict) else None
    ledger = start_run(
        "dispatcher",
        repo=str(cwd) if isinstance(cwd, str) else None,
        cwd=str(cwd) if isinstance(cwd, str) else None,
    )
    if event is None:
        channel, source = default_channel()
        target, error = target_for_channel(channel)
        if target is None:
            return emit_terminal_payload(
                ledger,
                status="failed",
                reason_code="router_target_missing",
                message=error or "RVF stable Stop hook target is unavailable",
                detail="router target missing",
                channel=channel,
                channel_source=source,
            )
        return route_to_target(raw_input, channel=channel, target=target, ledger=ledger)

    latest_user = latest_user_message_from_event(event)
    session_id = session_id_from_event(event)
    action = parse_channel_control(latest_user)
    channel, source = channel_from_state(session_id)
    state_marker_path: Path | None = None

    if action == "status":
        ledger.event(
            phase="router",
            event="session_channel_status",
            status="completed",
            reason_code="session_hook_channel_status",
            session_id=session_id,
            selected_channel=channel,
            channel_source=source,
            session_hook_gate_state=gate_status(session_id),
        )
        ledger.summary(
            status="session-hook-channel-control",
            reason_code="session_hook_channel_status",
            message=(
                "当前 chat session 的 RVF Stop hook channel 状态为 "
                f"{channel}；gate={gate_status(session_id)}；source={source}。"
            ),
            session_id=session_id,
            control_action="status",
            selected_channel=channel,
            channel_source=source,
            session_hook_gate_state=gate_status(session_id),
            state_path=str(state_path(session_id)) if session_id else None,
        )
        return emit_terminal_payload(
            ledger,
            status="session-hook-channel-control",
            reason_code="session_hook_channel_status",
            message=(
                "当前 chat session 的 RVF Stop hook channel 状态为 "
                f"{channel}；gate={gate_status(session_id)}；source={source}。"
            ),
            detail="session hook channel status",
            session_id=session_id,
            control_action="status",
            selected_channel=channel,
            channel_source=source,
            session_hook_gate_state=gate_status(session_id),
            state_path=str(state_path(session_id)) if session_id else None,
        )

    if action in {"stable", "dev", "default"}:
        if not session_id:
            return emit_terminal_payload(
                ledger,
                status="failed",
                reason_code="session_hook_channel_unknown_session",
                message=(
                    "review-validate-fix 无法记录当前 chat session 的 Stop hook channel："
                    "Stop event 未暴露 session id。"
                ),
                detail="session id unavailable",
                control_action=action,
            )
        channel, state_marker_path = set_channel(session_id, action, latest_user)
        if action == "default":
            _, source = default_channel()
        else:
            source = "latest-user-message"
        ledger.event(
            phase="router",
            event="session_channel_control",
            status="completed",
            reason_code="session_hook_channel_selected",
            session_id=session_id,
            control_action=action,
            selected_channel=channel,
            channel_source=source,
            state_path=str(state_marker_path),
        )

    target, error = target_for_channel(channel)
    if target is None:
        ledger.summary(
            status="failed",
            reason_code="router_channel_unconfigured",
            message=error or f"RVF Stop hook channel is unconfigured: {channel}",
            session_id=session_id,
            selected_channel=channel,
            channel_source=source,
        )
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="router_channel_unconfigured",
            message=error or f"RVF Stop hook channel is unconfigured: {channel}",
            detail="router channel unconfigured",
            session_id=session_id,
            selected_channel=channel,
            channel_source=source,
        )

    ledger.event(
        phase="router",
        event="channel_selected",
        status="completed",
        reason_code="channel_selected",
        session_id=session_id,
        selected_channel=channel,
        channel_source=source,
        target=str(target),
        state_path=str(state_marker_path) if state_marker_path is not None else None,
    )
    return route_to_target(raw_input, channel=channel, target=target, ledger=ledger)


if __name__ == "__main__":
    raise SystemExit(main())
