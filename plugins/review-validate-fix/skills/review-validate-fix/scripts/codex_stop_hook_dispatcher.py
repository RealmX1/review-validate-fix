#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import RunLedger, start_run  # noqa: E402
from rvf_handoff import handoff_completion_payload, handoff_path_from_event
from rvf_run_finalize import finalize_for_handoff, surface_finalize_record_errors
from rvf_analyze_advisory import (
    RVF_ANALYZE_FOLLOWUP_MARKER,
    surface_rvf_analyze_advisory,
)
from core.session_scope_allocation.session_change_manifest import build_manifest


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STOP_HOOK = SKILL_DIR / "scripts" / "codex_stop_review_validate_fix.py"
DEV_SYNC_CONTRACT_SCRIPT = Path("scripts") / "check_plugin_contracts.py"
DEV_SYNC_INSTALL_SCRIPT = Path("scripts") / "install_to_codex.py"
LEGACY_DEFAULT_CLINE_KANBAN_ENV = {
    "CODEX_RVF_CLINE_KANBAN_START_CMD": "npx -y kanban@0.1.66 --no-open",
    "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "npx -y kanban@0.1.66 task",
}
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
SUPPRESS_ENV_NAMES = (
    "RVF_SUPPRESS",
    "RVF_SUPPRESS_STOP_HOOK",
)
PLAN_OPERATION_MARKERS = (
    "<proposed_plan>",
    "</proposed_plan>",
)
PLAN_OPERATION_TEXT_RE = re.compile(
    rf"\A\s*{re.escape(PLAN_OPERATION_MARKERS[0])}.*{re.escape(PLAN_OPERATION_MARKERS[1])}\s*\Z",
    re.DOTALL,
)
PLAN_OPERATION_VALUES = {
    "plan",
    "planning",
    "plan-operation",
    "plan_operation",
    "proposed-plan",
    "proposed_plan",
}


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def fail_blocking(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


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


def suppress_requested() -> bool:
    return any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES)


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
    # dev-sync 子进程（contract-check / installer / 重入 hook）必须拿到不含父会话 RVF
    # run 状态的干净 env，否则子进程会误继承父 RVF run 的 run_dir / suppress marker /
    # tracker 开关。RVF env 命名空间横跨两个前缀：``CODEX_RVF_``（部署 + codex-specific）
    # 与裸 ``RVF_``（去-codex 后的中性运行时变量，如 RVF_RUN_DIR / RVF_SUPPRESS_STOP_HOOK /
    # RVF_TRACKER_*），两者都要剥离。
    return {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("CODEX_RVF_") or key.startswith("RVF_"))
    }


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
    except (OSError, UnicodeDecodeError):
        return False
    return False


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def first_readable_session_path(event: dict[str, Any]) -> Path | None:
    for path in event_session_paths(event):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            with resolved.open("rb"):
                pass
        except OSError:
            continue
        else:
            return resolved
    return None


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


def should_sync_session_scope(
    event: dict[str, Any],
    repo: Path,
    ledger: RunLedger,
) -> tuple[bool, str, str]:
    """Slice 3 dispatcher gate. New main path runs the allocator in dry-run
    mode and skips the installed hook when there are no unassigned review
    units. Tracker-disabled / session-id-less events fall back to the
    pre-Slice-3 manifest-based legacy gate so disable-mode behavior matches
    Phase 0/1 byte-for-byte."""
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from codex_stop_review_validate_fix import (
        _StopContextError,
        resolve_stop_context,
        refresh_global_diff_tracker,
        allocate_auto_review_scope,
    )
    from core.session_scope_allocation.reviewable_unit_diff_tracker import (
        LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
        LEGACY_REASON_SESSION_OWNED_DIRTY,
        REASON_NO_UNASSIGNED_REVIEW_SCOPE,
        REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
        _disabled as _tracker_disabled,
    )

    if _tracker_disabled():
        return _legacy_should_sync_session_scope(event, repo, ledger)

    try:
        context = resolve_stop_context(event, str(repo), ledger)
    except _StopContextError:
        # `resolve_stop_context` already emitted the gate-side
        # `transcript_unavailable` event under phase=gate. Mirror the
        # dev-sync side so dispatcher logs grep cleanly, then refuse to sync.
        ledger.event(
            phase="dev-sync",
            event="session_scope_unavailable",
            status="failed",
            reason_code="transcript_unavailable",
            repo=str(repo),
            cwd=str(repo),
        )
        return (
            False,
            "session transcript path was provided but is not readable; skipped RVF dev sync and installed hook",
            "transcript_unavailable",
        )
    except Exception as exc:
        # Transcript readable but malformed (binary garbage, etc.) — the
        # transcript-derived helpers may raise UnicodeDecodeError before the
        # allocator gets a chance. Match the legacy `session_manifest_failed`
        # path so disable-mode and tracker-enabled dispatchers agree on the
        # reason code.
        ledger.event(
            phase="dev-sync",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=str(repo),
            cwd=str(repo),
            error=f"{type(exc).__name__}: {exc}",
        )
        return (
            False,
            "session manifest failed; skipped RVF dev sync and installed hook",
            "session_manifest_failed",
        )
    if not context.get("session_paths"):
        # Match legacy behavior: missing transcript path means we keep
        # running the installed hook the way the pre-tracker dispatcher did.
        ledger.event(
            phase="dev-sync",
            event="session_scope_unavailable",
            status="skipped",
            reason_code="missing_transcript",
            repo=str(repo),
            cwd=str(repo),
        )
        return (
            True,
            "session transcript unavailable; preserving legacy dev sync behavior",
            "missing_transcript",
        )

    # Mirror the Stop-hook orchestrator: seed `session_units` via
    # `refresh_global_diff_tracker` (which calls build_manifest →
    # register_claims) before running the allocator dry-run. Without this
    # step, the allocator sees zero session-attributed units and always
    # reports `would_proceed=False`.
    refresh_result = refresh_global_diff_tracker(context, ledger)
    refresh_error = (
        refresh_result.get("error") if isinstance(refresh_result, dict) else None
    )
    if refresh_error:
        # build_manifest 失败时 fail-loud：mirror Stop-hook orchestrator 的行为，
        # 在 dev-sync 端 emit `session_scope_failed` 并返回
        # `session_manifest_failed`，避免 allocator 看到空 session_units 后被
        # 静默判为 `no_unassigned_review_scope`（这会让 dispatcher 错误地跳过
        # dev-sync 与 installed hook）。
        ledger.event(
            phase="dev-sync",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=str(repo),
            cwd=str(repo),
            error=refresh_error,
        )
        return (
            False,
            "session manifest failed; skipped RVF dev sync and installed hook",
            "session_manifest_failed",
        )
    try:
        result = allocate_auto_review_scope(context, ledger, dry_run=True)
    except Exception as exc:
        ledger.event(
            phase="dev-sync",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=str(repo),
            cwd=str(repo),
            error=f"{type(exc).__name__}: {exc}",
        )
        return (
            False,
            "session manifest failed; skipped RVF dev sync and installed hook",
            "session_manifest_failed",
        )

    if result is None:
        # Allocator returned None — happens when repo can't be resolved.
        # Fall back to legacy gate so the dispatcher sees Phase-0 behavior.
        return _legacy_should_sync_session_scope(event, repo, ledger)

    if result.get("would_proceed"):
        ledger.event(
            phase="dev-sync",
            event="session_scope_detected",
            status="dirty",
            reason_code=REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
            reason_code_legacy_alias=LEGACY_REASON_SESSION_OWNED_DIRTY,
            repo=str(repo),
            cwd=str(repo),
            candidate_unit_count=result.get("candidate_unit_count", 0),
        )
        return (
            True,
            "unassigned review scope available",
            REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
        )

    ledger.event(
        phase="dev-sync",
        event="session_scope_clean",
        status="skipped",
        reason_code=REASON_NO_UNASSIGNED_REVIEW_SCOPE,
        reason_code_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
        repo=str(repo),
        cwd=str(repo),
        candidate_unit_count=result.get("candidate_unit_count", 0),
    )
    return (
        False,
        "no unassigned review scope",
        REASON_NO_UNASSIGNED_REVIEW_SCOPE,
    )


def _legacy_should_sync_session_scope(
    event: dict[str, Any],
    repo: Path,
    ledger: RunLedger,
) -> tuple[bool, str, str]:
    """Pre-Slice-3 dispatcher gate body, kept verbatim. Reason-code literals
    stay as `session_owned_dirty` / `no_session_owned_dirty` so disable-mode
    users see no behavior change."""
    session_paths = event_session_paths(event)
    transcript = first_readable_session_path(event)
    if transcript is None:
        if session_paths:
            ledger.event(
                phase="dev-sync",
                event="session_scope_unavailable",
                status="failed",
                reason_code="transcript_unavailable",
                repo=str(repo),
                cwd=str(repo),
                paths={"transcripts": [str(path) for path in session_paths]},
            )
            return (
                False,
                "session transcript path was provided but is not readable; skipped RVF dev sync and installed hook",
                "transcript_unavailable",
            )
        ledger.event(
            phase="dev-sync",
            event="session_scope_unavailable",
            status="skipped",
            reason_code="missing_transcript",
            repo=str(repo),
            cwd=str(repo),
        )
        return (
            True,
            "session transcript unavailable; preserving legacy dev sync behavior",
            "missing_transcript",
        )
    try:
        manifest = build_manifest(repo, transcript)
    except Exception as exc:
        ledger.event(
            phase="dev-sync",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=str(repo),
            cwd=str(repo),
            paths={"transcript": str(transcript)},
            error=f"{type(exc).__name__}: {exc}",
        )
        return (
            False,
            "session manifest failed; skipped RVF dev sync and installed hook",
            "session_manifest_failed",
        )

    manifest_path = ledger.artifact("session-manifest.json", manifest)
    owned_dirty = manifest.get("owned_dirty_paths")
    if isinstance(owned_dirty, list) and owned_dirty:
        ledger.event(
            phase="dev-sync",
            event="session_scope_detected",
            status="dirty",
            reason_code="session_owned_dirty",
            repo=str(repo),
            cwd=str(repo),
            paths={"manifest": manifest_path} if manifest_path else {},
            owned_dirty_paths=owned_dirty,
        )
        return True, "session-owned dirty paths detected", "session_owned_dirty"

    ledger.event(
        phase="dev-sync",
        event="session_scope_clean",
        status="skipped",
        reason_code="no_session_owned_dirty",
        repo=str(repo),
        cwd=str(repo),
        paths={"manifest": manifest_path} if manifest_path else {},
        unattributed_dirty_paths=manifest.get("unattributed_dirty_paths"),
    )
    return False, "no session-owned dirty paths", "no_session_owned_dirty"


def run_step(
    cmd: list[str],
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    env = sync_child_env()
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
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


def _command_env(command: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        tokens = shlex.split(command)
    except ValueError:
        return env
    for token in tokens:
        if "=" not in token or token.startswith("-"):
            break
        name, value = token.split("=", 1)
        if not name:
            break
        env[name] = value
    return env


def _command_targets_current_dispatcher(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    current = Path(__file__).resolve()
    for token in tokens:
        if not token.endswith("codex_stop_hook_dispatcher.py"):
            continue
        candidate = token.split("=", 1)[1] if "=" in token else token
        try:
            if Path(candidate).expanduser().resolve() == current:
                return True
        except OSError:
            continue
    return False


def _merged_hook_config(hook_env: dict[str, str]) -> dict[str, str]:
    merged = dict(hook_env)
    for name, legacy_value in LEGACY_DEFAULT_CLINE_KANBAN_ENV.items():
        if merged.get(name, "").strip() == legacy_value:
            merged.pop(name, None)
    return merged


def hook_config_from_hooks_json() -> dict[str, str]:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    stop_groups = data.get("hooks", {}).get("Stop")
    if not isinstance(stop_groups, list):
        return {}
    for group in stop_groups:
        if not isinstance(group, dict):
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            command = hook.get("command") if isinstance(hook, dict) else None
            if not isinstance(command, str):
                continue
            if "review-validate-fix" not in command or "codex_stop_hook_dispatcher.py" not in command:
                continue
            if not _command_targets_current_dispatcher(command):
                continue
            return _merged_hook_config(_command_env(command))
    return {}


def installer_args_from_env() -> list[str]:
    args = ["--configure-stop-hook"]
    # Codex Desktop may invoke a cached hook command after hooks.json has been
    # updated. Prefer the on-disk RVF hook config so dev self-sync does not roll
    # a newer Cline Kanban hook back to stale gui mode.
    hook_env = hook_config_from_hooks_json()
    fork_mode = (
        hook_env.get("CODEX_RVF_FORK_MODE")
        or os.environ.get("CODEX_RVF_FORK_MODE", "")
    ).strip()
    if not fork_mode:
        fork_mode = ""
    if not fork_mode:
        return args
    if fork_mode in {"cline", "kanban", "ck"}:
        fork_mode = "cline-kanban"
    if fork_mode in {"kanban-message", "kanban-inject"}:
        fork_mode = "kanban-followup"
    args.extend(["--fork-mode", fork_mode])
    if fork_mode in {"auto", "cline-kanban", "kanban-followup"}:
        for env_name, option in (
            ("CODEX_RVF_CLINE_KANBAN_START_CMD", "--cline-kanban-start-cmd"),
            ("CODEX_RVF_CLINE_KANBAN_TASK_CMD", "--cline-kanban-task-cmd"),
            ("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT", "--cline-kanban-start-timeout"),
            ("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", "--cline-kanban-tmux-session"),
            ("CODEX_RVF_CLINE_KANBAN_BASE_REF", "--cline-kanban-base-ref"),
            ("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE", "--cline-kanban-worktree-mode"),
            ("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED", "--cline-kanban-auto-review-enabled"),
            ("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", "--cline-kanban-auto-review-mode"),
            ("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE", "--cline-kanban-start-in-plan-mode"),
        ):
            value = (hook_env.get(env_name) or os.environ.get(env_name, "")).strip()
            if value:
                args.extend([option, value])
    return args


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


def latest_assistant_message(path: Path) -> str | None:
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
                if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue
                if record.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") != "assistant":
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


def latest_assistant_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_assistant_message")
    if isinstance(direct, str) and direct:
        return direct
    for path in event_session_paths(event):
        message = latest_assistant_message(path)
        if message:
            return message
    return None


def text_marks_plan_operation(text: str | None) -> bool:
    return isinstance(text, str) and PLAN_OPERATION_TEXT_RE.match(text) is not None


def value_marks_plan_operation(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace(" ", "-")
    return normalized in PLAN_OPERATION_VALUES


def metadata_marks_plan_operation(event: dict[str, Any]) -> bool:
    for key in (
        "operation",
        "operation_name",
        "event",
        "event_name",
        "turn_kind",
        "turn_type",
        "response_type",
    ):
        if value_marks_plan_operation(event.get(key)):
            return True
    source = event.get("source")
    if isinstance(source, dict):
        return metadata_marks_plan_operation(source)
    return False


def event_marks_plan_operation(event: dict[str, Any]) -> bool:
    if metadata_marks_plan_operation(event):
        return True
    return text_marks_plan_operation(latest_assistant_message_from_event(event))


def detect_codex_goal_mode_context(event: dict[str, Any]) -> dict[str, Any] | None:
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from codex_stop_review_validate_fix import codex_goal_mode_context_from_event

    return codex_goal_mode_context_from_event(event)


def is_session_hook_control_event(event: dict[str, Any]) -> bool:
    latest_user = latest_user_message_from_event(event)
    if not latest_user:
        return False
    pattern = re.compile(
        rf"^\s*{re.escape(SESSION_HOOK_CONTROL_KEY)}\s*:\s*([A-Za-z_-]+)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(latest_user)
    if not match:
        return False
    value = match.group(1).strip().lower().replace("_", "-")
    return value in {
        "off",
        "disable",
        "disabled",
        "skip",
        "suppress",
        "on",
        "enable",
        "enabled",
        "resume",
        "status",
        "state",
    }


def installed_hook_env(ledger: RunLedger | None) -> dict[str, str]:
    env = os.environ.copy()
    hook_env = hook_config_from_hooks_json()
    env.update(hook_env)
    if ledger is not None:
        env.update(ledger.env())
    return env


def step_summary(result: dict[str, Any], ledger: RunLedger, name: str) -> dict[str, Any]:
    paths: dict[str, str] = {}
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    if isinstance(stdout, str) and stdout:
        path = ledger.artifact(f"{name}.stdout.txt", stdout)
        if path:
            paths["stdout"] = path
    if isinstance(stderr, str) and stderr:
        path = ledger.artifact(f"{name}.stderr.txt", stderr)
        if path:
            paths["stderr"] = path
    return {
        "cmd": result.get("cmd"),
        "cwd": result.get("cwd"),
        "returncode": result.get("returncode"),
        "duration_seconds": result.get("duration_seconds"),
        "paths": paths,
    }


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def compact_contract_timing_report(path: Path) -> dict[str, Any] | None:
    report = read_json_object(path)
    if not report:
        return None
    timing: dict[str, Any] = {
        "path": str(path),
        "duration_seconds": report.get("duration_seconds"),
        "measured_work_duration_seconds": report.get("measured_work_duration_seconds"),
        "returncode": report.get("returncode"),
    }
    slowest_step = report.get("slowest_step")
    if isinstance(slowest_step, dict):
        timing["slowest_step"] = {
            "label": slowest_step.get("label"),
            "duration_seconds": slowest_step.get("duration_seconds"),
            "percentage_of_total": slowest_step.get("percentage_of_total"),
        }
    groups = report.get("groups")
    if isinstance(groups, list):
        timing["groups"] = groups[:5]
    return timing


def dev_repo_script(repo: Path, rel_path: Path) -> Path:
    repo_root = repo.resolve()
    script = (repo_root / rel_path).resolve()
    try:
        script.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"dev-only sync script escaped dev repo: {rel_path}") from exc
    return script


def dev_sync_step_specs(repo: Path) -> list[tuple[str, Path, list[str], str]]:
    steps = [
        ("contract-check", dev_repo_script(repo, DEV_SYNC_CONTRACT_SCRIPT), [], "contract-check"),
    ]
    if is_truthy(os.environ.get("CODEX_RVF_DEV_SYNC_INSTALL"), default=True):
        steps.append(
            (
                "installer",
                dev_repo_script(repo, DEV_SYNC_INSTALL_SCRIPT),
                installer_args_from_env(),
                "installer",
            )
        )
    return steps


def sync_from_dev_repo(
    repo: Path,
    event: dict[str, Any],
    ledger: RunLedger,
) -> tuple[bool, Path | None, str]:
    python = sys.executable or "python3"
    steps: list[dict[str, Any]] = []
    contract_timing_report_path: Path | None = None
    contract_timing: dict[str, Any] | None = None
    ledger.artifact("stop-event.json", event)
    ledger.event(
        phase="dev-sync",
        event="started",
        status="started",
        reason_code="matched_dev_repo",
        repo=str(repo),
        cwd=str(repo),
        paths={"stop_event": str(ledger.artifact_path("stop-event.json"))},
    )

    try:
        step_specs = dev_sync_step_specs(repo)
    except ValueError as exc:
        reason = str(exc)
        ledger.event(
            phase="dev-sync",
            event="invalid_dev_sync_script",
            status="failed",
            reason_code="invalid_dev_sync_script",
            repo=str(repo),
            cwd=str(repo),
            error=reason,
        )
        ledger.summary(
            status="failed",
            reason_code="invalid_dev_sync_script",
            message=reason,
            repo=str(repo),
            event=event_summary(event),
            steps=steps,
        )
        return False, ledger.summary_path if ledger.available else None, reason

    for name, script, args, component in step_specs:
        extra_env: dict[str, str] = {}
        if name == "contract-check":
            contract_timing_report_path = ledger.artifact_path("contract-check.timing.json")
            extra_env["RVF_CONTRACT_TIMING_REPORT"] = str(contract_timing_report_path)
        if not script.is_file():
            reason = f"missing script: {script}"
            ledger.event(
                component=component,
                phase="dev-sync",
                event="missing_script",
                status="failed",
                reason_code="missing_sync_script",
                repo=str(repo),
                cwd=str(repo),
                script=str(script),
                steps=steps,
                error=reason,
            )
            ledger.summary(
                status="failed",
                reason_code="missing_sync_script",
                message=reason,
                repo=str(repo),
                event=event_summary(event),
                steps=steps,
            )
            return False, ledger.summary_path if ledger.available else None, reason
        result = run_step([python, str(script), *args], cwd=repo, extra_env=extra_env)
        summary = step_summary(result, ledger, name)
        if (
            name == "contract-check"
            and contract_timing_report_path is not None
            and contract_timing_report_path.is_file()
        ):
            summary["paths"]["timing_report"] = str(contract_timing_report_path)
            contract_timing = compact_contract_timing_report(contract_timing_report_path)
        steps.append(summary)
        ledger.event(
            component=component,
            phase="dev-sync",
            event="command_completed",
            status="completed" if result["returncode"] == 0 else "failed",
            reason_code="ok" if result["returncode"] == 0 else "sync_command_failed",
            repo=str(repo),
            cwd=str(repo),
            duration_ms=int(float(result["duration_seconds"]) * 1000),
            paths=summary["paths"],
            cmd=result["cmd"],
            returncode=result["returncode"],
            contract_timing=contract_timing if name == "contract-check" else None,
        )
        if result["returncode"] != 0:
            reason = f"command failed: {' '.join(map(str, result['cmd']))}"
            ledger.summary(
                status="failed",
                reason_code="sync_command_failed",
                message=reason,
                repo=str(repo),
                event=event_summary(event),
                steps=steps,
                dev_sync_steps=steps,
                contract_check_timing=contract_timing,
                contract_check_timing_report_path=(
                    str(contract_timing_report_path)
                    if contract_timing_report_path is not None
                    and contract_timing_report_path.is_file()
                    else None
                ),
            )
            return False, ledger.summary_path if ledger.available else None, reason

    ledger.event(
        phase="dev-sync",
        event="completed",
        status="synced",
        reason_code="synced",
        repo=str(repo),
        cwd=str(repo),
        steps=steps,
        contract_timing=contract_timing,
    )
    ledger.summary(
        status="synced",
        reason_code="synced",
        message="RVF dev repo synced and installed hook is ready.",
        repo=str(repo),
        event=event_summary(event),
        steps=steps,
        dev_sync_steps=steps,
        contract_check_timing=contract_timing,
        contract_check_timing_report_path=(
            str(contract_timing_report_path)
            if contract_timing_report_path is not None and contract_timing_report_path.is_file()
            else None
        ),
    )
    return True, ledger.summary_path if ledger.available else None, "synced"


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


def run_installed_stop_hook(raw_input: str, ledger: RunLedger | None = None) -> int:
    hook = configured_stop_hook()
    python = sys.executable or "python3"
    env = installed_hook_env(ledger)
    try:
        if ledger is not None:
            ledger.event(
                phase="dev-sync",
                event="installed_hook_started",
                status="started",
                reason_code="handoff_to_installed_hook",
                paths={"hook": str(hook)},
            )
        completed = subprocess.run(
            [python, str(hook)],
            input=raw_input,
            capture_output=True,
            text=True,
            env=env,
            timeout=stop_hook_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        if ledger is not None:
            ledger.event(
                phase="dev-sync",
                event="installed_hook_timeout",
                status="failed",
                reason_code="installed_hook_timeout",
                error=coerce_text(exc.stderr),
            )
            ledger.summary(
                status="failed",
                reason_code="installed_hook_timeout",
                message=f"installed hook timed out after {stop_hook_timeout()} seconds",
                hook=str(hook),
            )
        message = (
            "installed hook timed out after "
            f"{stop_hook_timeout()} seconds. stderr={coerce_text(exc.stderr)}"
        )
        if ledger is not None:
            return emit_terminal_payload(
                ledger,
                status="failed",
                reason_code="installed_hook_timeout",
                message=message,
                detail="installed hook timeout",
                hook=str(hook),
            )
        return fail_blocking(
            "review-validate-fix Stop hook dispatcher: " + message,
            124,
        )
    except OSError as exc:
        if ledger is not None:
            ledger.event(
                phase="dev-sync",
                event="installed_hook_exec_failed",
                status="failed",
                reason_code="installed_hook_exec_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            ledger.summary(
                status="failed",
                reason_code="installed_hook_exec_failed",
                message=f"failed to run installed hook {hook}: {exc}",
                hook=str(hook),
            )
        message = f"failed to run installed hook {hook}: {exc}"
        if ledger is not None:
            return emit_terminal_payload(
                ledger,
                status="failed",
                reason_code="installed_hook_exec_failed",
                message=message,
                detail="installed hook exec failed",
                hook=str(hook),
            )
        return fail_blocking(
            "review-validate-fix Stop hook dispatcher: " + message,
            127,
        )

    if completed.returncode == 0:
        if ledger is not None:
            ledger.event(
                phase="dev-sync",
                event="installed_hook_completed",
                status="completed",
                reason_code="installed_hook_completed",
            )
        sys.stdout.write(completed.stdout)
        return 0

    if ledger is not None:
        paths: dict[str, str] = {}
        if completed.stdout:
            path = ledger.artifact("installed-hook.stdout.txt", completed.stdout)
            if path:
                paths["stdout"] = path
        if completed.stderr:
            path = ledger.artifact("installed-hook.stderr.txt", completed.stderr)
            if path:
                paths["stderr"] = path
        ledger.event(
            phase="dev-sync",
            event="installed_hook_failed",
            status="failed",
            reason_code="installed_hook_failed",
            paths=paths,
            returncode=completed.returncode,
        )
        ledger.summary(
            status="failed",
            reason_code="installed_hook_failed",
            message=f"installed hook failed with exit code {completed.returncode}",
            hook=str(hook),
            returncode=completed.returncode,
            paths=paths,
        )
    message = (
        "installed hook failed "
        f"with exit code {completed.returncode}. stderr={completed.stderr.strip()}"
    )
    if ledger is not None:
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="installed_hook_failed",
            message=message,
            detail="installed hook failed",
            hook=str(hook),
            returncode=completed.returncode,
        )
    return fail_blocking(
        "review-validate-fix Stop hook dispatcher: " + message,
        completed.returncode,
    )


def main() -> int:
    raw_input, event = read_input()
    if event is None:
        return run_installed_stop_hook(raw_input)

    cwd = event.get("cwd")
    ledger = start_run(
        "dispatcher",
        repo=str(cwd) if isinstance(cwd, str) else None,
        cwd=str(cwd) if isinstance(cwd, str) else None,
    )
    ledger.artifact("stop-event.json", event)
    if suppress_requested():
        ledger.event(
            phase="dev-sync",
            event="suppressed",
            status="skipped",
            reason_code="suppressed",
            message="CODEX_RVF suppress env requested; skipping dispatcher and installed hook",
        )
        return emit_terminal_payload(
            ledger,
            status="skipped",
            reason_code="suppressed",
            message="CODEX_RVF suppress env requested; skipped dispatcher and installed hook",
            detail="检测到 suppress 环境变量，已跳过 RVF Stop hook。",
        )
    codex_goal_mode = detect_codex_goal_mode_context(event)
    if codex_goal_mode is not None:
        ledger.event(
            phase="dev-sync",
            event="codex_goal_mode_skipped",
            status="skipped",
            reason_code="codex_goal_mode",
            message="Codex main session is in /goal mode; skipping dispatcher and installed hook",
            **codex_goal_mode,
        )
        return emit_terminal_payload(
            ledger,
            status="skipped",
            reason_code="codex_goal_mode",
            message="Codex main session is in /goal mode; skipped dispatcher and installed hook",
            detail="检测到 Codex /goal mode，临时跳过 RVF Stop hook。",
            **codex_goal_mode,
        )
    if event_marks_plan_operation(event):
        ledger.event(
            phase="dev-sync",
            event="plan_operation_skipped",
            status="skipped",
            reason_code="plan_operation",
            message="Codex plan operation completed; skipping RVF Stop hook",
        )
        return emit_terminal_payload(
            ledger,
            status="skipped",
            reason_code="plan_operation",
            message="Codex plan operation completed; skipped RVF Stop hook",
            detail="检测到 Codex plan operation 结束，已跳过 RVF Stop hook。",
        )
    handoff_path_value = handoff_path_from_event(event)
    if handoff_path_value is not None:
        payload = handoff_completion_payload(event, ledger, cwd=cwd)
        if payload is not None:
            try:
                finalize_record = finalize_for_handoff(
                    handoff_path=handoff_path_value,
                    event=event,
                    decision_kind="dispatcher-handoff",
                )
                surface_finalize_record_errors(ledger, finalize_record, payload=payload)
                surface_rvf_analyze_advisory(
                    event=event,
                    ledger=ledger,
                    payload=payload,
                    finalize_record=finalize_record,
                )
            except Exception as exc:
                ledger.event(
                    phase="handoff",
                    event="finalize_failed",
                    status="warning",
                    reason_code="finalize_error",
                    level="warn",
                    error={"kind": type(exc).__name__, "message": str(exc)},
                )
            emit(payload)
            return 0
    latest_user = latest_user_message_from_event(event)
    if latest_user and RVF_ANALYZE_FOLLOWUP_MARKER in latest_user:
        ledger.event(
            phase="dev-sync",
            event="rvf_analyze_followup_trigger_skipped",
            status="skipped",
            reason_code="rvf_analyze_followup_trigger_turn",
            message="rvf-analyze follow-up trigger turn; skipping dispatcher sync and installed hook",
        )
        return emit_terminal_payload(
            ledger,
            status="skipped",
            reason_code="rvf_analyze_followup_trigger_turn",
            message="rvf-analyze follow-up trigger turn; skipped dispatcher sync and installed hook",
            detail="检测到 RVF analyze follow-up trigger，已跳过主 RVF 自动触发。",
        )
    sync_needed, reason, repo = should_sync(event)
    if not sync_needed:
        ledger.event(
            phase="dev-sync",
            event="skipped",
            status="skipped",
            reason_code="sync_not_needed",
            message=reason,
        )
        return run_installed_stop_hook(raw_input, ledger)

    assert repo is not None
    session_sync_needed, session_reason, session_reason_code = should_sync_session_scope(
        event,
        repo,
        ledger,
    )
    if not session_sync_needed:
        # Slice 3 reason-code rename: accept both the legacy
        # `no_session_owned_dirty` (emitted by tracker-disabled fallback) and
        # the new `no_unassigned_review_scope` (emitted by the allocator dry
        # run). The detail message branches on the same set.
        from core.session_scope_allocation.reviewable_unit_diff_tracker import REASON_NO_UNASSIGNED_REVIEW_SCOPE
        clean_scope_codes = ("no_session_owned_dirty", REASON_NO_UNASSIGNED_REVIEW_SCOPE)
        if session_reason_code in clean_scope_codes and is_session_hook_control_event(event):
            ledger.event(
                phase="dev-sync",
                event="session_hook_control_handoff",
                status="skipped",
                reason_code="session_hook_control",
                message="forwarding RVF_STOP_HOOK control message to installed hook",
                repo=str(repo),
                cwd=str(repo),
            )
            return run_installed_stop_hook(raw_input, ledger)
        # Stamp the legacy reason alias on the summary file so downstream
        # `latest_summary` consumers (and integration tests) can grep both
        # spellings during the one-release transition window.
        legacy_alias = (
            "no_session_owned_dirty"
            if session_reason_code == REASON_NO_UNASSIGNED_REVIEW_SCOPE
            else None
        )
        return emit_terminal_payload(
            ledger,
            status="skipped",
            reason_code=session_reason_code,
            message=session_reason,
            detail=(
                "session manifest 构建失败，已跳过 RVF dev sync 和 installed hook"
                if session_reason_code == "session_manifest_failed"
                else "当前 chat session 没有未分配的 review scope，跳过 RVF dev sync 和自动 review"
                if session_reason_code in clean_scope_codes
                else "RVF dev sync skipped"
            ),
            repo=str(repo),
            event=event_summary(event),
            reason_code_legacy_alias=legacy_alias,
        )

    synced, log_path, sync_reason = sync_from_dev_repo(repo, event, ledger)
    if not synced:
        return emit_terminal_payload(
            ledger,
            status="failed",
            reason_code="sync_command_failed",
            message=(
                "RVF dev sync failed; skipped installed hook to avoid using a stale "
                f"installed plugin skill. reason={sync_reason}"
            ),
            detail="RVF dev sync failed",
            repo=str(repo),
            event=event_summary(event),
            summary_path=str(log_path) if log_path is not None else None,
        )

    return run_installed_stop_hook(raw_input, ledger)


if __name__ == "__main__":
    raise SystemExit(main())
