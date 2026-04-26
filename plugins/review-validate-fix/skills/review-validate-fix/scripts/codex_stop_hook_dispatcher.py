#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STOP_HOOK = SKILL_DIR / "scripts" / "codex_stop_review_validate_fix.py"
DEFAULT_STATE_DIR = SKILL_DIR / "state" / "dev-sync"
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


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


def is_truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def state_dir() -> Path:
    explicit = os.environ.get("CODEX_RVF_DEV_SYNC_STATE_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    return DEFAULT_STATE_DIR


def command_timeout() -> float:
    value = os.environ.get("CODEX_RVF_DEV_SYNC_COMMAND_TIMEOUT")
    if value and value.strip():
        try:
            return max(1.0, float(value))
        except ValueError:
            pass
    return 60.0


def stop_hook_timeout() -> float:
    value = os.environ.get("CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT")
    if value and value.strip():
        try:
            return max(1.0, float(value))
        except ValueError:
            pass
    return 30.0


def coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def sync_child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("CODEX_RVF_")}


def git_root(path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return Path(output).resolve() if output else None


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


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
                return isinstance(payload, dict) and source_marks_subagent(
                    payload.get("source")
                )
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


def event_marks_subagent(event: dict[str, Any]) -> bool:
    if source_marks_subagent(event.get("source")):
        return True
    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def event_git_root(event: dict[str, Any]) -> Path | None:
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    return git_root(Path(cwd).expanduser())


def dev_repo_root() -> Path | None:
    configured = os.environ.get("CODEX_RVF_DEV_REPO")
    if not configured or not configured.strip():
        return None
    repo = Path(configured).expanduser()
    root = git_root(repo)
    return root or repo.resolve()


def should_sync(event: dict[str, Any]) -> tuple[bool, str, Path | None]:
    if not is_truthy(os.environ.get("CODEX_RVF_DEV_SYNC"), default=True):
        return False, "dev sync disabled by CODEX_RVF_DEV_SYNC", None
    repo = dev_repo_root()
    if repo is None:
        return False, "CODEX_RVF_DEV_REPO is not set", None
    cwd_root = event_git_root(event)
    if cwd_root is None:
        return False, "event cwd is not inside a git repo", repo
    if not same_path(cwd_root, repo):
        return False, f"event repo does not match dev repo: {cwd_root}", repo
    if event_marks_subagent(event):
        return False, "event is from a Codex subagent", repo
    return True, "matched RVF dev repo main session", repo


def run_step(cmd: list[str], *, cwd: Path) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=sync_child_env(),
            timeout=command_timeout(),
        )
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": 124,
            "stdout": coerce_text(exc.stdout),
            "stderr": coerce_text(exc.stderr)
            or f"timed out after {command_timeout()} seconds",
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def write_log(record: dict[str, Any]) -> Path | None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{stamp}.rvf-dev-sync.json"
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def sync_from_dev_repo(repo: Path, event: dict[str, Any]) -> tuple[bool, Path | None, str]:
    python = sys.executable or "python3"
    steps: list[dict[str, Any]] = []
    sync_script = repo / "scripts" / "sync_plugin_payload.py"
    install_script = repo / "scripts" / "install_to_codex.py"

    for script, args in (
        (sync_script, ["--check-contracts"]),
        (install_script, ["--as", "skill", "--configure-stop-hook"]),
    ):
        if not script.is_file():
            record = {
                "status": "failed",
                "reason": f"missing script: {script}",
                "repo": str(repo),
                "event": event_summary(event),
                "steps": steps,
            }
            log_path = write_log(record)
            return False, log_path, record["reason"]
        result = run_step([python, str(script), *args], cwd=repo)
        steps.append(result)
        if result["returncode"] != 0:
            record = {
                "status": "failed",
                "reason": f"command failed: {' '.join(map(str, result['cmd']))}",
                "repo": str(repo),
                "event": event_summary(event),
                "steps": steps,
            }
            log_path = write_log(record)
            return False, log_path, record["reason"]

    record = {
        "status": "synced",
        "repo": str(repo),
        "event": event_summary(event),
        "steps": steps,
    }
    return True, write_log(record), "synced"


def event_summary(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "cwd": event.get("cwd"),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "transcript_path": event.get("transcript_path"),
    }


def configured_stop_hook() -> Path:
    explicit = os.environ.get("CODEX_RVF_INSTALLED_STOP_HOOK")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    return DEFAULT_STOP_HOOK


def run_installed_stop_hook(raw_input: str) -> int:
    hook = configured_stop_hook()
    python = sys.executable or "python3"
    try:
        completed = subprocess.run(
            [python, str(hook)],
            input=raw_input,
            capture_output=True,
            text=True,
            timeout=stop_hook_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Stop hook dispatcher: installed hook timed "
                    f"out after {stop_hook_timeout()} seconds. stderr={exc.stderr or ''}"
                ),
            }
        )
        return 0
    except OSError as exc:
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Stop hook dispatcher: failed to run "
                    f"installed hook {hook}: {exc}"
                ),
            }
        )
        return 0

    if completed.returncode == 0:
        sys.stdout.write(completed.stdout)
        return 0

    emit(
        {
            "continue": True,
            "systemMessage": (
                "review-validate-fix Stop hook dispatcher: installed hook failed "
                f"with exit code {completed.returncode}. stderr={completed.stderr.strip()}"
            ),
        }
    )
    return 0


def main() -> int:
    raw_input, event = read_input()
    if event is None:
        return run_installed_stop_hook(raw_input)

    sync_needed, reason, repo = should_sync(event)
    if not sync_needed:
        return run_installed_stop_hook(raw_input)

    assert repo is not None
    synced, log_path, sync_reason = sync_from_dev_repo(repo, event)
    if not synced:
        log_note = f"; log={log_path}" if log_path is not None else ""
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Stop hook 未运行 fork：RVF dev sync "
                    f"失败，已避免使用旧 installed skill。reason={sync_reason}{log_note}"
                ),
            }
        )
        return 0

    return run_installed_stop_hook(raw_input)


if __name__ == "__main__":
    raise SystemExit(main())
