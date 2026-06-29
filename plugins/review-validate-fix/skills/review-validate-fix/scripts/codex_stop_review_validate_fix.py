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
import hashlib
import base64
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — 把 pyroot 加入 sys.path，供 adapters.* import
from adapters.codex.codex_gui_fork_app_server_bridge import (  # noqa: E402 — codex fork 执行缝（S9a 抽出）
    app_server_fork_requests,
    can_connect_app_server_socket,
    parent_thread_name_from_app_server,
    path_is_relative_to,
    run_app_server_fork,
    select_existing_app_server_socket_for_metadata,
)
from adapters.codex.transcript import (  # noqa: E402 — codex rollout / goal-mode 解析缝（S9b 抽出）
    codex_goal_mode_context_from_event,
    latest_user_message,
    session_id_from_path,
    user_messages_containing,
)
from core.run_ledger.run_ledger import (
    RunLedger,
    log_root,
    normalize_rvf_backend,
    rvf_state_fields,
    safe_token,
    skill_deploy_metadata,
    start_run,
)
from rvf_handoff import (
    handoff_completion_payload,
    handoff_path_from_event,
    notify_kanban_followup_stranded,
    resolve_kanban_task_url,
)
from rvf_run_finalize import finalize_for_handoff, surface_finalize_record_errors
from rvf_analyze_advisory import (
    RVF_ANALYZE_FOLLOWUP_MARKER,
    current_kanban_task_id,
    surface_rvf_analyze_advisory,
)

try:
    # Vendored single-file reader (same copy rvf_user_prompt_submit uses): a
    # structured "which skill did the user explicitly invoke this turn" read from
    # the Codex rollout, plus an anchored text fallback. Used by the turn-scoped
    # `$rvf-analyze` re-entrancy guard below. Optional: stay resilient on Claude /
    # missing transcript / vendor absent.
    import codex_invoked_skill
except Exception:  # pragma: no cover - structured read is best-effort
    codex_invoked_skill = None
from kanban_followup_lock import (
    STATUS_ACTIVE as KANBAN_FOLLOWUP_LOCK_ACTIVE,
    STATUS_STALE as KANBAN_FOLLOWUP_LOCK_STALE,
    clear_marker as clear_kanban_followup_lock,
    marker_status as kanban_followup_lock_status,
    read_marker as read_kanban_followup_lock,
    clear_pending_marker as clear_kanban_followup_pending,
    iter_pending_markers as iter_kanban_followup_pending,
    pending_status as kanban_followup_pending_status,
    read_pending_marker as read_kanban_followup_pending,
    stamp_pending_notified as stamp_kanban_followup_pending_notified,
    write_pending_marker as write_kanban_followup_pending,
)
from session_manifest import build_manifest
from core.session_scope_allocation.reviewable_unit_diff_tracker import (
    LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
    LEGACY_REASON_SESSION_OWNED_DIRTY,
    REASON_NO_UNASSIGNED_REVIEW_SCOPE,
    REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
    REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
    _disabled as _tracker_disabled,
    _list_committed_round_changed_paths,
    _list_round_skip_review_commit_shas,
    _manual_suppression_scope_probe,
    allocate_review_scope,
    find_manual_rvf_run_for_scope_hash,
    invalidate_reviewed_units_for_run,
    lease_release,
    sweep_stale,
)
from review_reopen_marker import (
    STATUS_ACTIVE as REVIEW_REOPEN_ACTIVE,
    clear_review_reopen_marker,
    read_review_reopen_marker,
    review_reopen_status,
)
from round_baseline_marker import resolve_round_baseline_head
import review_highwater_marker
from cline_kanban_client import (
    DEFAULT_START_CMD as DEFAULT_CLINE_KANBAN_START_CMD,
    DEFAULT_START_TIMEOUT_SECONDS as DEFAULT_CLINE_KANBAN_START_TIMEOUT_SECONDS,
    DEFAULT_TASK_CMD as DEFAULT_CLINE_KANBAN_TASK_CMD,
    DEFAULT_TMUX_SESSION as DEFAULT_CLINE_KANBAN_TMUX_SESSION,
)
from session_label import (
    DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS,
    first_user_message,
    parent_conversation_fallback_chars,
    single_line_excerpt,
    strip_codex_user_message_preamble,
    text_from_message_payload,
)
import rvf_dispatch_flow as dispatch_flow
import rvf_prep_file
import rvf_bootstrap_confirm
from core.host_adapter.host_transcript_format_detection import HOST_CLAUDE, HOST_CODEX, detect_transcript_format
import rvf_parent_context
from rvf_dispatch_prompts import (
    cline_kanban_artifact_reference_lines,
    dispatch_scope_of_work_text,
)


class BootstrapConfirmationRequired(Exception):
    """Raised when bootstrap dispatch is blocked pending user yes/Yes/YES confirmation."""

    def __init__(self, decision: rvf_bootstrap_confirm.Decision, marker_path: Path):
        self.decision = decision
        self.marker_path = marker_path
        super().__init__(decision.reason)


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = SKILL_DIR / "state"
DEFAULT_SESSION_HOOK_STATE_DIR = SKILL_DIR / "state" / "session-hook"
DEFAULT_CLINE_KANBAN_CLIENT = SKILL_DIR / "scripts" / "cline_kanban_client.py"
DEFAULT_CLINE_KANBAN_STATE_DIR = Path.home() / ".cline" / "kanban"
DEFAULT_PREPARE_REVIEW_RUN = SKILL_DIR / "scripts" / "prepare_review_run.py"
KANBAN_TASK_SUPPRESSIONS_DIRNAME = "kanban-task-suppressions"
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
CLINE_KANBAN_TASK_MARKER = "RVF_CLINE_KANBAN_TASK"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"
CLINE_KANBAN_WORKTREE_MODES = {"branch", "inplace"}
DEFAULT_CLINE_KANBAN_WORKTREE_MODE = "branch"
# 主 agent 最终回复里 `RVF_HANDOFF_FILE:` marker 之后那段摘要的固定结构指令。
# 单一来源，供 fork / kanban-followup / kanban-dispatch 三处 prompt builder 复用，
# 避免再次像历史那样三份拷贝各自漂移成「1-3 句」自由散文、导致输出成无结构 paragraph。
# 与 references/handoff-template.md 的 `Reviewers：`/`Validate/fixers：` 两行结构、
# 以及 check_review_output.py 已保留的同名标签一致。
HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION = (
    "空一行后按固定结构分两行追加极短中文摘要："
    "`Reviewers：<reviewers 检查了什么、发现几项或没问题>` 一行、"
    "`Validate/fixers：<validate/fixers 验证/修复/驳回/升级了什么>` 一行，"
    "每行各自一句、不要挤成一段"
)
# 父会话对话 context 注入（dispatch 期把父 transcript 抽成可读 blob 写进 run
# artifacts，供 cline-kanban child agent 在 review 前阅读作背景；不重定义 scope）。
PARENT_CONTEXT_ENV = "RVF_PARENT_CONTEXT"
"""开关：默认开启；设 ``0`` / ``false`` / ``no`` / ``off`` 关闭父对话 context 生成。"""
PARENT_CONTEXT_MAX_BYTES_ENV = "RVF_PARENT_CONTEXT_MAX_BYTES"
"""总字节上限覆盖；缺省用 rvf_parent_context.DEFAULT_MAX_BYTES (64KB)，超限保留最近内容。"""
PARENT_CONTEXT_ARTIFACT_NAME = "parent-conversation-context.md"
"""run artifacts 中父对话 context 的文件名，与 task prompt / review-env 引用一致。"""
PARENT_CONTEXT_PROMPT_KEY = "RVF_PARENT_CONVERSATION_CONTEXT"
"""task prompt / review-env 中标记父对话 context 路径的键名。"""
DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS = 60 * 60
KANBAN_FOLLOWUP_LEASE_TTL_ENV = "RVF_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS"
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
SUPPRESS_STOP_HOOK_MARKER = "RVF_SUPPRESS_STOP_HOOK=1"
MANUAL_RVF_COMPLETED_AT_KEY = "manual_rvf_completed_at"
MANUAL_RVF_RUN_ID_KEY = "manual_rvf_run_id"
MANUAL_RVF_MARKER_KEYS = (
    MANUAL_RVF_COMPLETED_AT_KEY,
    MANUAL_RVF_RUN_ID_KEY,
    "manual_rvf_updated_at",
    "manual_rvf_expires_at",
    "manual_rvf_repo",
    "manual_rvf_head",
    "manual_rvf_dirty_hash",
)
MANUAL_RVF_MARKER_TTL_SECONDS = 12 * 60 * 60
DEFAULT_RVF_MODE = "fork"
DEFAULT_FORK_LAUNCH_MODE = "auto"
SUPPRESS_ENV_NAMES = (
    "RVF_SUPPRESS",
    "RVF_SUPPRESS_STOP_HOOK",
)
# detached $rvf-analyze 线程注入的标记 env。语义上与 SUPPRESS_ENV_NAMES 区分开：
# 这是「这是 analyze 线程自己的 Stop event」的显式信号，用于 evaluate_stop_event
# 早退守卫，短路所有昂贵 gate，避免后台 analyze 递归触发新一轮 RVF。
RVF_ANALYZE_THREAD = "RVF_ANALYZE_THREAD"
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
SESSION_SCOPE_PATH_KEYS = tuple(key for key in SESSION_PATH_KEYS if key != "log_path")
PLAN_DOC_REVIEW_DIR_PREFIXES = ("docs/", "doc/", ".claude/plans/")
PLAN_DOC_REVIEW_NAME_MARKERS = (
    "plan",
    "blueprint",
    "prd",
    "proposal",
    "decision",
    "scaffold",
    "handoff",
    "roadmap",
    "rfc",
)


@dataclass(frozen=True)
class GateResult:
    status: str
    repo: str | None
    output: str


@dataclass(frozen=True)
class StopDecision:
    action: str
    reason_code: str
    repo: str | None = None
    cwd: str | None = None
    parent_thread_id: str | None = None
    parent_thread_path: Path | None = None
    backend: str = "off"
    message: str = ""
    summary_fields: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    status: str = "skipped"


@dataclass(frozen=True)
class ProviderHealthRequirement:
    provider: str
    reason: str
    command: tuple[str, ...]
    remediation: str


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


def stop_hook_rvf_state_fields(
    *,
    phase: str,
    backend: str | None = None,
    backend_raw: str | None = None,
    prepare_metadata: dict[str, Any] | None = None,
    handoff_path: str | Path | None = None,
    completion_gate: str | None = None,
) -> dict[str, Any]:
    metadata = prepare_metadata or {}
    return rvf_state_fields(
        phase=phase,
        backend=backend,
        backend_raw=backend_raw,
        scope_contract_path=metadata.get("scope_contract"),
        scope_of_work_path=metadata.get("scope_of_work_file"),
        review_packet_path=metadata.get("review_packet"),
        session_manifest_path=metadata.get("session_manifest_file"),
        handoff_path=handoff_path,
        completion_gate=completion_gate,
    )


def state_dir() -> Path:
    return log_root()


def kanban_task_suppression_path(task_id: str) -> Path:
    return state_dir() / KANBAN_TASK_SUPPRESSIONS_DIRNAME / f"{safe_state_key(task_id)}.json"


def write_kanban_task_suppression(
    *,
    task_id: str,
    cwd: str,
    ledger: RunLedger,
) -> str:
    path = kanban_task_suppression_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "suppress_stop_hook": True,
        "reason": "rvf-created-cline-kanban-task",
        "repo": cwd,
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def read_kanban_task_suppression(task_id: str) -> dict[str, Any] | None:
    path = kanban_task_suppression_path(task_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


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


def is_falsey(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"0", "false", "no", "n", "off", "skip", "disabled"}


def provider_health_check_enabled() -> bool:
    return not is_falsey(os.environ.get("RVF_PROVIDER_HEALTH_CHECK"))


def provider_health_timeout_seconds() -> float:
    raw = os.environ.get("RVF_PROVIDER_HEALTH_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 12.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 12.0


def codex_bin() -> str:
    return os.environ.get("CODEX_RVF_CODEX_BIN", "codex")


def parent_context_enabled() -> bool:
    """父会话对话 context 注入是否开启（默认开启，``RVF_PARENT_CONTEXT=0/false`` 关闭）。"""
    return not is_falsey(os.environ.get(PARENT_CONTEXT_ENV))


def parent_context_max_bytes() -> int:
    """父对话 context 总字节预算；非法/缺省回退 rvf_parent_context.DEFAULT_MAX_BYTES。"""
    raw = os.environ.get(PARENT_CONTEXT_MAX_BYTES_ENV)
    if raw is None or not raw.strip():
        return rvf_parent_context.DEFAULT_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError:
        return rvf_parent_context.DEFAULT_MAX_BYTES
    return value if value > 0 else rvf_parent_context.DEFAULT_MAX_BYTES


def freeze_parent_conversation_context(
    *,
    parent_thread_path: Path | None,
    ledger: RunLedger,
    cwd: str,
) -> str | None:
    """渲染父会话对话 context 并写进 run artifacts；返回 artifact 路径或 None。

    完全 fail-open：开关关闭、父 transcript 缺失、渲染为空、写入失败任意一种都
    返回 None 且不抛异常——绝不阻塞 dispatch。
    """
    if not parent_context_enabled():
        return None
    if parent_thread_path is None:
        return None
    try:
        blob = rvf_parent_context.render_parent_context(
            parent_thread_path,
            max_bytes=parent_context_max_bytes(),
        )
    except Exception as exc:  # noqa: BLE001 - fail-open，不阻塞 dispatch
        ledger.event(
            phase="prepare",
            event="parent_conversation_context_failed",
            status="warning",
            reason_code="parent_conversation_context_render_error",
            repo=cwd,
            cwd=cwd,
            error=str(exc),
        )
        return None
    if not blob:
        return None
    header = (
        "# 父会话对话 Context（仅作 review 背景）\n\n"
        f"- 来源 transcript：`{parent_thread_path}`\n"
        "- 用途：让本 child agent 在 review 前了解父会话的对话/推理脉络。\n"
        "- 边界：**仅作背景**，不得用本文件重定义 review scope；scope 仍以 "
        "`$RVF_SCOPE_CONTRACT` 为准。Codex reasoning 加密，标 `<encrypted reasoning>`；"
        "tool 输出已轻压缩。\n\n"
        "---\n\n"
    )
    artifact_path = ledger.artifact(PARENT_CONTEXT_ARTIFACT_NAME, header + blob)
    if artifact_path:
        ledger.event(
            phase="prepare",
            event="parent_conversation_context_frozen",
            status="completed",
            reason_code="parent_conversation_context_frozen",
            repo=cwd,
            cwd=cwd,
            paths={"parent_conversation_context": artifact_path},
        )
    return artifact_path


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


def event_session_scope_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_SCOPE_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def first_readable_session_path(event: dict[str, Any]) -> Path | None:
    for path in event_session_scope_paths(event):
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
        return resolved
    return None


def latest_user_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_user_message")
    if isinstance(direct, str) and direct:
        return direct

    for path in event_session_paths(event):
        message = latest_user_message(path)
        if message:
            return message
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


def parent_thread_path_for_origin(
    event: dict[str, Any],
    *,
    ledger: RunLedger | None = None,
    repo: str | None = None,
    cwd: str | None = None,
) -> Path | None:
    """``parent_thread_path_from_event`` 的 origin.json 友好扩展版。

    解析顺序：
    1. ``parent_thread_path_from_event(event)`` — Codex ``session_meta`` 验证过
       的路径；命中即返回（保持 Codex 既有行为）。
    2. 退到 ``event_session_paths(event)`` 中任何 **存在** 的文件 —— 不要求
       ``session_meta`` 命中，这样 Claude Code transcript（无 session_meta，
       但 file 存在）也能被 origin.json 收录，让 trajectory_capture 后续能
       探测到 host 与切片。
    3. 若 event 完全没有 session path 字段 → emit 诊断 ledger event
       ``origin_metadata_missing_transcript_path`` 便于事后定位。

    与上层 ``parent_conversation_origin`` / ``write_dispatch_prep_file`` 解耦：
    此 helper 只决定 transcript path，不影响 ``session_id`` / ``transcript_origin_label``
    等下游字段（它们已经各自处理 None）。
    """
    primary = parent_thread_path_from_event(event)
    if primary is not None:
        return primary
    for path in event_session_paths(event):
        try:
            expanded = path.expanduser()
            if expanded.is_file():
                resolved = expanded.resolve()
                if ledger is not None:
                    ledger.event(
                        phase="prepare",
                        event="origin_metadata_transcript_path_fallback",
                        status="ok",
                        reason_code="origin_metadata_transcript_path_fallback",
                        repo=repo,
                        cwd=cwd,
                        paths={"transcript_path": str(resolved)},
                    )
                return resolved
        except OSError:
            continue
    if ledger is not None and not list(event_session_paths(event)):
        ledger.event(
            phase="prepare",
            event="origin_metadata_missing_transcript_path",
            status="warning",
            reason_code="origin_metadata_missing_transcript_path",
            repo=repo,
            cwd=cwd,
        )
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


def short_identifier(value: str | None, fallback: str = "unknown") -> str:
    if not value:
        return fallback
    stripped = value.strip()
    if not stripped:
        return fallback
    first_segment = stripped.split("-", 1)[0]
    if re.match(r"^[A-Fa-f0-9]{8,}(?:-|$)", stripped):
        return first_segment[:12]
    return stripped[:32]


def short_run_ref(run_id: str) -> str:
    match = re.search(r"-([A-Fa-f0-9]{8,})$", run_id)
    if match:
        return match.group(1)[:12]
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]


def transcript_origin_label(path: Path | None, session_id: str | None) -> str | None:
    if path is None:
        return None
    stem = path.stem
    if stem.startswith("rollout-"):
        stem = stem.removeprefix("rollout-")
    match = re.match(
        r"(?P<started>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<session>[A-Za-z0-9]{8,12})",
        stem,
    )
    if match:
        return f"{match.group('started')} {match.group('session')}"
    if session_id:
        return short_identifier(session_id)
    return path.name


def quoted_prompt_session_name(path: Path | None) -> str | None:
    if path is None:
        return None
    message = first_user_message(path)
    if not message:
        return None
    excerpt = single_line_excerpt(message, parent_conversation_fallback_chars())
    if not excerpt:
        return None
    return f'"{excerpt}"'


def codex_session_index_path() -> Path:
    override = os.environ.get("CODEX_SESSION_INDEX_PATH")
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".codex" / "session_index.jsonl"


def session_index_thread_name(session_id: str | None) -> str | None:
    if not session_id:
        return None
    path = codex_session_index_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    match: str | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("id") != session_id:
            continue
        thread_name = record.get("thread_name")
        if isinstance(thread_name, str) and thread_name.strip():
            match = thread_name.strip()
    return match


def parent_conversation_origin(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    run_id: str,
    parent_thread_name: str | None = None,
    name_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_id = parent_session_id or (
        session_id_from_path(parent_thread_path) if parent_thread_path is not None else None
    )
    transcript_path = str(parent_thread_path) if parent_thread_path is not None else None
    # host_kind 复用 executor 选择的同一探测（detect_transcript_format）：同输入→同结果、
    # 不与 resolve_cline_kanban_agent_id 漂移。它决定 session_ref_fallback 前缀与 codex_url
    # 是否成立——避免把 Claude Code 会话硬贴成 "Codex" 标签 / `codex://` URL。
    host_kind: str | None = None
    if parent_thread_path is not None:
        try:
            host_kind = detect_transcript_format(parent_thread_path)
        except Exception:
            host_kind = None
    name_source = "app_server_name"
    label = parent_thread_name.strip() if isinstance(parent_thread_name, str) else ""
    if not label:
        label = session_index_thread_name(session_id) or ""
        name_source = "session_index_thread_name" if label else "session_ref_fallback"
    if not label:
        label = quoted_prompt_session_name(parent_thread_path) or ""
        name_source = "first_user_prompt_fallback" if label else "session_ref_fallback"
    if not label:
        # 前三级 lookup 全是 Codex-only schema（session_index / Codex 记录的首条 user
        # message），在 Claude 会话上必然落空。这里按 host_kind 给出正确前缀；host 未知
        # （transcript 缺失/不可识别）时仍兜底 "Codex"，与既有 Codex-only 用例零回归。
        host_prefix = "Claude" if host_kind == HOST_CLAUDE else "Codex"
        label = f"{host_prefix} {transcript_origin_label(parent_thread_path, session_id) or short_identifier(session_id)}"
        name_source = "session_ref_fallback"
    run_ref = short_run_ref(run_id)
    return {
        "label": label,
        "task_title": f"RVF from {label} run {run_ref}",
        "name_source": name_source,
        "name_lookup": name_lookup,
        "session_id": session_id,
        "session_short_id": short_identifier(session_id),
        "host_kind": host_kind,
        # codex:// 只对 Codex（含 host 未知兜底）成立；Claude Code 会话无该 scheme，
        # 置 None（prompt block 经 value_or_unavailable 渲染为 <unavailable>），
        # 其「打开」入口由已输出的 RVF_PARENT_TRANSCRIPT_PATH 承担。
        "codex_url": (
            f"codex://local/{session_id}" if session_id and host_kind != HOST_CLAUDE else None
        ),
        "transcript_path": transcript_path,
        "transcript_file": parent_thread_path.name if parent_thread_path is not None else None,
        "run_id": run_id,
        "run_ref": run_ref,
    }


def parent_conversation_host_label(host_kind: str | None) -> str:
    """父会话 harness 的人类可读名，用于 prompt-block 文案标题。

    Claude transcript → ``Claude Code``；其余（Codex / host 未知 / parent_origin 缺
    ``host_kind`` 键）→ ``Codex``，与 ``parent_conversation_origin`` 的兜底口径一致。
    """
    return "Claude Code" if host_kind == HOST_CLAUDE else "Codex"


def source_origin_for_kanban_task(
    *,
    task_id: str,
    attempt_id: str | None,
    task_title: str | None,
    task_title_source: str | None,
    fallback_origin: dict[str, Any],
) -> dict[str, Any]:
    title = task_title.strip() if isinstance(task_title, str) else ""
    origin = dict(fallback_origin)
    if title:
        label = title
        name_source = task_title_source or "cline_kanban_task_title"
    else:
        label = f"Cline Kanban task {task_id}"
        name_source = "cline_kanban_task_id_fallback"
    origin.update(
        {
            "label": label,
            "name_source": name_source,
            "source_kind": "cline-kanban-task",
            "kanban_task_id": task_id,
            "kanban_attempt_id": attempt_id,
            "kanban_task_title": title or None,
            "kanban_task_title_source": task_title_source,
            "source_session_label": fallback_origin.get("label"),
            "source_session_name_source": fallback_origin.get("name_source"),
        }
    )
    return origin


def value_or_unavailable(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None:
        text = str(value).strip()
        if text:
            return text
    return "<unavailable>"


def parent_origin_prompt_block(
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    parent_conversation_ref = value_or_unavailable(
        parent_origin.get("label") or parent_origin.get("session_id")
    )
    parent_conversation_source = value_or_unavailable(parent_origin.get("name_source"))
    parent_codex_url = value_or_unavailable(parent_origin.get("codex_url"))
    parent_transcript_path = value_or_unavailable(parent_origin.get("transcript_path"))
    parent_transcript_file = value_or_unavailable(parent_origin.get("transcript_file"))
    parent_origin_path = value_or_unavailable(origin_path)
    host_label = parent_conversation_host_label(parent_origin.get("host_kind"))
    lines = [
        f"Original {host_label} conversation metadata:\n"
        f"RVF_PARENT_CONVERSATION_REF: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME_SOURCE: {parent_conversation_source}\n"
        f"RVF_PARENT_CODEX_URL: {parent_codex_url}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {parent_transcript_path}\n"
        f"RVF_PARENT_TRANSCRIPT_FILE: {parent_transcript_file}\n"
        f"RVF_ORIGIN_METADATA: {parent_origin_path}\n"
    ]
    if parent_origin.get("source_kind") == "cline-kanban-task":
        lines.append(
            "RVF_PARENT_SOURCE_KIND: cline-kanban-task\n"
            f"RVF_PARENT_KANBAN_TASK_ID: {value_or_unavailable(parent_origin.get('kanban_task_id'))}\n"
            f"RVF_PARENT_KANBAN_ATTEMPT_ID: {value_or_unavailable(parent_origin.get('kanban_attempt_id'))}\n"
            f"RVF_PARENT_KANBAN_TASK_TITLE: {value_or_unavailable(parent_origin.get('kanban_task_title'))}\n"
            "RVF_PARENT_KANBAN_TASK_TITLE_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('kanban_task_title_source'))}\n"
            "RVF_PARENT_SOURCE_SESSION_REF: "
            f"{value_or_unavailable(parent_origin.get('source_session_label'))}\n"
            "RVF_PARENT_SOURCE_SESSION_NAME_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('source_session_name_source'))}\n"
        )
    lines.append(
        "\n"
        "维护 handoff.md 时，`## Origin` 必须逐字保留上面的 original "
        f"{host_label} conversation name/ref、name source、codex URL、transcript path "
        "和 origin metadata path；如果存在 `RVF_PARENT_KANBAN_TASK_ID`，还必须写入 "
        "`source Kanban task id` 和 `source Kanban attempt id`，以便任务改名后仍可反查"
        "当前 task title；不要把 `RVF_PARENT_SESSION_ID` 当成 conversation name source。"
    )
    return "".join(lines)


def parent_origin_summary_fields(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    parent_name_lookup: dict[str, Any],
    origin_path: str | None,
) -> dict[str, Any]:
    return {
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "parent_conversation_ref": parent_origin.get("label"),
        "parent_conversation_name": parent_origin.get("label"),
        "parent_conversation_name_source": parent_origin.get("name_source"),
        "parent_thread_name_lookup": parent_name_lookup,
        "parent_codex_url": parent_origin.get("codex_url"),
        "parent_origin_path": origin_path,
        "parent_transcript_file": parent_origin.get("transcript_file"),
        "parent_source_kind": parent_origin.get("source_kind"),
        "parent_kanban_task_id": parent_origin.get("kanban_task_id"),
        "parent_kanban_attempt_id": parent_origin.get("kanban_attempt_id"),
        "parent_kanban_task_title": parent_origin.get("kanban_task_title"),
        "parent_kanban_task_title_source": parent_origin.get("kanban_task_title_source"),
        "parent_source_session_ref": parent_origin.get("source_session_label"),
        "parent_source_session_name_source": parent_origin.get("source_session_name_source"),
    }


def plugin_deploy_prompt_block() -> str:
    metadata = skill_deploy_metadata(SKILL_DIR)
    deploy_label = metadata.get("deploy_label") or "unknown"
    skill_heading = metadata.get("skill_heading") or "<unknown>"
    return (
        f"RVF_PLUGIN_DEPLOY: {deploy_label}\n"
        f"RVF_PLUGIN_SKILL_HEADING: {skill_heading}\n"
    )


def add_parent_origin_to_rvf_fork_prompt(
    prompt: str,
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    if RVF_FORK_MARKER not in prompt:
        return prompt
    if "RVF_PARENT_CONVERSATION_NAME_SOURCE:" in prompt:
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        f"{parent_origin_prompt_block(parent_origin=parent_origin, origin_path=origin_path)}"
    )


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


def write_session_hook_state(session_id: str, state: dict[str, Any]) -> Path:
    path = session_hook_state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_manual_rvf_session_marker(
    *,
    session_id: str,
    run_id: str,
    repo: str | Path | None = None,
    completed_at: str | None = None,
    ttl_seconds: int = MANUAL_RVF_MARKER_TTL_SECONDS,
) -> Path:
    timestamp = completed_at or datetime.now(timezone.utc).isoformat()
    try:
        completed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        completed = datetime.now(timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    expires_at = datetime.fromtimestamp(completed.timestamp() + ttl_seconds, timezone.utc).isoformat()
    state = read_session_hook_state(session_id) or {}
    marker_update = {
        MANUAL_RVF_COMPLETED_AT_KEY: timestamp,
        MANUAL_RVF_RUN_ID_KEY: run_id,
        "manual_rvf_updated_at": datetime.now(timezone.utc).isoformat(),
        "manual_rvf_expires_at": expires_at,
    }
    snapshot = manual_rvf_dirty_snapshot(Path(repo).expanduser().resolve()) if repo is not None else None
    if snapshot is not None:
        marker_update.update(snapshot)
    state.update(marker_update)
    return write_session_hook_state(session_id, state)


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def manual_rvf_dirty_snapshot(repo: Path) -> dict[str, str] | None:
    completed_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_root.returncode != 0:
        return None
    root = Path(completed_root.stdout.strip()).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0 or status.returncode != 0 or diff.returncode != 0:
        return None
    digest = hashlib.sha256()
    digest.update(head.stdout.encode("utf-8", "replace"))
    digest.update(b"\0")
    digest.update(status.stdout.encode("utf-8", "replace"))
    digest.update(b"\0")
    digest.update(diff.stdout.encode("utf-8", "replace"))
    for raw_line in status.stdout.splitlines():
        if not raw_line.startswith("?? ") or len(raw_line) < 4:
            continue
        rel = raw_line[3:].strip()
        path = root / rel
        if not path.is_file():
            continue
        digest.update(b"\0untracked\0")
        digest.update(rel.encode("utf-8", "replace"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            return None
    return {
        "manual_rvf_repo": str(root),
        "manual_rvf_head": head.stdout.strip(),
        "manual_rvf_dirty_hash": digest.hexdigest(),
    }


def read_manual_rvf_session_marker(session_id: str, repo: str | Path | None = None) -> dict[str, Any] | None:
    state = read_session_hook_state(session_id)
    if state is None:
        return None

    completed_at = state.get(MANUAL_RVF_COMPLETED_AT_KEY)
    run_id = state.get(MANUAL_RVF_RUN_ID_KEY)
    expires_at = state.get("manual_rvf_expires_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        return None
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    if isinstance(expires_at, str) and expires_at.strip():
        expires = parse_iso_datetime(expires_at)
        if expires is None or datetime.now(timezone.utc) >= expires:
            return None
    else:
        completed = parse_iso_datetime(completed_at)
        if completed is None:
            return None
        if datetime.now(timezone.utc).timestamp() - completed.timestamp() >= MANUAL_RVF_MARKER_TTL_SECONDS:
            return None

    if repo is not None:
        snapshot = manual_rvf_dirty_snapshot(Path(repo).expanduser().resolve())
        if snapshot is None:
            return None
        for key in ("manual_rvf_repo", "manual_rvf_head", "manual_rvf_dirty_hash"):
            if state.get(key) != snapshot[key]:
                return None

    return {
        "session_id": session_id,
        MANUAL_RVF_COMPLETED_AT_KEY: completed_at,
        MANUAL_RVF_RUN_ID_KEY: run_id,
        "manual_rvf_expires_at": expires_at,
        "manual_rvf_repo": state.get("manual_rvf_repo"),
        "manual_rvf_head": state.get("manual_rvf_head"),
        "manual_rvf_dirty_hash": state.get("manual_rvf_dirty_hash"),
        "state_path": str(session_hook_state_path(session_id)),
    }


def clear_manual_rvf_session_marker(session_id: str) -> Path | None:
    state = read_session_hook_state(session_id)
    path = session_hook_state_path(session_id)
    if state is None:
        return None

    for key in MANUAL_RVF_MARKER_KEYS:
        state.pop(key, None)

    if set(state) <= {"session_id"}:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path

    return write_session_hook_state(session_id, state)


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
        state = read_session_hook_state(session_id) or {}
        if any(key in state for key in MANUAL_RVF_MARKER_KEYS):
            state.pop("enabled", None)
            state.pop("control", None)
            state.pop("latest_user_message", None)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            return write_session_hook_state(session_id, state)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return path
        return path

    state = read_session_hook_state(session_id) or {}
    state.update(
        {
            "enabled": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "control": SESSION_HOOK_CONTROL_KEY,
            "latest_user_message": latest_user,
        }
    )
    return write_session_hook_state(session_id, state)


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
        f"{plugin_deploy_prompt_block()}"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CWD: {cwd_line}\n"
        f"RVF_TARGET_REPO: {repo}\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后 fork 出来的 "
        "review-validate-fix 会话。请基于完整父会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}\n\n"
        "如果父会话历史里出现 `RVF_STOP_HOOK: off`、`RVF_STOP_HOOK: on` "
        "或 `RVF_STOP_HOOK: status`、`RVF_STOP_HOOK_CHANNEL: ...` 这样的行，"
        "请只把它们视为 Stop hook "
        "会话控制元数据；不要把它们当成用户分配的代码任务、review issue、"
        "research 对象或 scope-of-work 内容。\n\n"
        "从准备阶段开始创建并持续维护 run artifact `handoff.md`。完成后最终回复"
        "第一行输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，"
        f"{HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION}，也不要在正文里重复 "
        "handoff 文件内容。Stop hook 会把 `RVF_HANDOFF_FILE` marker 作为完成信号，"
        "run 结束时发送 OS 系统通知（不再自动用编辑器打开 handoff）。"
    )


def git_branch_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    branch = completed.stdout.strip()
    return branch or None


def cline_kanban_worktree_mode_from_env() -> str:
    raw = os.environ.get("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE", DEFAULT_CLINE_KANBAN_WORKTREE_MODE)
    value = (raw or DEFAULT_CLINE_KANBAN_WORKTREE_MODE).strip().lower()
    if not value:
        return DEFAULT_CLINE_KANBAN_WORKTREE_MODE
    if value not in CLINE_KANBAN_WORKTREE_MODES:
        allowed = ", ".join(sorted(CLINE_KANBAN_WORKTREE_MODES))
        raise ValueError(f"invalid CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE={raw!r}; expected one of: {allowed}")
    return value


def automatic_cline_kanban_worktree_mode() -> str:
    return DEFAULT_CLINE_KANBAN_WORKTREE_MODE


def automatic_cline_kanban_base_ref(cwd: str) -> str | None:
    if not cwd:
        return None
    try:
        return git_head(cwd)
    except Exception:
        return None


def dispatch_prep_target_flow(mode: str, *, cline_kanban_worktree_mode: str | None = None) -> str:
    normalized = mode.strip().lower()
    if normalized in {"cline-kanban", "cline", "kanban", "ck"}:
        return "flow-2-inplace" if cline_kanban_worktree_mode == "inplace" else "flow-2-branch"
    if normalized in {"kanban-followup", "kanban-message", "kanban-inject"}:
        return "flow-1-self-rising"
    return "flow-3-inplace"


def dispatch_prep_summary_fields(
    record: rvf_prep_file.PrepFileRecord,
    *,
    target_flow: str,
) -> dict[str, Any]:
    target_worktree = record.payload.get("target_worktree")
    target_kanban_task_id = record.payload.get("target_kanban_task_id")
    return {
        "rvf_dispatch_token": record.token,
        "rvf_dispatch_prep_file_path": str(record.path),
        "rvf_dispatch_prep_status": "written",
        "rvf_dispatch_target_flow": target_flow,
        "rvf_dispatch_target_worktree": target_worktree if isinstance(target_worktree, str) else None,
        "rvf_dispatch_target_kanban_task_id": (
            target_kanban_task_id if isinstance(target_kanban_task_id, str) else None
        ),
    }


def dispatch_prep_prompt_block(record: rvf_prep_file.PrepFileRecord) -> str:
    return (
        "RVF dispatch prep file:\n"
        f"RVF_DISPATCH=token={record.token}\n"
        f"RVF_PREP_FILE: {record.path}\n"
        f"RVF_PREP_SCHEMA_VERSION: {rvf_prep_file.SCHEMA_VERSION}\n"
        "目标 session 的 UserPromptSubmit hook 只做 token 校验；"
        "agent 需要时可直接读取上面的 prep file。"
    )


def add_dispatch_prep_to_prompt(prompt: str, record: rvf_prep_file.PrepFileRecord) -> str:
    if "RVF_DISPATCH=token=" in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{dispatch_prep_prompt_block(record)}"


def dispatch_prep_tracker_scope_path(record: rvf_prep_file.PrepFileRecord) -> Path | None:
    rvf_run = record.payload.get("rvf_run")
    if not isinstance(rvf_run, dict):
        return None
    raw_path = rvf_run.get("tracker_scope_path")
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        return None
    return Path(raw_path).expanduser()


def write_dispatch_prep_file(
    *,
    ledger: RunLedger,
    origin_session_id: str | None,
    origin_repo: str | None,
    origin_cwd: str | None,
    target_flow: str,
    target_worktree: str | None,
    target_kanban_task_id: str | None = None,
    target_session_id: str | None = None,
    origin_metadata_path: str | None = None,
    parent_thread_path: Path | None = None,
) -> rvf_prep_file.PrepFileRecord:
    tracker_scope_meta = getattr(ledger, "tracker_scope_meta", None)
    tracker_scope_path = None
    tracker_lease_id = None
    tracker_scope_hash = None
    if isinstance(tracker_scope_meta, dict):
        tracker_scope_path = tracker_scope_meta.get("tracker_scope_path")
        tracker_lease_id = tracker_scope_meta.get("tracker_lease_id") or tracker_scope_meta.get("lease_id")
        tracker_scope_hash = tracker_scope_meta.get("tracker_scope_hash") or tracker_scope_meta.get("scope_hash")
    artifacts_dir = ledger.artifacts_dir
    payload: dict[str, Any] = {
        "origin_session_id": origin_session_id,
        "origin_repo": origin_repo,
        "origin_cwd": origin_cwd,
        "origin_branch": git_branch_name(origin_repo),
        "origin_transcript_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "origin_metadata_path": origin_metadata_path,
        "plugin_deploy": skill_deploy_metadata(SKILL_DIR),
        "target_flow": target_flow,
        "target_worktree": target_worktree,
        "target_kanban_task_id": target_kanban_task_id,
        "target_session_id": target_session_id,
        "rvf_run": {
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "artifacts_dir": str(artifacts_dir),
            "scope_contract_path": str(artifacts_dir / "inputs" / "scope.contract.json"),
            "tracker_scope_path": str(tracker_scope_path) if tracker_scope_path else None,
            "tracker_lease_id": tracker_lease_id,
            "tracker_scope_hash": tracker_scope_hash,
        },
        "handoff_expectations": {
            "handoff_path": str(artifacts_dir / "handoff.md"),
            "expected_artifacts": ["review-result.json", "merge-table.md", "handoff.md"],
        },
        "workflow_constraints": {
            "pause_origin_edits": target_flow == "flow-2-branch",
            "in_place_mode": target_flow != "flow-2-branch",
        },
    }
    swept_paths = rvf_prep_file.sweep_stale()
    if swept_paths:
        ledger.event(
            phase="prepare",
            event="dispatch_prep_file_sweep_completed",
            status="completed",
            reason_code="dispatch_prep_file_sweep_completed",
            repo=origin_repo,
            cwd=origin_cwd,
            paths={"prep_root": str(rvf_prep_file.prep_root())},
            removed_count=len(swept_paths),
            removed_paths=[str(path) for path in swept_paths],
        )
    record = rvf_prep_file.write_prep_file(payload)
    ledger.artifact(
        "dispatch-prep-file.json",
        {
            "token": record.token,
            "prep_file_path": str(record.path),
            "target_flow": target_flow,
            "payload": record.payload,
        },
    )
    ledger.event(
        phase="prepare",
        event="dispatch_prep_file_written",
        status="completed",
        reason_code="dispatch_prep_file_written",
        repo=origin_repo,
        cwd=origin_cwd,
        paths={"prep_file": str(record.path)},
        target_flow=target_flow,
        target_kanban_task_id=target_kanban_task_id,
    )
    return record


def update_dispatch_prep_file(
    *,
    ledger: RunLedger,
    record: rvf_prep_file.PrepFileRecord,
    target_flow: str,
    target_worktree: str | None = None,
    target_kanban_task_id: str | None = None,
    target_session_id: str | None = None,
) -> rvf_prep_file.PrepFileRecord:
    updates = {
        key: value
        for key, value in {
            "target_worktree": target_worktree,
            "target_kanban_task_id": target_kanban_task_id,
            "target_session_id": target_session_id,
        }.items()
        if value is not None
    }
    if not updates:
        return record
    # Reload latest payload from disk so freeze-side writes (notably
    # `rvf_run.shared_workflow_state` written by
    # freeze_cline_kanban_dispatch_artifacts) survive merging via this stale
    # caller-held record. update_prep_file() does a shallow merge over
    # `record.payload`, so without this reload we'd silently overwrite any
    # field the freeze step wrote after this caller obtained `record`.
    fresh = rvf_prep_file.read_prep_file(record.token)
    if fresh.status == "valid" and fresh.payload is not None:
        base_record = rvf_prep_file.PrepFileRecord(
            token=fresh.token,
            path=fresh.path,
            payload=dict(fresh.payload),
        )
    else:
        base_record = record
    updated = rvf_prep_file.update_prep_file(base_record, updates)
    ledger.artifact(
        "dispatch-prep-file.json",
        {
            "token": updated.token,
            "prep_file_path": str(updated.path),
            "target_flow": target_flow,
            "payload": updated.payload,
        },
    )
    ledger.event(
        phase="prepare",
        event="dispatch_prep_file_updated",
        status="completed",
        reason_code="dispatch_prep_file_updated",
        paths={"prep_file": str(updated.path)},
        target_flow=target_flow,
        target_worktree=target_worktree,
        target_kanban_task_id=target_kanban_task_id,
    )
    return updated


def cline_kanban_workspace_path(*payloads: dict[str, Any]) -> str | None:
    for payload in payloads:
        candidates = [payload]
        task = payload.get("task")
        if isinstance(task, dict):
            candidates.append(task)
        for candidate in candidates:
            for key in ("workspace_path", "workspacePath"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            workspace = candidate.get("workspace")
            if isinstance(workspace, dict):
                value = workspace.get("path")
                if isinstance(value, str) and value.strip():
                    return value
    return None


def kanban_followup_review_validate_fix_prompt(
    *,
    task_id: str,
    attempt_id: str | None,
    target_repo: str,
    cwd: str | None,
    ledger: RunLedger,
    source_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    attempt_line = f"RVF_CURRENT_ATTEMPT_ID: {attempt_id}\n" if attempt_id else ""
    cwd_line = cwd or "<unknown cwd>"
    origin_block = parent_origin_prompt_block(
        parent_origin=source_origin,
        origin_path=origin_path,
    )
    return (
        "$review-validate-fix\n\n"
        f"{KANBAN_FOLLOWUP_MARKER}\n"
        f"{plugin_deploy_prompt_block()}"
        f"RVF_RUN_ID: {ledger.run_id}\n"
        f"RVF_TARGET_REPO: {target_repo}\n"
        f"RVF_CURRENT_TASK_ID: {task_id}\n"
        f"{attempt_line}"
        f"RVF_CURRENT_CWD: {cwd_line}\n\n"
        f"{origin_block}\n\n"
        "上面的 RVF_PARENT_CONVERSATION_* 字段指本次 follow-up 的定位来源："
        "如果当前会话位于 Cline Kanban task 内，它们应优先使用 Kanban task "
        "title/name，方便开发者在 Kanban UI 中定位；否则使用源 Codex chat session "
        "name/ref。维护 handoff.md 时，`## Origin` 的 `original Codex conversation`、"
        "`conversation name source`、`original Codex URL`、`original transcript` "
        "和 `origin metadata` 必须保留这些值；若存在 `RVF_PARENT_KANBAN_*` 字段，"
        "还必须写 `source Kanban task id`、`source Kanban attempt id`、"
        "`source Kanban task title at trigger`，并让 `generated Kanban task` 写当前 "
        "task/attempt id。即使 task title 之后被开发者改名，后续 agent 也能用 task id "
        "查回当前名称。\n\n"
        "这是由 Cline Kanban host 在当前 task 的 coding agent chat session 中注入的"
        "真实用户消息，用于在同一 task/session 内触发 review-validate-fix。"
        "不要创建新的 Kanban task，不要 fork 新会话，也不要把这条消息当作 hook system context。\n\n"
        "请在当前 task worktree 中运行完整 review-validate-fix。目标仓库为上面的 "
        "`RVF_TARGET_REPO`；如果当前 task worktree 的 repo root 与该路径不同，以当前 task "
        "worktree 为执行位置，并在 handoff 中记录这一点。\n\n"
        "从准备阶段开始创建并持续维护 run artifact `handoff.md`。完成后最终回复"
        "第一行输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，"
        f"{HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION}，也不要在正文里重复 "
        "handoff 文件内容。Stop hook 会把 `RVF_HANDOFF_FILE` marker 作为完成信号，"
        "run 结束时发送 OS 系统通知（不再自动用编辑器打开 handoff）。"
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


def session_user_message_contains(event: dict[str, Any], marker: str) -> bool:
    return any(
        user_messages_containing(path.expanduser(), marker)
        for path in event_session_paths(event)
    )


def cline_kanban_script_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    if value and value.strip():
        return Path(value).expanduser()
    return default


def event_or_env_text(
    event: dict[str, Any],
    env_names: tuple[str, ...],
    event_keys: tuple[str, ...],
) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return string_event_value(event, event_keys)


def is_codex_agent_id(agent_id: str | None) -> bool:
    if agent_id is None:
        return False
    normalized = agent_id.strip().lower()
    return (
        normalized in {"codex", "codex-cli", "openai-codex"}
        or normalized.startswith("codex:")
        or "codex" in re.split(r"[^a-z0-9]+", normalized)
    )


# ⚠️ cline-kanban 的 `kanban task create --agent-id <id>` 决定该 task 用哪个 executor
# 跑（合法值：cline | claude | codex | droid | gemini | opencode | default）。
# RVF dispatch 只要传 `--agent-id`，就**覆盖了 cline-kanban 自身的默认 executor 选择**
# （cline-kanban 不传时会用它自己的 default profile）。
#
# 默认策略：镜像父（main agent）所用 harness。理由——同 executor fork
# （codex→codex / claude→claude）既能直接复用父会话上下文，又能命中 prompt cache；
# 跨 executor 时虽然仍可从对方 log/session 抽取通用 context，但 prompt cache 命中会
# 不可控地丢失。因此默认不再硬钉 "codex"，而是跟随父 harness。
# 仍保留 CODEX_RVF_CLINE_KANBAN_AGENT_ID 显式钉死某个 fix harness 的能力（优先级最高）。
def default_cline_kanban_agent_id(parent_thread_path: Path | None) -> str:
    """根据父会话 transcript 推断应镜像的 cline-kanban agent_id。

    复用 ``core.host_adapter.host_transcript_format_detection.detect_transcript_format`` 做 host 识别：
    Claude Code transcript → ``claude``；Codex rollout → ``codex``；
    无法识别（父 transcript 缺失 / 未知格式）→ 退回历史默认 ``codex``，
    保证既有 Codex-only 用例零回归。
    """
    if parent_thread_path is not None:
        try:
            host_kind = detect_transcript_format(parent_thread_path)
        except Exception:
            host_kind = None
        if host_kind == HOST_CLAUDE:
            return "claude"
        if host_kind == HOST_CODEX:
            return "codex"
    return "codex"


def resolve_cline_kanban_agent_id(
    parent_thread_path: Path | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """解析 cline-kanban task 的 agent_id。

    优先级：显式 ``CODEX_RVF_CLINE_KANBAN_AGENT_ID`` 钉死 > 镜像父 harness > ``codex`` 兜底。
    provider-health 门与实际 create 站点共用此函数，避免两处对 executor 的判断漂移。
    """
    environ = env if env is not None else os.environ
    pinned = (environ.get("CODEX_RVF_CLINE_KANBAN_AGENT_ID") or "").strip()
    if pinned:
        return pinned
    return default_cline_kanban_agent_id(parent_thread_path)


def provider_health_requirements(
    decision: StopDecision,
    event: dict[str, Any],
) -> list[ProviderHealthRequirement]:
    if decision.backend == "gui":
        return [
            ProviderHealthRequirement(
                provider="codex",
                reason="Legacy GUI/app-server RVF fallback uses Codex as the child session provider.",
                command=(codex_bin(), "login", "status"),
                remediation="请先运行 `codex login`，或使用 `codex login --with-api-key` 配置可用认证。",
            )
        ]

    if decision.backend == "kanban":
        # 与 start_cline_kanban_task 的 create 站点共用 resolver：默认镜像父 harness，
        # 父 transcript 用 host-agnostic 的 parent_thread_path_for_origin（也能识别 Claude）。
        agent_id = resolve_cline_kanban_agent_id(parent_thread_path_for_origin(event))
        if is_codex_agent_id(agent_id):
            return [
                ProviderHealthRequirement(
                    provider="codex",
                    reason=f"Cline Kanban RVF task will start agent_id={agent_id!r}.",
                    command=(codex_bin(), "login", "status"),
                    remediation=(
                        "请先运行 `codex login`，确认 `codex login status` 成功后再让 "
                        "Stop hook 创建 Cline Kanban RVF task。"
                    ),
                )
            ]

    if decision.backend == "kanban-followup":
        agent_id = (
            event_or_env_text(
                event,
                ("KANBAN_AGENT_ID", "CLINE_KANBAN_AGENT_ID"),
                ("kanban_agent_id", "kanbanAgentId", "agent_id", "agentId"),
            )
            or resolve_cline_kanban_agent_id(parent_thread_path_for_origin(event))
        )
        if is_codex_agent_id(agent_id):
            return [
                ProviderHealthRequirement(
                    provider="codex",
                    reason=f"Cline Kanban follow-up is targeting agent_id={agent_id!r}.",
                    command=(codex_bin(), "login", "status"),
                    remediation="请先运行 `codex login`，再重试 RVF follow-up 注入。",
                )
            ]

    return []


def command_output_text(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in (stdout or "", stderr or "") if part).strip()


def subprocess_output_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def codex_login_output_indicates_failure(output: str) -> bool:
    normalized = output.strip().lower()
    if not normalized:
        return False
    failure_markers = (
        "not logged in",
        "not authenticated",
        "logged out",
        "login expired",
        "session expired",
        "expired session",
        "authentication expired",
        "auth expired",
        "invalid credentials",
        "credential expired",
        "token expired",
    )
    return any(marker in normalized for marker in failure_markers)


def run_provider_health_requirement(
    requirement: ProviderHealthRequirement,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = list(requirement.command)
    record: dict[str, Any] = {
        "provider": requirement.provider,
        "reason": requirement.reason,
        "command": command,
        "remediation": requirement.remediation,
        "status": "failed",
    }
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "command_missing",
            }
        )
        return record
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "returncode": None,
                "stdout": subprocess_output_text(exc.stdout),
                "stderr": subprocess_output_text(exc.stderr),
                "failure_reason": "timeout",
                "timeout_seconds": timeout_seconds,
            }
        )
        return record
    except Exception as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "error",
            }
        )
        return record

    output = command_output_text(completed.stdout, completed.stderr)
    failed = completed.returncode != 0
    if requirement.provider == "codex" and codex_login_output_indicates_failure(output):
        failed = True
    record.update(
        {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "status": "failed" if failed else "ok",
            "failure_reason": "nonzero_or_auth_unhealthy" if failed else None,
        }
    )
    return record


def maybe_start_codex_login(ledger: RunLedger) -> dict[str, Any] | None:
    if not is_truthy(os.environ.get("CODEX_RVF_AUTO_CODEX_LOGIN")):
        return None

    log_path = ledger.artifacts_dir / "codex-login.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                [codex_bin(), "login"],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception as exc:
        return {
            "started": False,
            "error": f"{type(exc).__name__}: {exc}",
            "command": [codex_bin(), "login"],
            "log_path": str(log_path),
        }

    return {
        "started": True,
        "pid": process.pid,
        "command": [codex_bin(), "login"],
        "log_path": str(log_path),
    }


def provider_health_failure_message(
    failed: list[dict[str, Any]],
    login_attempt: dict[str, Any] | None,
) -> str:
    providers = ", ".join(sorted({str(item.get("provider")) for item in failed if item.get("provider")}))
    first = failed[0]
    remediation = str(first.get("remediation") or "请先修复 provider 认证状态后重试。")
    detail = command_output_text(
        str(first.get("stdout") or ""),
        str(first.get("stderr") or ""),
    )
    detail_line = f" health_output={detail[:240]!r}。" if detail else ""
    login_line = ""
    if login_attempt is not None:
        if login_attempt.get("started") is True:
            login_line = (
                " 已按 CODEX_RVF_AUTO_CODEX_LOGIN=1 尝试后台启动 `codex login`，"
                f"log={login_attempt.get('log_path')}。"
            )
        else:
            login_line = (
                " 已尝试后台启动 `codex login`，但启动失败："
                f"{login_attempt.get('error')}。"
            )
    return (
        "provider 登录/认证健康检查未通过，已阻止 RVF 自动启动，避免创建会立即失败的 "
        f"review 任务。providers={providers or '<unknown>'}。{remediation}"
        f"{detail_line}{login_line}"
    )


def provider_health_guard_decision(
    decision: StopDecision,
    event: dict[str, Any],
    ledger: RunLedger,
) -> StopDecision | None:
    if not provider_health_check_enabled():
        return None

    requirements = provider_health_requirements(decision, event)
    if not requirements:
        return None

    timeout_seconds = provider_health_timeout_seconds()
    results = [run_provider_health_requirement(requirement, timeout_seconds) for requirement in requirements]
    health_path = ledger.artifact(
        "provider-health.json",
        {
            "enabled": True,
            "backend": decision.backend,
            "timeout_seconds": timeout_seconds,
            "results": results,
        },
    )
    failed = [result for result in results if result.get("status") != "ok"]
    ledger.event(
        phase="provider-health",
        event="completed" if not failed else "failed",
        status="completed" if not failed else "failed",
        reason_code="provider_health_completed" if not failed else "provider_health_failed",
        repo=decision.repo,
        cwd=decision.cwd,
        backend=decision.backend,
        paths={"provider_health": health_path} if health_path else {},
        providers=[result.get("provider") for result in results],
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend=decision.backend,
            backend_raw=decision.backend,
        ),
    )
    if not failed:
        return None

    login_attempt = (
        maybe_start_codex_login(ledger)
        if any(result.get("provider") == "codex" for result in failed)
        else None
    )
    message = provider_health_failure_message(failed, login_attempt)
    return skip_decision(
        message,
        ledger,
        "provider_health_failed",
        repo=decision.repo,
        cwd=decision.cwd,
        backend=decision.backend,
        provider_health_path=health_path,
        provider_health=results,
        login_attempt=login_attempt,
        gate_status=(decision.summary_fields or {}).get("gate_status"),
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend=decision.backend,
            backend_raw=decision.backend,
        ),
    )


# Cline Kanban 在 task session 的 hook 环境中自动设置 KANBAN_TASK_ID 和
# KANBAN_WORKSPACE_ID；这是 kanban-followup 判断“当前 Stop hook 位于 Kanban task
# 内”的原生信号。早期 Kanban task 只设置 KANBAN_HOOK_TASK_ID，重启 runtime 后
# 这些旧 session 仍可能触发 Stop hook，因此保留 legacy hook env alias。ATTEMPT/
# PROJECT_PATH 不是公开文档确认的自动变量，这里只作为 host 定制字段或 Stop event
# 扩展字段兼容读取。
def current_kanban_task_id(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"),
        ("kanban_task_id", "kanbanTaskId", "task_id", "taskId"),
    )


def current_kanban_attempt_id(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        ("KANBAN_ATTEMPT_ID", "CLINE_KANBAN_ATTEMPT_ID"),
        ("kanban_attempt_id", "kanbanAttemptId", "attempt_id", "attemptId"),
    )


def current_kanban_task_title(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        (
            "KANBAN_TASK_TITLE",
            "KANBAN_TASK_NAME",
            "CLINE_KANBAN_TASK_TITLE",
            "CLINE_KANBAN_TASK_NAME",
        ),
        (
            "kanban_task_title",
            "kanbanTaskTitle",
            "kanban_task_name",
            "kanbanTaskName",
            "task_title",
            "taskTitle",
            "task_name",
            "taskName",
        ),
    )


def current_kanban_project_path(event: dict[str, Any], fallback: str) -> str:
    value = event_or_env_text(
        event,
        ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH"),
        ("kanban_project_path", "kanbanProjectPath", "project_path", "projectPath"),
    )
    return value or fallback


def kanban_followup_lock_session_id(event: dict[str, Any]) -> str | None:
    return session_hook_id_from_event(event) or session_id_from_event(event) or parent_thread_id_from_event(event)


def _kanban_followup_delivery_channel(message_id: Any) -> str:
    """从 Cline Kanban message_id 推断投递通道（用于诚实上报）。

    ``terminal:`` 前缀来自外部 ``kanban task message`` CLI 在**无 app-server socket**时的
    terminal fallback——它返回乐观的 ``status:started`` 回执，但消息未必成为真实 prompt turn
    （目标 session 处于 awaiting_review / 已停止时尤甚），故视为「未确认投递」。其余形态视为经
    app-server 的可确认投递。``terminal:`` 并非必然失败，只是 dispatch 这一刻尚不能确认落地；
    落地的权威信号是目标 session 的 UserPromptSubmit hook（arm in-progress 锁）。
    """
    if isinstance(message_id, str) and message_id.strip().lower().startswith("terminal:"):
        return "terminal"
    return "app-server"


def clear_kanban_followup_lock_for_event(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | None,
    handoff_path: str | None = None,
) -> list[str]:
    task_id = current_kanban_task_id(event)
    session_id = kanban_followup_lock_session_id(event)
    removed = clear_kanban_followup_lock(task_id=task_id, session_id=session_id)
    # handoff 完成顺带清掉残留 pending（对称卫生）：正常路径投递落地时 UPS arm 已清，
    # 这里兜底处理「同 token pending 因故未被清」的尾巴。
    removed_pending = clear_kanban_followup_pending(task_id=task_id)
    if removed or removed_pending:
        ledger.event(
            phase="complete",
            event="kanban_followup_in_progress_cleared",
            status="completed",
            reason_code="kanban_followup_handoff_complete",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            handoff_path=handoff_path,
            removed_kanban_followup_in_progress_marker_paths=removed,
            removed_kanban_followup_pending_marker_paths=removed_pending,
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                handoff_path=handoff_path,
                completion_gate="handoff_file_ready",
            ),
        )
    return removed


def _kanban_followup_pending_decision(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    task_id: str | None,
    session_id: str | None,
    cwd: str | None,
) -> StopDecision | None:
    """无 in-progress 锁时，按 dispatched-unconfirmed(pending) marker 做对账。

    - pending 仍 active（在途窗口内）：上一条 follow-up 刚 dispatch、尚未确认落地，本次 Stop
      暂不重复 dispatch 以免双注入；窗口由 pending TTL 限定（默认 15min），落地时 UPS arm 会
      按 token 清掉它。
    - pending 已 stale（在途窗口已过仍未确认）：上一条 follow-up 静默丢投——上报
      ``kanban_followup_prior_dispatch_unconfirmed``、清 pending，并放行（返回 None）让正常流程重投。
    - 无 pending：返回 None，照常 dispatch。
    """
    pending = read_kanban_followup_pending(task_id=task_id)
    if pending is None:
        return None
    status = kanban_followup_pending_status(pending)
    pending_path = pending.get("_marker_path")
    if status == KANBAN_FOLLOWUP_LOCK_ACTIVE:
        return skip_decision(
            "上一条 Cline Kanban RVF follow-up 刚 dispatch、尚未确认落地"
            f"（token={pending.get('token') or '<unknown>'}，"
            f"channel={pending.get('delivery_channel') or '<unknown>'}，"
            f"dispatched_at={pending.get('dispatched_at') or '<unknown>'}）；"
            "本次 Stop 暂不重复 dispatch，待其落地或在途窗口超时后再处理。",
            ledger,
            "kanban_followup_dispatch_in_flight",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_pending_marker=pending,
            kanban_followup_pending_marker_path=pending_path,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_dispatch_in_flight",
            ),
        )
    removed = clear_kanban_followup_pending(task_id=task_id)
    ledger.event(
        phase="gate",
        event="kanban_followup_prior_dispatch_unconfirmed",
        status="completed",
        reason_code="kanban_followup_prior_dispatch_unconfirmed",
        cwd=cwd,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_pending_marker=pending,
        removed_kanban_followup_pending_marker_paths=removed,
    )
    return None


# ---------------------------------------------------------------------------
# S1b：跨 task stranded-pending 扫荡 + 升级（治本主体）
#
# flow-1-self-rising 的死结：被卡住的 task 自己不会再 Stop，而唯一的同 task 对账
# （``_kanban_followup_pending_decision``）只在该 task 下次 Stop 触发——于是
# dispatched-unconfirmed 的 review 会静默永久 parked。本扫荡让**任意会话、任意 repo 的 Stop**
# 都能发现并持续把别的 task 遗留的 stale pending 浮现给用户，直至目标 session 的
# UserPromptSubmit hook 在真实投递落地时按 token 清掉 marker。「stale marker 仍在」即 stranded
# 的权威信号（marker 的清除只发生在：真实投递 UPS arm 清 / 同 task stale 自愈清 / handoff 清锁）。
# ---------------------------------------------------------------------------

DEFAULT_KANBAN_FOLLOWUP_STRANDED_RENOTIFY_SECONDS = 3600
DEFAULT_KANBAN_FOLLOWUP_STRANDED_SWEEP_MAX = 8
KANBAN_FOLLOWUP_STRANDED_RENOTIFY_ENV = "RVF_KANBAN_FOLLOWUP_STRANDED_RENOTIFY_SECONDS"
KANBAN_FOLLOWUP_STRANDED_SWEEP_MAX_ENV = "RVF_KANBAN_FOLLOWUP_STRANDED_SWEEP_MAX"
KANBAN_FOLLOWUP_AUTO_REDISPATCH_ENV = "RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"
KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_ENV = "RVF_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS"
DEFAULT_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS = 20
# verify-consumed 精修读 transcript 的体积上限（超过则跳过精修、按 stranded 处理），
# 防在 30s Codex 链路预算内读超大 JSONL transcript 卡顿。
KANBAN_FOLLOWUP_CONSUMED_SCAN_MAX_BYTES = 32 * 1024 * 1024


def _kanban_followup_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(default)


def _kanban_followup_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return int(default)


def _parse_marker_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _strip_dispatch_prep_block(prompt: str) -> str:
    """剥掉 prompt 末尾由 ``add_dispatch_prep_to_prompt`` 追加的 dispatch-prep 注入块。

    注入块以 ``"RVF dispatch prep file:"`` 起头、追加在 prompt 末尾，故从该标记处截断即可
    干净移除；S2 重投据此用新 token 重新注入。无注入块时原样返回（已 rstrip）。
    """
    marker = "RVF dispatch prep file:"
    idx = prompt.find(marker)
    if idx == -1:
        return prompt.rstrip()
    return prompt[:idx].rstrip()


def _kanban_followup_marker_consumed(marker: dict[str, Any]) -> bool:
    """（可选 best-effort 精修）据 ``origin_transcript_path`` 判 stranded review 是否已迟到消费。

    命中 ``RVF_DISPATCH=token={token}`` = 注入的 follow-up 已落进派发方 transcript（同会话
    消费）→ 视为 consumed。解析不出 / 文件缺失 / 过大 → 返回 False（**不据此判 consumed**，
    仍按 stranded 升级）。这是对「UPS arm 异常没清掉」假 stranded 的兜底；权威信号仍是
    marker 是否仍在，故本精修永远只会「多清」不会「漏报 stranded」。
    """
    token = marker.get("token")
    if not (isinstance(token, str) and token.strip()):
        return False
    transcript_raw = marker.get("origin_transcript_path")
    if not (isinstance(transcript_raw, str) and transcript_raw.strip()):
        return False
    try:
        path = Path(transcript_raw).expanduser()
        if not path.is_file():
            return False
        if path.stat().st_size > KANBAN_FOLLOWUP_CONSUMED_SCAN_MAX_BYTES:
            return False
        needle = f"RVF_DISPATCH=token={token}"
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _maybe_redispatch_stranded_kanban_followup(
    marker: dict[str, Any],
    ledger: RunLedger,
    *,
    token: str | None,
) -> dict[str, Any]:
    """S2（可选，``RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH=1`` 开启）：app-server 此刻可达时
    对 stranded marker 机会式重投一次。

    诚实边界：即便 app-server 活着，已停 session 也未必消费 → best-effort，绝不谎报已跑。
    契约（按审查修正）：铸**新** dispatch-prep + 新 token（旧 token 的 prep 可能已被
    ``rvf_prep_file.sweep_stale`` 清，复用会让目标 UPS 判 ``dispatch_no_prep`` 不清 pending）；
    用**稳定** idempotency key ``rvf-redispatch-<task>-<token>``（不可用每 Stop 变的 run_id，
    否则 Kanban 侧不幂等）；子进程带 timeout；重投后据投递通道改写/清除 pending，避免与该
    task 自己的下次 Stop 撞成双投。
    """
    if not is_truthy(os.environ.get(KANBAN_FOLLOWUP_AUTO_REDISPATCH_ENV)):
        return {"redispatched": False, "reason": "disabled"}
    task_id = marker.get("kanban_task_id")
    project_path = marker.get("kanban_project_path")
    prompt_path = marker.get("prompt_path")
    if not (isinstance(task_id, str) and task_id.strip()):
        return {"redispatched": False, "reason": "missing-task-id"}
    if not (isinstance(project_path, str) and project_path.strip()):
        return {"redispatched": False, "reason": "missing-project-path"}
    if not (isinstance(prompt_path, str) and prompt_path.strip()):
        return {"redispatched": False, "reason": "missing-prompt-path"}
    # app-server 可达性闸门：不可达即放弃（marker 仍在 + 已发通知，留待 human-in-the-loop）。
    try:
        socket_path, _socket_source, _socket_meta = select_existing_app_server_socket_for_metadata()
    except Exception:
        return {"redispatched": False, "reason": "app-server-unreachable"}
    if not can_connect_app_server_socket(socket_path):
        return {"redispatched": False, "reason": "app-server-unreachable"}
    try:
        old_prompt = Path(prompt_path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return {"redispatched": False, "reason": "prompt-unreadable"}
    base_prompt = _strip_dispatch_prep_block(old_prompt)
    run_dir = marker.get("run_dir")
    origin_metadata_path = (
        str(Path(run_dir).expanduser() / "artifacts" / "origin.json")
        if isinstance(run_dir, str) and run_dir.strip()
        else None
    )
    try:
        fresh_prep = write_dispatch_prep_file(
            ledger=ledger,
            origin_session_id=marker.get("session_id"),
            origin_repo=marker.get("repo"),
            origin_cwd=marker.get("cwd"),
            target_flow="flow-1-self-rising",
            target_worktree=marker.get("cwd"),
            target_kanban_task_id=task_id,
            origin_metadata_path=origin_metadata_path,
        )
    except Exception as exc:
        return {"redispatched": False, "reason": "prep-failed", "error": f"{type(exc).__name__}: {exc}"}
    new_prompt = add_dispatch_prep_to_prompt(base_prompt, fresh_prep)
    # 幂等键必须绑定**稳定**的 stranded token（marker 当前 token），不能用每次扫荡新铸的
    # fresh_prep.token——否则同一 stranded marker 在 renotify 窗口内被多个会话的 Stop 并发扫荡时
    # 各生成不同 key，Kanban 无法去重 → 重复派发。token 缺失（退化 marker）才回退占位。
    idem = f"rvf-redispatch-{safe_token(task_id)}-{token or 'no-token'}"
    redispatch_timeout = _kanban_followup_env_float(
        KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS,
    )
    try:
        message_payload = start_cline_kanban_followup_message(
            project_path=project_path,
            task_id=task_id,
            attempt_id=marker.get("kanban_attempt_id"),
            prompt=new_prompt,
            ledger=ledger,
            idempotency_key=idem,
            timeout=redispatch_timeout if redispatch_timeout > 0 else None,
        )
    except subprocess.TimeoutExpired:
        return {"redispatched": False, "reason": "redispatch-timeout", "token": fresh_prep.token}
    except Exception as exc:
        return {"redispatched": False, "reason": "redispatch-failed", "error": f"{type(exc).__name__}: {exc}"}
    new_message_id = message_payload.get("message_id")
    new_channel = _kanban_followup_delivery_channel(new_message_id)
    if new_channel == "app-server":
        # 这次直接确认到 app-server → 清掉 pending（用旧 token 过 clear 的防误清 guard）。
        clear_kanban_followup_pending(task_id=task_id, token=token)
    else:
        # 仍未确认 → 把 pending 改写到新 token、重置在途窗口，落地时 UPS arm 按新 token 清。
        # 必须带上**本次重投新派发**的 prompt_path/turn_id：否则改写后的 marker 丢失 prompt_path，
        # 下一次扫荡的 S2 重投会因 missing-prompt-path 永久无法再重投。last_notified_at 有意不保留——
        # 改写后 marker 变 active（新 expires_at），sweep 会跳过它直到再次 stale；届时重新通知才正确。
        try:
            write_kanban_followup_pending(
                task_id=task_id,
                session_id=marker.get("session_id"),
                run_id=str(ledger.run_id),
                run_dir=str(ledger.run_dir),
                repo=marker.get("repo"),
                cwd=marker.get("cwd"),
                token=fresh_prep.token,
                delivery_channel=new_channel,
                attempt_id=marker.get("kanban_attempt_id"),
                message_id=new_message_id if isinstance(new_message_id, str) else None,
                turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
                prompt_path=message_payload.get("prompt_path"),
                kanban_project_path=project_path,
                kanban_task_title=marker.get("kanban_task_title"),
                kanban_task_title_source=marker.get("kanban_task_title_source"),
                origin_transcript_path=marker.get("origin_transcript_path"),
            )
        except Exception:
            pass
    return {
        "redispatched": True,
        "reason": "redispatched",
        "delivery_channel": new_channel,
        "token": fresh_prep.token,
        "idempotency_key": idem,
    }


def sweep_stranded_kanban_followup_pending(
    event: dict[str, Any],
    ledger: RunLedger,
) -> None:
    """跨 task 扫荡 stale dispatched-unconfirmed pending 并升级（S1b）。整函数 try/except 永不抛。

    任意会话、任意 repo 的 Stop 入口调用：打破 flow-1-self-rising 的自升循环。受 30s Codex 链路
    预算约束——单次最多处理 ``SWEEP_MAX`` 条、通知 timeout=10、可选精修读 transcript 有体积上限。
    """
    try:
        current_task_id = current_kanban_task_id(event)
        renotify_seconds = _kanban_followup_env_float(
            KANBAN_FOLLOWUP_STRANDED_RENOTIFY_ENV,
            DEFAULT_KANBAN_FOLLOWUP_STRANDED_RENOTIFY_SECONDS,
        )
        sweep_max = _kanban_followup_env_int(
            KANBAN_FOLLOWUP_STRANDED_SWEEP_MAX_ENV,
            DEFAULT_KANBAN_FOLLOWUP_STRANDED_SWEEP_MAX,
        )
        if sweep_max <= 0:
            return
        markers = iter_kanban_followup_pending()
        now_ts = datetime.now(timezone.utc).timestamp()
        processed = 0
        for marker in markers:
            if processed >= sweep_max:
                break
            if not isinstance(marker, dict):
                continue
            marker_task_id = marker.get("kanban_task_id")
            if not (isinstance(marker_task_id, str) and marker_task_id.strip()):
                continue
            # 当前 Stop 的 task 交既有同 task 对账（_kanban_followup_pending_decision）清+重投，
            # 避免两条路径语义打架。
            if current_task_id and marker_task_id == current_task_id:
                continue
            if kanban_followup_pending_status(marker, now_ts=now_ts) == KANBAN_FOLLOWUP_LOCK_ACTIVE:
                continue
            token = marker.get("token") if isinstance(marker.get("token"), str) else None
            # 可选 best-effort 精修：迟到消费 → 清 marker、不通知。
            if _kanban_followup_marker_consumed(marker):
                removed = clear_kanban_followup_pending(task_id=marker_task_id, token=token)
                ledger.event(
                    phase="gate",
                    event="kanban_followup_pending_reconciled_consumed",
                    status="completed",
                    reason_code="kanban_followup_pending_reconciled_consumed",
                    cline_kanban_task_id=marker_task_id,
                    kanban_followup_pending_marker_path=marker.get("_marker_path"),
                    removed_kanban_followup_pending_marker_paths=removed,
                )
                processed += 1
                continue
            # 防刷屏：距上次通知不足 renotify_seconds 则跳过通知（保留 marker，持续浮现）。
            last_notified_ts = _parse_marker_epoch(marker.get("last_notified_at"))
            if last_notified_ts is not None and (now_ts - last_notified_ts) < renotify_seconds:
                continue
            task_url = resolve_kanban_task_url(marker.get("kanban_project_path"), marker_task_id)
            notify_result = notify_kanban_followup_stranded(
                task_id=marker_task_id,
                task_title=marker.get("kanban_task_title"),
                task_url=task_url,
                reason="stranded-escalated",
            )
            stamp_kanban_followup_pending_notified(task_id=marker_task_id, token=token)
            redispatch = _maybe_redispatch_stranded_kanban_followup(marker, ledger, token=token)
            ledger.event(
                phase="gate",
                event="kanban_followup_pending_stranded_escalated",
                status="completed",
                reason_code="kanban_followup_pending_stranded_escalated",
                cline_kanban_task_id=marker_task_id,
                cline_kanban_task_title=marker.get("kanban_task_title"),
                kanban_followup_pending_marker_path=marker.get("_marker_path"),
                kanban_followup_task_url=task_url,
                kanban_followup_stranded_notified=bool(notify_result.get("notified")),
                kanban_followup_stranded_notify_reason=notify_result.get("reason"),
                kanban_followup_stranded_dispatched_at=marker.get("dispatched_at"),
                kanban_followup_stranded_redispatch=redispatch,
            )
            processed += 1
    except Exception:
        # 对齐既有 best-effort：扫荡的任何异常都不得影响本次 Stop 的主流程。
        pass


def kanban_followup_in_progress_decision(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | None,
) -> StopDecision | None:
    task_id = current_kanban_task_id(event)
    session_id = kanban_followup_lock_session_id(event)
    marker = read_kanban_followup_lock(task_id=task_id, session_id=session_id)
    if marker is None:
        # 无 in-progress 锁（从未落地或已 handoff 清掉）→ 转交 pending 对账：判断上一条
        # dispatch 是否在途（去重）或已静默丢投（重投）。
        return _kanban_followup_pending_decision(
            event, ledger, task_id=task_id, session_id=session_id, cwd=cwd
        )

    status = kanban_followup_lock_status(marker)
    marker_path = marker.get("_marker_path")
    if status == KANBAN_FOLLOWUP_LOCK_ACTIVE:
        blocking_run_id = marker.get("run_id")
        # 锁卫生：若上游 agent 经 $rvf-reopen 明确武装了 rescope marker（失败再入），
        # 当前这把仍 active 的 followup 锁属于被放弃的那一轮 cycle（其 run 与 rescope
        # 的 target_run_id 不同），应 reconcile 掉、让下游 reopen + 全量重审继续，
        # 而不是被它 6h 空转阻塞（治原 R1.5 式 squat）。仅在确有 active rescope
        # marker 且锁非 rescope 目标 run 时清理，绝不误清正在跑的 followup。
        reopen_marker = read_review_reopen_marker(
            task_id=task_id,
            session_id=session_hook_id_from_event(event),
        )
        reopen_target_run_id = (
            reopen_marker.get("target_run_id") if isinstance(reopen_marker, dict) else None
        )
        if (
            reopen_marker is not None
            and review_reopen_status(reopen_marker) == REVIEW_REOPEN_ACTIVE
            and blocking_run_id != reopen_target_run_id
        ):
            removed_lock = clear_kanban_followup_lock(task_id=task_id, session_id=session_id)
            ledger.event(
                phase="gate",
                event="kanban_followup_lock_reconciled_for_reopen",
                status="completed",
                reason_code="kanban_followup_lock_reconciled_for_failed_impl_reentry",
                cwd=cwd,
                cline_kanban_task_id=task_id,
                session_id=session_id,
                reconciled_blocking_run_id=blocking_run_id,
                reopen_target_run_id=reopen_target_run_id,
                kanban_followup_in_progress_marker=marker,
                removed_kanban_followup_in_progress_marker_paths=removed_lock,
            )
            return None
        return skip_decision(
            "已有 Cline Kanban RVF follow-up 仍在进行"
            f"（阻塞 run_id={blocking_run_id or '<unknown>'}，"
            f"armed_at={marker.get('armed_at') or '<unknown>'}，"
            f"run_dir={marker.get('run_dir') or '<unknown>'}）；"
            "本次 Stop hook 跳过自动 RVF dispatch，避免在上一轮 handoff 完成前创建新 RVF。"
            "若该 followup 实际已被放弃，可经 $rvf-reopen 武装 rescope state 以 reconcile 掉它。",
            ledger,
            "kanban_followup_in_progress",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker=marker,
            kanban_followup_in_progress_marker_path=marker_path,
            active_rvf_run_id=blocking_run_id,
            active_rvf_run_dir=marker.get("run_dir"),
            active_rvf_armed_at=marker.get("armed_at"),
            active_rvf_turn_id=marker.get("turn_id"),
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_in_progress",
            ),
        )

    removed = clear_kanban_followup_lock(task_id=task_id, session_id=session_id)
    ledger.event(
        phase="gate",
        event="kanban_followup_in_progress_marker_consumed_incomplete",
        status="completed",
        reason_code=f"kanban_followup_in_progress_{status}",
        cwd=cwd,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_in_progress_marker=marker,
        consumed_kanban_followup_in_progress_marker_paths=removed,
    )
    return None


def consume_review_reopen_marker(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
    *,
    cwd: str | None,
) -> dict[str, Any] | None:
    """失败再入：在 dirty-route 即将 dispatch / allocate 前消费 rescope marker。

    若 marker active → 按 ``target_run_id`` 把「最近一次刚经过 RVF 的实现 run」
    仍存在的 ``reviewed`` units 翻回 ``available``（run-scoped，**绝不广播**到其它 run
    或全 worktree），使紧接着的 allocate（本进程或 kanban-followup 后台 run 的 refresh+
    allocate）自然得到「该实现 units ∪ 本次 fix delta」全量重审；随后 consume marker。
    ``stale`` / ``invalid`` 也消费但不重开。best-effort：tracker 异常不阻断 Stop 主流程。
    """
    task_id = current_kanban_task_id(event)
    session_id = session_hook_id_from_event(event)
    marker = read_review_reopen_marker(task_id=task_id, session_id=session_id)
    if marker is None:
        return None

    status = review_reopen_status(marker)
    target_run_id = marker.get("target_run_id")

    if status != REVIEW_REOPEN_ACTIVE:
        removed = clear_review_reopen_marker(task_id=task_id, session_id=session_id)
        ledger.event(
            phase="gate",
            event="review_reopen_marker_discarded",
            status="completed",
            reason_code=f"review_reopen_{status}",
            cwd=cwd,
            repo=repo,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            target_run_id=target_run_id,
            review_reopen_marker=marker,
            consumed_review_reopen_marker_paths=removed,
        )
        return None

    reopen_result: dict[str, Any] | None = None
    reopen_error: dict[str, str] | None = None
    if target_run_id:
        try:
            reopen_result = invalidate_reviewed_units_for_run(
                repo=repo,
                run_id=target_run_id,
                reason="failed_impl_reentry",
            )
        except Exception as exc:  # best-effort：绝不因 tracker 异常打断 Stop 主流程
            reopen_error = {"kind": type(exc).__name__, "message": str(exc)}

    removed = clear_review_reopen_marker(task_id=task_id, session_id=session_id)
    ledger.event(
        phase="gate",
        event="review_scope_reopened_for_failed_impl",
        status="completed" if reopen_error is None else "warning",
        reason_code="failed_impl_reentry",
        level="warn" if reopen_error is not None else "info",
        cwd=cwd,
        repo=repo,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        target_run_id=target_run_id,
        run_id_source=marker.get("source"),
        reopened_unit_count=(reopen_result or {}).get("reopened_unit_count"),
        reopened_unit_ids=(reopen_result or {}).get("reopened_unit_ids"),
        candidate_unit_count=(reopen_result or {}).get("candidate_unit_count"),
        review_reopen_marker=marker,
        consumed_review_reopen_marker_paths=removed,
        error=reopen_error,
    )
    return reopen_result


def current_kanban_workspace_path(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        (
            "KANBAN_WORKSPACE_PATH",
            "CLINE_KANBAN_WORKSPACE_PATH",
            "KANBAN_TASK_WORKSPACE_PATH",
            "CLINE_KANBAN_TASK_WORKSPACE_PATH",
            "KANBAN_WORKTREE_PATH",
            "CLINE_KANBAN_WORKTREE_PATH",
        ),
        (
            "kanban_workspace_path",
            "kanbanWorkspacePath",
            "workspace_path",
            "workspacePath",
            "task_workspace_path",
            "taskWorkspacePath",
            "worktree_path",
            "worktreePath",
        ),
    )


def git_toplevel_or_none(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).expanduser().resolve() if value else None


def _task_workspace_from_mapping(value: dict[str, Any]) -> str | None:
    for key in ("workspace_path", "workspacePath"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    workspace = value.get("workspace")
    if isinstance(workspace, dict):
        candidate = workspace.get("path")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def kanban_task_workspace_from_payload(payload: dict[str, Any], task_id: str) -> str | None:
    expected = task_id.strip()
    if not expected:
        return None
    for candidate in _iter_nested_dicts(payload):
        if _task_id_from_mapping(candidate) != expected:
            continue
        workspace = _task_workspace_from_mapping(candidate)
        if workspace:
            return workspace
    return None


def lookup_kanban_task_workspace_from_state(
    *,
    task_id: str,
    project_path: str | None,
) -> dict[str, Any]:
    workspaces_dir = cline_kanban_state_dir() / "workspaces"
    checked: list[str] = []
    matches: list[dict[str, str]] = []
    errors: list[str] = []
    if not workspaces_dir.is_dir():
        return {"workspace_path": None, "source": "kanban_state_missing", "checked": checked}
    for sessions_path in sorted(workspaces_dir.glob("*/sessions.json")):
        checked.append(str(sessions_path))
        try:
            payload = json.loads(sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{sessions_path}: {type(exc).__name__}: {exc}")
            continue
        sessions = payload.values() if isinstance(payload, dict) else []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_task_id = session.get("taskId") or session.get("task_id") or session.get("id")
            if session_task_id != task_id:
                continue
            workspace = _task_workspace_from_mapping(session)
            if workspace:
                matches.append({"workspace_path": workspace, "sessions_path": str(sessions_path)})

    unique = []
    seen: set[str] = set()
    for match in matches:
        resolved = str(Path(match["workspace_path"]).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append({**match, "workspace_path": resolved})
    if len(unique) == 1:
        return {
            "workspace_path": unique[0]["workspace_path"],
            "source": "kanban_state_session_workspace",
            "checked": checked,
            "matches": unique,
            "errors": errors,
        }
    return {
        "workspace_path": None,
        "source": "kanban_state_ambiguous" if unique else "kanban_state_missing_task_workspace",
        "checked": checked,
        "matches": unique,
        "errors": errors,
        "project_path": project_path,
    }


def lookup_cline_kanban_task_workspace(
    *,
    project_path: str,
    task_id: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    direct = current_kanban_workspace_path({})
    if direct:
        return {"workspace_path": direct, "source": "kanban_workspace_env"}
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    command = [
        sys.executable,
        str(client),
        "list",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    artifact = ledger.artifact(
        "kanban-followup-workspace-lookup.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode == 0:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            return {
                "workspace_path": None,
                "source": "cline_kanban_workspace_lookup_invalid_json",
                "artifact": artifact,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if isinstance(payload, dict):
            workspace = kanban_task_workspace_from_payload(payload, task_id)
            if workspace:
                return {
                    "workspace_path": workspace,
                    "source": "cline_kanban_task_list_workspace",
                    "artifact": artifact,
                }
    state_lookup = lookup_kanban_task_workspace_from_state(task_id=task_id, project_path=project_path)
    state_lookup["task_list_lookup"] = {
        "artifact": artifact,
        "returncode": completed.returncode,
        "error": completed.stderr.strip() or completed.stdout.strip(),
    }
    return state_lookup


def kanban_task_workspace_guard_payload(
    *,
    event: dict[str, Any],
    cwd: str | None,
    ledger: RunLedger,
) -> dict[str, Any] | None:
    task_id = current_kanban_task_id(event)
    if not (task_id and cwd):
        return None
    cwd_root = git_toplevel_or_none(Path(cwd).expanduser())
    if cwd_root is None:
        return None
    direct_workspace = current_kanban_workspace_path(event)
    lookup: dict[str, Any]
    if direct_workspace:
        lookup = {"workspace_path": direct_workspace, "source": "kanban_workspace_event"}
    else:
        project_path = current_kanban_project_path(event, str(cwd_root))
        lookup = lookup_cline_kanban_task_workspace(
            project_path=project_path,
            task_id=task_id,
            ledger=ledger,
        )
    raw_workspace = lookup.get("workspace_path")
    if not isinstance(raw_workspace, str) or not raw_workspace.strip():
        ledger.event(
            phase="gate",
            event="kanban_task_workspace_guard_unresolved",
            status="warning",
            reason_code="kanban_task_workspace_unresolved",
            cwd=cwd,
            kanban_task_id=task_id,
            cwd_git_root=str(cwd_root),
            workspace_lookup=lookup,
        )
        return None
    workspace_root = git_toplevel_or_none(Path(raw_workspace).expanduser()) or Path(raw_workspace).expanduser().resolve()
    if _same_existing_path(cwd_root, workspace_root):
        ledger.event(
            phase="gate",
            event="kanban_task_workspace_guard_matched",
            status="completed",
            reason_code="kanban_task_workspace_matched",
            cwd=cwd,
            kanban_task_id=task_id,
            cwd_git_root=str(cwd_root),
            kanban_task_workspace=str(workspace_root),
            workspace_lookup=lookup,
        )
        return None
    return skip_payload(
        "当前 Stop event 位于 Cline Kanban task，但 cwd/git root 与该 task 的执行 worktree 不一致；"
        "已跳过自动 RVF，避免审查 unrelated dirty repo。",
        ledger,
        "kanban_task_workspace_mismatch",
        repo=str(cwd_root),
        cwd=cwd,
        backend="kanban-followup",
        kanban_task_id=task_id,
        cwd_git_root=str(cwd_root),
        kanban_task_workspace=str(workspace_root),
        workspace_lookup=lookup,
        **stop_hook_rvf_state_fields(
            phase="complete",
            backend="kanban-followup",
            backend_raw="kanban-followup",
            completion_gate="kanban_task_workspace_mismatch",
        ),
    )


def freeze_cline_kanban_dispatch_artifacts(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    ledger: RunLedger,
    dispatch_prep: rvf_prep_file.PrepFileRecord,
    target_flow: str,
) -> dict[str, Any]:
    scope_text = dispatch_scope_of_work_text(
        target_flow=target_flow,
        cwd=cwd,
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        prompt_path=prompt_path,
        run_id=ledger.run_id,
        run_dir=ledger.run_dir,
    )
    scope_path = ledger.artifact("startup-scope-of-work.md", scope_text)
    if not scope_path:
        raise RuntimeError("failed to write Cline Kanban startup scope artifact")
    # 父会话对话 context（fail-open；缺失/关闭不阻塞 dispatch）。child 通过
    # task prompt 的 RVF_PARENT_CONVERSATION_CONTEXT 标记从 run artifacts 读取。
    freeze_parent_conversation_context(
        parent_thread_path=parent_thread_path,
        ledger=ledger,
        cwd=cwd,
    )
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
        "--rvf-backend",
        "kanban-task",
    ]
    if parent_thread_path is not None:
        command.extend(["--transcript", str(parent_thread_path)])
    tracker_scope_path = dispatch_prep_tracker_scope_path(dispatch_prep)
    if tracker_scope_path is not None:
        if not tracker_scope_path.exists():
            raise RuntimeError(f"dispatch prep tracker scope artifact missing: {tracker_scope_path}")
        command.extend(["--tracker-scope", str(tracker_scope_path)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "cline-kanban-dispatch-prepare-command.json",
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
            or "failed to freeze Cline Kanban dispatch review artifacts"
        )
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Cline Kanban dispatch prepare JSON: {completed.stdout!r}") from exc
    metadata_path = ledger.artifact("cline-kanban-dispatch-prepare.json", metadata)
    ledger.event(
        phase="prepare",
        event="cline_kanban_dispatch_prepared",
        status="completed",
        reason_code="cline_kanban_dispatch_prepared",
        repo=cwd,
        cwd=cwd,
        paths={
            "metadata": metadata_path,
            "scope_of_work": metadata.get("scope_of_work_file"),
            "session_manifest": metadata.get("session_manifest_file"),
            "review_packet": metadata.get("review_packet"),
            "snapshot": metadata.get("before_workspace_snapshot"),
            "worktree_bootstrap": metadata.get("worktree_bootstrap"),
            "review_env": metadata.get("review_env_file"),
            "review_agent_context": metadata.get("review_agent_context_file"),
            "tracker_scope": str(tracker_scope_path) if tracker_scope_path is not None else None,
            "dispatch_prep_file": str(dispatch_prep.path),
        },
        target_flow=target_flow,
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            prepare_metadata=metadata,
        ),
    )
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    completed_state = {
        "started_at": now_iso,
        "completed_at": now_iso,
        "status": "completed",
        "target_flow": target_flow,
        "target_repo": cwd,
        "rvf_backend": "kanban-task",
        "run_id": metadata.get("run_id"),
        "run_dir": metadata.get("run_dir"),
        "artifacts": {
            "scope_contract": metadata.get("scope_contract"),
            "review_packet": metadata.get("review_packet"),
            "review_packet_metadata": metadata.get("review_packet_metadata"),
            "review_env": metadata.get("review_env_file"),
            "review_agent_context": metadata.get("review_agent_context_file"),
            "worktree_bootstrap": metadata.get("worktree_bootstrap"),
            "session_manifest": metadata.get("session_manifest_file"),
            "scope_of_work": metadata.get("scope_of_work_file"),
        },
    }
    new_rvf_run = dict(dispatch_prep.payload.get("rvf_run") or {})
    new_rvf_run["shared_workflow_state"] = completed_state
    updated_record = rvf_prep_file.update_prep_file(dispatch_prep, {"rvf_run": new_rvf_run})
    return {
        "metadata_path": metadata_path,
        "metadata": metadata,
        "scope_of_work_path": scope_path,
        "dispatch_prep_record": updated_record,
    }


def git_head(cwd: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "failed to resolve git HEAD")
    return completed.stdout.strip()


def parse_json_command_output(completed: subprocess.CompletedProcess[str], *, label: str) -> dict[str, Any]:
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{label} failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {label} JSON: {completed.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid {label} payload: {payload!r}")
    return payload


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def cline_kanban_task_prompt(
    *,
    cwd: str,
    prompt_path: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    ledger: RunLedger,
    dispatch_prep: rvf_prep_file.PrepFileRecord,
    worktree_mode: str,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    host_label = parent_conversation_host_label(parent_origin.get("host_kind"))
    parent_conversation_ref = str(
        parent_origin.get("label") or f"<unknown {host_label} conversation>"
    )
    parent_conversation_source = str(parent_origin.get("name_source") or "<unknown>")
    parent_codex_url = str(parent_origin.get("codex_url") or "<unavailable>")
    parent_transcript_file = str(parent_origin.get("transcript_file") or "<unknown>")
    apply_helper = SKILL_DIR / "scripts" / "apply_worktree_bootstrap.py"
    # 父会话对话 context artifact 可能在 freeze 期写入（fail-open，可能缺失）。
    # 仅当文件确实存在时，才在 prompt 里加 RVF_PARENT_CONVERSATION_CONTEXT 标记与引用，
    # 用 $RVF_ARTIFACTS_DIR 相对形式与其它 artifact 引用保持一致。
    parent_context_path = ledger.artifact_path(PARENT_CONTEXT_ARTIFACT_NAME)
    parent_context_ref = (
        f"$RVF_ARTIFACTS_DIR/{PARENT_CONTEXT_ARTIFACT_NAME}"
        if parent_context_path.exists()
        else None
    )
    parent_context_marker_line = (
        f"{PARENT_CONTEXT_PROMPT_KEY}: {parent_context_ref}\n"
        if parent_context_ref is not None
        else ""
    )
    if worktree_mode == "inplace":
        worktree_instructions = (
            "你运行在 Cline Kanban task 的 inplace 模式中。执行 repo 是当前父 worktree；"
            "如果需要绝对路径，使用 `git rev-parse --show-toplevel`。不要重放 worktree bootstrap；"
            "本 task 与父会话共享同一个 dirty worktree，bootstrap artifacts 仅作冻结证据。\n\n"
            "如需 shell 环境，可只加载已生成的 review env：\n\n"
            "```sh\n"
            f"export RVF_RUN_DIR={shell_quote(str(ledger.run_dir))}\n"
            f"export CODEX_RVF_LOG_ROOT={shell_quote(str(ledger.root))}\n"
            f"export RVF_RUN_ID={shell_quote(str(ledger.run_id))}\n"
            'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"\n'
            '. "$RVF_ARTIFACTS_DIR/review-env.sh"\n'
            "```\n\n"
        )
    else:
        worktree_instructions = (
            "你运行在 Cline Kanban 为本 task 创建的独立 git worktree 中。执行 repo 是当前 task worktree；"
            "如果需要绝对路径，使用 `git rev-parse --show-toplevel`。上面的父 repo 仅作 metadata，"
            "不要回到父 worktree 运行 review/validate/fix。开始任何 review/validate/fix 前，必须先把父会话的 "
            "session-owned 未提交改动重放到当前 worktree：\n\n"
            "```sh\n"
            'RVF_TASK_REPO="$(git rev-parse --show-toplevel)"\n'
            f"export RVF_RUN_DIR={shell_quote(str(ledger.run_dir))}\n"
            f"export CODEX_RVF_LOG_ROOT={shell_quote(str(ledger.root))}\n"
            f"export RVF_RUN_ID={shell_quote(str(ledger.run_id))}\n"
            'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"\n'
            '. "$RVF_ARTIFACTS_DIR/review-env.sh"\n'
            'export RVF_REPO="$RVF_TASK_REPO"\n'
            f"python3 {shell_quote(str(apply_helper))} --metadata \"$RVF_WORKTREE_BOOTSTRAP\" --repo \"$RVF_REPO\"\n"
            "```\n\n"
        )
    return (
        "$review-validate-fix\n\n"
        f"{RVF_FORK_MARKER}\n"
        f"{CLINE_KANBAN_TASK_MARKER}\n"
        f"{plugin_deploy_prompt_block()}"
        "RVF_TARGET_REPO: .\n"
        f"RVF_PARENT_REPO: {cwd}\n"
        f"RVF_PARENT_CWD: {cwd}\n"
        f"RVF_RUN_ID: {ledger.run_id}\n"
        f"RVF_RUN_DIR: {ledger.run_dir}\n"
        "RVF_ARTIFACTS_DIR: $RVF_RUN_DIR/artifacts\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CONVERSATION_REF: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME_SOURCE: {parent_conversation_source}\n"
        f"RVF_PARENT_CODEX_URL: {parent_codex_url}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {transcript}\n"
        f"RVF_PARENT_TRANSCRIPT_FILE: {parent_transcript_file}\n"
        f"{parent_context_marker_line}"
        "RVF_REVIEW_ENV: $RVF_ARTIFACTS_DIR/review-env.sh\n"
        "RVF_REVIEW_AGENT_CONTEXT: $RVF_ARTIFACTS_DIR/review-agent-context.md\n"
        "RVF_ORIGIN_METADATA: $RVF_ARTIFACTS_DIR/origin.json\n"
        "RVF_ORIGINAL_FORK_PROMPT: $RVF_ARTIFACTS_DIR/fork.prompt.txt\n"
        f"RVF_DISPATCH=token={dispatch_prep.token}\n"
        f"RVF_PREP_FILE: {dispatch_prep.path}\n\n"
        f"Original {host_label} conversation trace:\n"
        f"- name/ref: `{parent_conversation_ref}`\n"
        f"- name source: `{parent_conversation_source}`\n"
        f"- open: `{parent_codex_url}`\n"
        f"- transcript: `{transcript}`\n"
        f"- origin metadata: `$RVF_ARTIFACTS_DIR/origin.json`\n\n"
        f"{worktree_instructions}"
        f"{cline_kanban_artifact_reference_lines(parent_conversation_context_ref=parent_context_ref)}"
        "本 task 由 UserPromptSubmit hook 调用 shared prepare 入口生成 review-env、scope.contract、review packet 等。"
        "开始任何 review/validate/fix 前，先 `cat $RVF_PREP_FILE` 确认 `rvf_run.shared_workflow_state.status == \"completed\"` 且 "
        "`artifacts` 字段齐全；齐全则跳过手动跑 `prepare_review_run.py`，直接 source `$RVF_REVIEW_ENV` 继续既有模式。"
        "若 `shared_workflow_state.status` 是 `failed` / `timeout` / `pending`，再按 SKILL.md fallback 手动跑 prepare。"
        "本 task 已经复用 `RVF_RUN_DIR`，所有 handoff、reviewer 输出、summary 和 events 都必须继续写入该 installed plugin state run。"
        "Handoff 默认开启时，必须持续维护 "
        "`$RVF_ARTIFACTS_DIR/handoff.md`，并在文件顶部保留 `## Origin` 区块，"
        "逐字写入上面的 original Codex conversation name/ref、name source、codex URL、transcript path、"
        "RVF run id 和 origin metadata path。最终回复第一行输出 "
        "`RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，"
        f"{HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION}。"
        "Stop hook 会把 `RVF_HANDOFF_FILE` marker 作为完成信号，run 结束时发送 OS 系统"
        "通知（kanban 来源的通知点击后可打开对应 task；不再自动用编辑器打开 handoff）。"
    )


def cline_kanban_client_env(ledger: RunLedger) -> dict[str, str]:
    env = {**os.environ, **ledger.env()}
    for name in SUPPRESS_ENV_NAMES:
        env.pop(name, None)
    return env


def parent_origin_is_kanban_task(parent_origin: dict[str, Any] | None) -> bool:
    if not isinstance(parent_origin, dict):
        return False
    for key in ("kanban_task_id", "cline_kanban_task_id"):
        value = parent_origin.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def resume_dispatch_from_confirmation_marker(
    marker_payload: dict[str, Any],
) -> dict[str, Any]:
    """Re-run the Cline Kanban dispatch flow after a user-confirmed bootstrap.

    Reads the persisted dispatch context, re-loads the prep file, and calls the
    same Cline Kanban entry point with ``skip_freeze=True`` and ``skip_size_check=True``
    so freeze artifacts already on disk are reused and the user is not re-prompted.
    """
    context = marker_payload.get("dispatch_context")
    if not isinstance(context, dict):
        raise ValueError("confirmation marker missing dispatch_context")
    token = marker_payload.get("token") or context.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("confirmation marker missing token")
    lookup = rvf_prep_file.read_prep_file(token)
    if lookup.status != "valid" or lookup.payload is None:
        raise ValueError(f"prep file lookup failed: {lookup.status} ({lookup.error})")
    dispatch_prep = rvf_prep_file.PrepFileRecord(
        token=lookup.token, path=lookup.path, payload=dict(lookup.payload)
    )
    run_id = context.get("run_id")
    run_dir = context.get("run_dir")
    cwd = str(context.get("cwd") or "")
    if not cwd:
        raise ValueError("dispatch_context missing cwd")
    ledger = start_run(
        "stop-hook-resume",
        repo=cwd,
        cwd=cwd,
        run_id=run_id if isinstance(run_id, str) else None,
        run_dir=Path(run_dir) if isinstance(run_dir, str) and run_dir else None,
    )
    parent_origin = context.get("parent_origin") if isinstance(context.get("parent_origin"), dict) else {}
    parent_thread_path_raw = context.get("parent_thread_path")
    parent_thread_path = (
        Path(parent_thread_path_raw)
        if isinstance(parent_thread_path_raw, str) and parent_thread_path_raw
        else None
    )
    worktree_mode = str(context.get("worktree_mode") or DEFAULT_CLINE_KANBAN_WORKTREE_MODE)
    task_payload = start_cline_kanban_task(
        cwd=cwd,
        prompt_path=str(context.get("prompt_path") or ""),
        dispatch_prep=dispatch_prep,
        parent_session_id=str(context.get("parent_session_id") or ""),
        parent_thread_path=parent_thread_path,
        parent_origin=parent_origin,
        ledger=ledger,
        task_title=str(context.get("task_title") or "RVF"),
        model=None,
        reasoning_effort=None,
        base_ref=str(context.get("base_ref") or ""),
        worktree_mode=worktree_mode,
        skip_freeze=True,
        skip_size_check=True,
    )
    target_flow = dispatch_prep_target_flow("cline-kanban", cline_kanban_worktree_mode=worktree_mode)
    update_dispatch_prep_file(
        ledger=ledger,
        record=dispatch_prep,
        target_flow=target_flow,
        target_worktree=(
            str(task_payload.get("workspace_path"))
            if isinstance(task_payload.get("workspace_path"), str)
            else None
        ),
        target_kanban_task_id=(
            str(task_payload.get("cline_kanban_task_id"))
            if isinstance(task_payload.get("cline_kanban_task_id"), str)
            else None
        ),
    )
    return task_payload


_PREP_EXPIRY_BUFFER_SECONDS = 60


def _refresh_prep_file_expiry_for_confirm_marker(
    *,
    prep_path: Path,
    marker_path: Path,
) -> None:
    """Bump prep file ``expires_at`` so it outlives the bootstrap-confirm marker.

    Prep file TTL (default 300s) starts at dispatch prepare; the confirm marker
    TTL (default 300s) starts later, after freeze. Without this refresh the
    prep file expires before the marker, so a user who replies ``yes`` inside
    the marker's TTL window can still fail resume with ``expired`` prep lookup.
    We rewrite the prep file JSON in place with a refreshed ``expires_at``
    derived from the marker's own ``expires_at`` + a small buffer.
    """
    try:
        marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    marker_expires_at = marker_payload.get("expires_at") if isinstance(marker_payload, dict) else None
    if not isinstance(marker_expires_at, (int, float)):
        return
    target_ts = float(marker_expires_at) + _PREP_EXPIRY_BUFFER_SECONDS
    try:
        prep_payload = json.loads(prep_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(prep_payload, dict):
        return
    new_expires_at = rvf_prep_file.format_timestamp(
        datetime.fromtimestamp(target_ts, tz=timezone.utc)
    )
    prep_payload["expires_at"] = new_expires_at
    try:
        prep_path.write_text(
            json.dumps(prep_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def maybe_block_dispatch_on_bootstrap_size(
    *,
    ledger: RunLedger,
    startup_prepare: dict[str, Any],
    dispatch_prep: rvf_prep_file.PrepFileRecord,
    parent_session_id: str,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    cwd: str,
    task_title: str,
    base_ref: str,
    worktree_mode: str,
    prompt_path: str,
) -> None:
    metadata = startup_prepare.get("metadata") if isinstance(startup_prepare.get("metadata"), dict) else {}
    bootstrap_payload = metadata.get("worktree_bootstrap_metadata")
    if not isinstance(bootstrap_payload, dict):
        return
    exempt = parent_origin_is_kanban_task(parent_origin)
    decision = rvf_bootstrap_confirm.compute_decision(
        bootstrap_payload,
        exempt_kanban_followup=exempt,
    )
    rvf_bootstrap_confirm.sweep_expired(state_dir())
    if decision.exempt and decision.bootstrap_kind == "full-dirty":
        ledger.event(
            phase="fork",
            event="bootstrap_confirm_exempt",
            status="advisory",
            reason_code="bootstrap_confirm_exempt",
            repo=cwd,
            cwd=cwd,
            cline_kanban_task_id=parent_origin.get("kanban_task_id") if isinstance(parent_origin, dict) else None,
            **decision.to_summary(),
        )
        return
    if not decision.needs_confirmation:
        return
    dispatch_context = {
        "cwd": cwd,
        "prompt_path": str(prompt_path),
        "parent_session_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path else None,
        "parent_origin": parent_origin,
        "task_title": task_title,
        "base_ref": base_ref,
        "worktree_mode": worktree_mode,
        "dispatch_prep_file_path": str(dispatch_prep.path),
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
    }
    marker = rvf_bootstrap_confirm.write_marker(
        state_dir(),
        session_id=parent_session_id or "unknown-session",
        token=dispatch_prep.token,
        decision=decision,
        dispatch_context=dispatch_context,
    )
    _refresh_prep_file_expiry_for_confirm_marker(
        prep_path=dispatch_prep.path,
        marker_path=marker,
    )
    ledger.event(
        phase="fork",
        event="bootstrap_confirm_required",
        status="blocked",
        reason_code="bootstrap_confirm_required",
        repo=cwd,
        cwd=cwd,
        paths={"confirmation_marker": str(marker)},
        **decision.to_summary(),
    )
    raise BootstrapConfirmationRequired(decision, marker)


def start_cline_kanban_task(
    *,
    cwd: str,
    prompt_path: str,
    dispatch_prep: rvf_prep_file.PrepFileRecord,
    parent_session_id: str,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    ledger: RunLedger,
    task_title: str,
    model: str | None,
    reasoning_effort: str | None,
    base_ref: str,
    worktree_mode: str,
    skip_freeze: bool = False,
    skip_size_check: bool = False,
) -> dict[str, Any]:
    del model, reasoning_effort
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    target_flow = dispatch_prep_target_flow("cline-kanban", cline_kanban_worktree_mode=worktree_mode)
    if skip_freeze:
        existing_state = dispatch_prep.payload.get("rvf_run") if isinstance(dispatch_prep.payload, dict) else None
        existing_shared = existing_state.get("shared_workflow_state") if isinstance(existing_state, dict) else None
        run_dir_str = ledger.run_dir if ledger.run_dir is not None else None
        metadata_path_guess = Path(run_dir_str) / "artifacts" / "cline-kanban-dispatch-prepare.json" if run_dir_str else None
        metadata: dict[str, Any] = {}
        if metadata_path_guess and metadata_path_guess.exists():
            try:
                metadata = json.loads(metadata_path_guess.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
        if not metadata and isinstance(existing_shared, dict):
            artifacts = existing_shared.get("artifacts")
            if isinstance(artifacts, dict):
                metadata = {**existing_shared, **artifacts}
        startup_prepare = {"metadata": metadata, "metadata_path": str(metadata_path_guess) if metadata_path_guess else None}
    else:
        startup_prepare = freeze_cline_kanban_dispatch_artifacts(
            cwd=cwd,
            parent_session_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            prompt_path=prompt_path,
            ledger=ledger,
            dispatch_prep=dispatch_prep,
            target_flow=target_flow,
        )
        updated_prep = startup_prepare.get("dispatch_prep_record")
        if isinstance(updated_prep, rvf_prep_file.PrepFileRecord):
            dispatch_prep = updated_prep
    if not skip_size_check:
        maybe_block_dispatch_on_bootstrap_size(
            ledger=ledger,
            startup_prepare=startup_prepare,
            dispatch_prep=dispatch_prep,
            parent_session_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            parent_origin=parent_origin,
            cwd=cwd,
            task_title=task_title,
            base_ref=base_ref,
            worktree_mode=worktree_mode,
            prompt_path=prompt_path,
        )
    task_prompt = cline_kanban_task_prompt(
        cwd=cwd,
        prompt_path=prompt_path,
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        parent_origin=parent_origin,
        ledger=ledger,
        dispatch_prep=dispatch_prep,
        worktree_mode=worktree_mode,
    )
    task_prompt_path = ledger.artifact("cline-kanban-task.prompt.md", task_prompt)
    if not task_prompt_path:
        raise RuntimeError("failed to write Cline Kanban task prompt artifact")

    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    start_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD", DEFAULT_CLINE_KANBAN_START_CMD)
    start_timeout = os.environ.get(
        "CODEX_RVF_CLINE_KANBAN_START_TIMEOUT",
        str(DEFAULT_CLINE_KANBAN_START_TIMEOUT_SECONDS),
    )
    tmux_session = os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", DEFAULT_CLINE_KANBAN_TMUX_SESSION)
    # 传给 cline-kanban 的 --agent-id 会覆盖其默认 executor 选择；默认镜像父
    # （main agent）harness 以复用 context + prompt cache，CODEX_RVF_CLINE_KANBAN_AGENT_ID
    # 可显式钉死某个 fix harness。详见 resolve_cline_kanban_agent_id 注释。
    agent_id = resolve_cline_kanban_agent_id(parent_thread_path)
    auto_review_enabled = is_truthy(os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED"))
    auto_review_mode = os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", "commit").strip() or "commit"
    start_in_plan_mode = is_truthy(os.environ.get("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE"))
    env = cline_kanban_client_env(ledger)

    ensure_command = [
        sys.executable,
        str(client),
        "ensure",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--start-cmd",
        start_cmd,
        "--start-timeout",
        start_timeout,
        "--tmux-session",
        tmux_session,
        "--start-if-needed",
    ]
    ensure_completed = subprocess.run(ensure_command, capture_output=True, text=True, env=env, check=False)
    ensure_payload = parse_json_command_output(ensure_completed, label="Cline Kanban ensure")
    ledger.artifact(
        "cline-kanban-ensure.json",
        {
            "command": ensure_command,
            "returncode": ensure_completed.returncode,
            "stdout": ensure_completed.stdout,
            "stderr": ensure_completed.stderr,
            "payload": ensure_payload,
        },
    )

    create_command = [
        sys.executable,
        str(client),
        "create",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--base-ref",
        base_ref,
        "--prompt",
        task_prompt,
        "--title",
        task_title,
        "--agent-id",
        agent_id,
        "--parent-session-id",
        parent_session_id,
        "--worktree-mode",
        worktree_mode,
        "--prep-file-path",
        str(dispatch_prep.path),
    ]
    if start_in_plan_mode:
        create_command.append("--start-in-plan-mode")
    if auto_review_enabled:
        create_command.extend(["--auto-review-enabled", "--auto-review-mode", auto_review_mode])
    create_completed = subprocess.run(create_command, capture_output=True, text=True, env=env, check=False)
    create_payload = parse_json_command_output(create_completed, label="Cline Kanban task create")
    ledger.artifact(
        "cline-kanban-create-task.json",
        {
            "command": create_command,
            "returncode": create_completed.returncode,
            "stdout": create_completed.stdout,
            "stderr": create_completed.stderr,
            "payload": create_payload,
        },
    )
    task_id = str(create_payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError(f"Cline Kanban task create response did not include task_id: {create_payload!r}")
    suppression_path = write_kanban_task_suppression(task_id=task_id, cwd=cwd, ledger=ledger)

    start_command = [
        sys.executable,
        str(client),
        "start",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
        "--worktree-mode",
        worktree_mode,
    ]
    start_completed = subprocess.run(start_command, capture_output=True, text=True, env=env, check=False)
    start_payload = parse_json_command_output(start_completed, label="Cline Kanban task start")
    ledger.artifact(
        "cline-kanban-start-task.json",
        {
            "command": start_command,
            "returncode": start_completed.returncode,
            "stdout": start_completed.stdout,
            "stderr": start_completed.stderr,
            "payload": start_payload,
        },
    )
    metadata = startup_prepare.get("metadata") if isinstance(startup_prepare.get("metadata"), dict) else {}
    workspace_path = cline_kanban_workspace_path(start_payload, create_payload)
    if workspace_path is None:
        raise RuntimeError(
            "Cline Kanban task create/start response did not include task execution workspace_path/workspacePath"
        )
    if worktree_mode != "inplace" and _same_existing_path(Path(workspace_path), Path(cwd)):
        raise RuntimeError(
            "Cline Kanban task create/start resolved workspace_path to the parent project path "
            f"in {worktree_mode} mode; expected the task execution worktree"
        )
    return {
        "cline_kanban_task_id": task_id,
        "cline_kanban_base_ref": base_ref,
        "cline_kanban_task_prompt_path": task_prompt_path,
        "cline_kanban_stop_hook_suppression_path": suppression_path,
        "cline_kanban_ensure": ensure_payload,
        "cline_kanban_create": create_payload,
        "cline_kanban_start": start_payload,
        "cline_kanban_task_cmd": task_cmd,
        "cline_kanban_start_cmd": start_cmd,
        "cline_kanban_tmux_session": tmux_session,
        "cline_kanban_agent_id": agent_id,
        "cline_kanban_worktree_mode": worktree_mode,
        "cline_kanban_prep_file_path": str(dispatch_prep.path),
        "cline_kanban_auto_review_enabled": auto_review_enabled,
        "cline_kanban_auto_review_mode": auto_review_mode if auto_review_enabled else None,
        "workspace_path": workspace_path,
        "startup_prepare_metadata_path": startup_prepare.get("metadata_path"),
        "worktree_bootstrap_path": metadata.get("worktree_bootstrap"),
        "worktree_bootstrap_patch_path": metadata.get("worktree_bootstrap_patch"),
        "worktree_bootstrap_files_dir": metadata.get("worktree_bootstrap_files_dir"),
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            prepare_metadata=metadata,
        ),
    }


def start_cline_kanban_followup_message(
    *,
    project_path: str,
    task_id: str,
    attempt_id: str | None,
    prompt: str,
    ledger: RunLedger,
    idempotency_key: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    prompt_path = ledger.artifact("kanban-followup.prompt.md", prompt)
    if not prompt_path:
        raise RuntimeError("failed to write Cline Kanban follow-up prompt artifact")

    # 默认沿用每次 Stop 唯一的 ledger.run_id 作幂等键（主派发路径）；S2 重投显式传入
    # 稳定键（rvf-redispatch-<task>-<token>），使 Kanban 侧对「同一条 stranded 重投」幂等。
    command = [
        sys.executable,
        str(client),
        "message",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
        "--prompt-file",
        prompt_path,
        "--source",
        "review-validate-fix",
        "--idempotency-key",
        idempotency_key or ledger.run_id,
    ]
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])

    # timeout 默认 None=主路径行为不变（无内层超时，由 Codex 链路 30s 总预算兜底）；
    # S2 重投显式传入有界 timeout，避免在 best-effort 路径上无界阻塞吃光预算。
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
        timeout=timeout,
    )
    command_path = ledger.artifact(
        "kanban-followup-message.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    payload = parse_json_command_output(completed, label="Cline Kanban task message")
    message_id = str(payload.get("message_id") or payload.get("messageId") or "").strip()
    if not message_id:
        raise RuntimeError(f"Cline Kanban task message response did not include message_id: {payload!r}")
    payload["message_id"] = message_id
    payload.setdefault("task_id", task_id)
    if attempt_id:
        payload.setdefault("attempt_id", attempt_id)

    ledger.artifact(
        "kanban-followup-message-result.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "payload": payload,
        },
    )
    payload["command_artifact_path"] = command_path
    payload["prompt_path"] = prompt_path
    payload["task_cmd"] = task_cmd
    payload["project_path"] = project_path
    return payload


def _iter_nested_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_dicts(child)


def _task_id_from_mapping(value: dict[str, Any]) -> str | None:
    for key in ("id", "task_id", "taskId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _task_title_from_mapping(value: dict[str, Any]) -> str | None:
    for key in ("title", "name", "task_title", "taskTitle", "task_name", "taskName"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def kanban_task_title_from_payload(payload: dict[str, Any], task_id: str) -> str | None:
    expected = task_id.strip()
    if not expected:
        return None
    for candidate in _iter_nested_dicts(payload):
        if _task_id_from_mapping(candidate) != expected:
            continue
        title = _task_title_from_mapping(candidate)
        if title:
            return title
    return None


def cline_kanban_state_dir() -> Path:
    value = os.environ.get("CODEX_RVF_CLINE_KANBAN_STATE_DIR")
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return DEFAULT_CLINE_KANBAN_STATE_DIR


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _kanban_session_matches_project(session: dict[str, Any], *, task_id: str, project_path: Path) -> bool:
    session_task_id = session.get("taskId") or session.get("task_id") or session.get("id")
    if session_task_id != task_id:
        return False
    workspace_path = session.get("workspacePath") or session.get("workspace_path")
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return False
    return _same_existing_path(Path(workspace_path), project_path)


def _candidate_kanban_board_paths(*, project_path: Path, task_id: str) -> list[Path]:
    workspaces_dir = cline_kanban_state_dir() / "workspaces"
    if not workspaces_dir.is_dir():
        return []
    candidates: list[Path] = []

    project_named_board = workspaces_dir / project_path.name / "board.json"
    if project_named_board.exists():
        candidates.append(project_named_board)

    for sessions_path in sorted(workspaces_dir.glob("*/sessions.json")):
        try:
            payload = json.loads(sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        sessions = [value for value in payload.values() if isinstance(value, dict)]
        if any(_kanban_session_matches_project(session, task_id=task_id, project_path=project_path) for session in sessions):
            board_path = sessions_path.with_name("board.json")
            if board_path.exists() and board_path not in candidates:
                candidates.insert(0, board_path)

    return candidates


def lookup_cline_kanban_board_task_title(
    *,
    project_path: str,
    task_id: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    project = Path(project_path).expanduser()
    checked: list[str] = []
    errors: list[str] = []
    for board_path in _candidate_kanban_board_paths(project_path=project, task_id=task_id):
        checked.append(str(board_path))
        try:
            payload = json.loads(board_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{board_path}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{board_path}: non-object JSON")
            continue
        title = kanban_task_title_from_payload(payload, task_id)
        if title:
            artifact = ledger.artifact(
                "kanban-followup-board-title-lookup.json",
                {
                    "state_dir": str(cline_kanban_state_dir()),
                    "project_path": project_path,
                    "task_id": task_id,
                    "checked": checked,
                    "matched_board": str(board_path),
                    "title": title,
                    "errors": errors,
                },
            )
            return {
                "title": title,
                "source": "cline_kanban_board_lookup",
                "artifact": artifact,
            }
    artifact = ledger.artifact(
        "kanban-followup-board-title-lookup.json",
        {
            "state_dir": str(cline_kanban_state_dir()),
            "project_path": project_path,
            "task_id": task_id,
            "checked": checked,
            "errors": errors,
        },
    )
    return {
        "title": None,
        "source": "cline_kanban_board_lookup_missing_title",
        "artifact": artifact,
    }


def lookup_cline_kanban_task_title(
    *,
    project_path: str,
    task_id: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    command = [
        sys.executable,
        str(client),
        "list",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    artifact = ledger.artifact(
        "kanban-followup-task-lookup.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        board_lookup = lookup_cline_kanban_board_task_title(
            project_path=project_path,
            task_id=task_id,
            ledger=ledger,
        )
        if board_lookup.get("title"):
            board_lookup["task_list_lookup"] = {
                "source": "cline_kanban_task_lookup_failed",
                "artifact": artifact,
                "error": completed.stderr.strip() or completed.stdout.strip(),
            }
            return board_lookup
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_failed",
            "artifact": artifact,
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_invalid_json",
            "artifact": artifact,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_invalid_payload",
            "artifact": artifact,
        }
    title = kanban_task_title_from_payload(payload, task_id)
    if not title:
        board_lookup = lookup_cline_kanban_board_task_title(
            project_path=project_path,
            task_id=task_id,
            ledger=ledger,
        )
        if board_lookup.get("title"):
            board_lookup["task_list_lookup"] = {
                "source": "cline_kanban_task_lookup_missing_title",
                "artifact": artifact,
            }
            return board_lookup
    return {
        "title": title,
        "source": "cline_kanban_task_lookup" if title else "cline_kanban_task_lookup_missing_title",
        "artifact": artifact,
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
    launch_mode: str | None = None,
    origin_repo: str | None = None,
) -> dict[str, Any]:
    mode = (
        launch_mode
        if launch_mode is not None
        else os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE)
    ).strip().lower()
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

    parent_name_lookup = parent_thread_name_from_app_server(parent_session_id, cwd)
    parent_origin = parent_conversation_origin(
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        run_id=ledger.run_id,
        parent_thread_name=parent_name_lookup.get("name"),
        name_lookup=parent_name_lookup,
    )
    origin_path = ledger.artifact("origin.json", parent_origin)
    effective_prompt = add_parent_origin_to_rvf_fork_prompt(
        effective_prompt,
        parent_origin=parent_origin,
        origin_path=origin_path,
    )
    cline_kanban_worktree_mode = (
        automatic_cline_kanban_worktree_mode()
        if mode in {"cline-kanban", "cline", "kanban", "ck"}
        else None
    )
    cline_kanban_base_ref = (
        automatic_cline_kanban_base_ref(cwd)
        if mode in {"cline-kanban", "cline", "kanban", "ck"}
        else None
    )
    target_flow = dispatch_prep_target_flow(
        mode,
        cline_kanban_worktree_mode=cline_kanban_worktree_mode,
    )
    dispatch_prep = write_dispatch_prep_file(
        ledger=ledger,
        origin_session_id=parent_session_id,
        origin_repo=origin_repo or cwd,
        origin_cwd=cwd,
        target_flow=target_flow,
        target_worktree=None if target_flow == "flow-2-branch" else cwd,
        origin_metadata_path=origin_path,
        parent_thread_path=parent_thread_path,
    )
    effective_prompt = add_dispatch_prep_to_prompt(effective_prompt, dispatch_prep)
    prompt_path = ledger.artifact("fork.prompt.txt", effective_prompt)
    ledger.event(
        phase="fork",
        event="started",
        status="started",
        reason_code="fork_started",
        parent_thread_id=parent_session_id,
        paths={
            key: value
            for key, value in {
                "prompt": prompt_path,
                "dispatch_prep_file": str(dispatch_prep.path),
            }.items()
            if value
        },
        mode=mode,
        log_prefix=log_prefix,
    )

    dispatch_prep_fields = dispatch_prep_summary_fields(dispatch_prep, target_flow=target_flow)
    result: dict[str, Any] = {
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "parent_conversation_ref": parent_origin.get("label"),
        "parent_conversation_name": parent_origin.get("label"),
        "parent_conversation_name_source": parent_origin.get("name_source"),
        "parent_thread_name_lookup": parent_name_lookup,
        "parent_codex_url": parent_origin.get("codex_url"),
        "parent_origin_path": origin_path,
        "parent_transcript_file": parent_origin.get("transcript_file"),
        "cwd": cwd,
        "prompt_path": prompt_path,
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "cline_kanban_worktree_mode": cline_kanban_worktree_mode,
        **dispatch_prep_fields,
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
    elif mode in {"cline-kanban", "cline", "kanban", "ck"}:
        result["mode"] = "cline-kanban"
        result["legacy_gui_fallback_enabled"] = legacy_gui_fallback_enabled()
        if parent_thread_path is None:
            result.update(
                {
                    "status": "cline-kanban-unavailable",
                    "error": (
                        "CODEX_RVF_FORK_MODE=cline-kanban requires a readable parent "
                        "transcript/session scope anchor; task was not started."
                    ),
                }
            )
        elif not cwd:
            result.update(
                {
                    "status": "cline-kanban-unconfigured",
                    "error": "CODEX_RVF_FORK_MODE=cline-kanban requires a target repo cwd.",
                }
            )
        elif not cline_kanban_base_ref:
            result.update(
                {
                    "status": "cline-kanban-unconfigured",
                    "error": "CODEX_RVF_FORK_MODE=cline-kanban requires a resolvable current HEAD.",
                }
            )
        elif not prompt_path:
            result.update(
                {
                    "status": "cline-kanban-unavailable",
                    "error": "fork prompt artifact is unavailable; Cline Kanban task was not started.",
                }
            )
        else:
            task_title = str(parent_origin["task_title"])
            try:
                task_payload = start_cline_kanban_task(
                    cwd=cwd,
                    prompt_path=prompt_path,
                    dispatch_prep=dispatch_prep,
                    parent_session_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    parent_origin=parent_origin,
                    ledger=ledger,
                    task_title=task_title,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    base_ref=cline_kanban_base_ref,
                    worktree_mode=cline_kanban_worktree_mode or DEFAULT_CLINE_KANBAN_WORKTREE_MODE,
                )
                dispatch_prep = update_dispatch_prep_file(
                    ledger=ledger,
                    record=dispatch_prep,
                    target_flow=target_flow,
                    target_worktree=(
                        str(task_payload.get("workspace_path"))
                        if isinstance(task_payload.get("workspace_path"), str)
                        else None
                    ),
                    target_kanban_task_id=(
                        str(task_payload.get("cline_kanban_task_id"))
                        if isinstance(task_payload.get("cline_kanban_task_id"), str)
                        else None
                    ),
                )
                result.update(
                    {
                        "status": "cline-kanban-started",
                        "task_title": task_title,
                        "cline_kanban_task_title": task_title,
                        **task_payload,
                        **dispatch_prep_summary_fields(dispatch_prep, target_flow=target_flow),
                    }
                )
            except BootstrapConfirmationRequired as confirm_exc:
                system_message = rvf_bootstrap_confirm.format_system_message(
                    confirm_exc.decision,
                    marker_path=confirm_exc.marker_path,
                )
                result.update(
                    {
                        "status": "bootstrap-confirm-required",
                        "bootstrap_confirm_marker_path": str(confirm_exc.marker_path),
                        "bootstrap_confirm_decision": confirm_exc.decision.to_summary(),
                        "system_message": system_message,
                    }
                )
            except Exception as exc:
                result.update(
                    {
                        "status": "cline-kanban-unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        if dispatch_flow.should_attempt_legacy_gui_fallback(
            primary_result=result,
            backend_selection_mode=(extra_summary or {}).get("backend_selection_mode"),
            fallback_enabled=bool(result.get("legacy_gui_fallback_enabled")),
        ):
            cline_failure = {
                "status": result.get("status"),
                "error": result.get("error"),
                "mode": "cline-kanban",
            }
            ledger.event(
                phase="fork",
                event="legacy_gui_fallback_started",
                status="started",
                reason_code="legacy_gui_fallback_started",
                parent_thread_id=parent_session_id,
                paths={"prompt": prompt_path} if prompt_path else {},
                primary_backend="cline-kanban",
                fallback_backend="gui",
                error=cline_failure.get("error"),
            )
            try:
                fallback_payload = run_app_server_fork(
                    parent_thread_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    cwd=cwd,
                    prompt=effective_prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    log_path=ledger.summary_path,
                )
                result.update(fallback_payload)
                result["mode"] = "legacy-gui"
                result["effective_backend"] = "legacy-gui"
                result["legacy_gui_fallback"] = {
                    "started": True,
                    "primary_backend": "cline-kanban",
                    "fallback_backend": "gui",
                    "primary_failure": cline_failure,
                }
            except Exception as exc:
                result["legacy_gui_fallback"] = {
                    "started": False,
                    "primary_backend": "cline-kanban",
                    "fallback_backend": "gui",
                    "primary_failure": cline_failure,
                    "error": f"{type(exc).__name__}: {exc}",
                }
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
                    f"Unsupported {mode_env_name}={mode!r}. Use auto, gui, cline-kanban, dry-run, "
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
    elif status == "cline-kanban-started":
        reason_code = "cline_kanban_task_started"
    elif status == "cline-kanban-unconfigured":
        reason_code = "cline_kanban_unconfigured"
    elif status == "cline-kanban-unavailable":
        reason_code = "cline_kanban_unavailable"
    elif status == "bootstrap-confirm-required":
        reason_code = "bootstrap_confirm_required"

    event_paths: dict[str, Any] = {}
    if prompt_path:
        event_paths["prompt"] = prompt_path
    if result.get("rvf_dispatch_prep_file_path"):
        event_paths["dispatch_prep_file"] = result["rvf_dispatch_prep_file_path"]
    if result.get("parent_origin_path"):
        event_paths["origin"] = result["parent_origin_path"]
    if result.get("app_server_requests_path"):
        event_paths["app_server_requests"] = result["app_server_requests_path"]
    if result.get("cline_kanban_task_prompt_path"):
        event_paths["cline_kanban_task_prompt"] = result["cline_kanban_task_prompt_path"]
    if result.get("worktree_bootstrap_path"):
        event_paths["worktree_bootstrap"] = result["worktree_bootstrap_path"]
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
    elif status == "cline-kanban-started":
        ledger.event(
            phase="fork",
            event="completed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            mode="cline-kanban",
            cline_kanban_task_id=result.get("cline_kanban_task_id"),
            cline_kanban_task_title=result.get("task_title"),
            cline_kanban_base_ref=result.get("cline_kanban_base_ref"),
            parent_conversation_ref=result.get("parent_conversation_ref"),
            parent_conversation_name=result.get("parent_conversation_name"),
            parent_conversation_name_source=result.get("parent_conversation_name_source"),
            parent_codex_url=result.get("parent_codex_url"),
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
    elif status == "bootstrap-confirm-required":
        marker_path_str = result.get("bootstrap_confirm_marker_path")
        if isinstance(marker_path_str, str) and marker_path_str:
            event_paths["confirmation_marker"] = marker_path_str
        ledger.event(
            phase="fork",
            event="bootstrap_confirm_blocked",
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
        detail = None
        message = (
            "manual fork prompt prepared; no Terminal was launched and no "
            "current-chat continuation was submitted."
        )
    elif status == "app-server-started":
        detail = None
        message = "Codex GUI/app-server fork was started."
    elif status == "cline-kanban-started":
        workspace = result.get("workspace_path")
        workspace_line = f" workspace={workspace}." if isinstance(workspace, str) and workspace else ""
        if result.get("rvf_dispatch_target_flow") == "flow-2-inplace":
            detail = (
                "pause_origin_edits=false"
                + (f",workspace={workspace}" if isinstance(workspace, str) and workspace else "")
            )
            message = (
                "Cline Kanban RVF task was created and started."
                f"{workspace_line} Flow 2 inplace mode 使用当前 worktree；"
                "无需暂停 origin worktree 编辑。"
            )
        else:
            detail = (
                "pause_origin_edits=true"
                + (f",workspace={workspace}" if isinstance(workspace, str) and workspace else "")
            )
            message = (
                "Cline Kanban RVF task was created and started."
                f"{workspace_line} Flow 2 branch mode 已使用独立 task worktree；"
                "请暂停在 origin worktree 继续编辑，等 RVF_HANDOFF_FILE 返回后再合并。"
            )
    elif status == "cline-kanban-unconfigured":
        detail = None
        message = str(result.get("error") or "Cline Kanban RVF mode is not configured.")
    elif status == "cline-kanban-unavailable":
        detail = None
        message = str(result.get("error") or "Cline Kanban is unavailable; task was not started.")
    elif status == "bootstrap-confirm-required":
        detail = None
        message = str(
            result.get("system_message")
            or "review-validate-fix: bootstrap dirty 量超阈，已暂停 dispatch；请回复 yes/Yes/YES 继续或其他内容取消。"
        )
    elif status in {"desktop-control-unavailable-report", "desktop-control-unavailable-fail"}:
        detail = None
        report_reason = result.get("report_reason")
        message = report_reason if isinstance(report_reason, str) else "Codex GUI fork unavailable."
    else:
        detail = None
        message = f"{log_prefix} triggered: {status}."

    summary_fields = dict(result)
    summary_fields.pop("status", None)
    result_state_fields = {
        key: value
        for key, value in summary_fields.items()
        if key == "rvf_state" or key.startswith("rvf_")
    }
    if extra_summary:
        summary_fields.update(extra_summary)
    if result_state_fields.get("rvf_state"):
        summary_fields.update(result_state_fields)
    if "rvf_state" not in summary_fields:
        backend_raw = str(summary_fields.get("backend") or result.get("mode") or mode)
        canonical_backend = normalize_rvf_backend(backend_raw)
        if canonical_backend is not None:
            summary_fields.update(
                stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend=canonical_backend,
                    backend_raw=backend_raw,
                )
            )
    if status == "bootstrap-confirm-required":
        ledger.summary(
            status=str(status),
            reason_code=reason_code,
            message=message,
            **summary_fields,
        )
        return {"continue": True, "systemMessage": message}
    return ledger.hook_payload(
        status=str(status),
        reason_code=reason_code,
        message=message,
        detail=detail,
        **summary_fields,
    )


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


def rvf_mode() -> str:
    return dispatch_flow.rvf_mode_from_value(os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE))


def normalize_backend_from_env(
    event: dict[str, Any] | None = None,
    mode_env_name: str = "CODEX_RVF_FORK_MODE",
) -> str:
    return dispatch_flow.backend_from_values(
        mode=os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE),
        fork_mode=os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE),
        in_kanban_task=bool(event is not None and current_kanban_task_id(event)),
    )


def fork_mode_selection_from_env(mode_env_name: str = "CODEX_RVF_FORK_MODE") -> str:
    return dispatch_flow.backend_selection_mode_from_fork_mode(
        os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE)
    )


def legacy_gui_fallback_enabled() -> bool:
    return not is_falsey(os.environ.get("CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK", "0"))


def cline_kanban_failure_allows_legacy_gui_fallback(result: dict[str, Any]) -> bool:
    return dispatch_flow.cline_kanban_failure_allows_legacy_gui_fallback(result)


def launch_mode_for_backend(backend: str) -> str:
    return dispatch_flow.launch_mode_for_backend(backend)


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
    cwd = fork_cwd_for_event(event, repo)
    parent_thread_path = parent_thread_path_for_origin(
        event,
        ledger=ledger,
        repo=repo,
        cwd=cwd,
    )
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
        origin_repo=repo,
    )


def launch_backend(
    decision: StopDecision,
    event: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    if not decision.repo:
        return skip_payload(
            "Stop decision did not include a target repo.",
            ledger,
            "missing_target_repo",
            backend=decision.backend,
        )
    cwd = decision.cwd or fork_cwd_for_event(event, decision.repo)
    if decision.backend == "kanban-followup":
        task_id = current_kanban_task_id(event)
        if not task_id:
            return skip_payload(
                "Cline Kanban follow-up backend requires KANBAN_TASK_ID or task_id in the Stop event.",
                ledger,
                "kanban_followup_missing_task_id",
                repo=decision.repo,
                cwd=cwd,
                backend=decision.backend,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )
        attempt_id = current_kanban_attempt_id(event)
        project_path = current_kanban_project_path(event, decision.repo)
        source_session_id = session_id_from_event(event) or parent_thread_id_from_event(event)
        source_thread_path = parent_thread_path_for_origin(
            event,
            ledger=ledger,
            repo=decision.repo,
            cwd=cwd,
        )
        source_name_lookup = parent_thread_name_from_app_server(source_session_id, cwd)
        codex_origin = parent_conversation_origin(
            parent_session_id=source_session_id,
            parent_thread_path=source_thread_path,
            run_id=ledger.run_id,
            parent_thread_name=source_name_lookup.get("name"),
            name_lookup=source_name_lookup,
        )
        task_title = current_kanban_task_title(event)
        task_title_source = "cline_kanban_task_env" if task_title else None
        task_lookup: dict[str, Any] | None = None
        if not task_title:
            task_lookup = lookup_cline_kanban_task_title(
                project_path=project_path,
                task_id=task_id,
                ledger=ledger,
            )
            lookup_title = task_lookup.get("title")
            if isinstance(lookup_title, str) and lookup_title.strip():
                task_title = lookup_title.strip()
                task_title_source = str(task_lookup.get("source") or "cline_kanban_task_lookup")
        source_origin = source_origin_for_kanban_task(
            task_id=task_id,
            attempt_id=attempt_id,
            task_title=task_title,
            task_title_source=task_title_source,
            fallback_origin=codex_origin,
        )
        origin_path = ledger.artifact("origin.json", source_origin)
        source_origin_fields = parent_origin_summary_fields(
            parent_session_id=source_session_id,
            parent_thread_path=source_thread_path,
            parent_origin=source_origin,
            parent_name_lookup=source_name_lookup,
            origin_path=origin_path,
        )
        prompt = kanban_followup_review_validate_fix_prompt(
            task_id=task_id,
            attempt_id=attempt_id,
            target_repo=decision.repo,
            cwd=cwd,
            ledger=ledger,
            source_origin=source_origin,
            origin_path=origin_path,
        )
        dispatch_prep = write_dispatch_prep_file(
            ledger=ledger,
            origin_session_id=source_session_id,
            origin_repo=decision.repo,
            origin_cwd=cwd,
            target_flow="flow-1-self-rising",
            target_worktree=cwd,
            target_kanban_task_id=task_id,
            origin_metadata_path=origin_path,
            parent_thread_path=source_thread_path,
        )
        prompt = add_dispatch_prep_to_prompt(prompt, dispatch_prep)
        dispatch_prep_fields = dispatch_prep_summary_fields(
            dispatch_prep,
            target_flow="flow-1-self-rising",
        )

        # in-progress 锁的 arm 已移交给目标 session 的 UserPromptSubmit hook
        # （rvf_user_prompt_submit.arm_kanban_followup_lock_on_delivery）：只有当注入的
        # follow-up trigger **真正以 prompt 落地**（投递被证明）时才上锁，而不再像旧设计
        # 那样在 dispatch 这一刻乐观地预先 arm。旧设计在投递前就 arm，一旦投递静默失败
        # （例如 /compact 在注入 turn 落地前重置了会话），就会留下一把纯 TTL 锁空转 6h、
        # 挡住后续自动 dispatch（squat）。读侧的 kanban_followup_in_progress_decision 与
        # handoff 清锁保持不变。
        # 残留权衡（按用户选择接受）：dispatch→delivery 在途窗口内若再触发一次 Stop，
        # 可能重复 dispatch 一条 follow-up——投递成功时该窗口为亚秒级；投递失败时本就需要重发。
        in_progress_marker_path: str | None = None
        ledger.event(
            phase="fork",
            event="kanban_followup_started",
            status="started",
            reason_code="kanban_followup_started",
            repo=decision.repo,
            cwd=cwd,
            mode="kanban-followup",
            cline_kanban_task_id=task_id,
            cline_kanban_attempt_id=attempt_id,
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            **source_origin_fields,
            **dispatch_prep_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
        )
        try:
            message_payload = start_cline_kanban_followup_message(
                project_path=project_path,
                task_id=task_id,
                attempt_id=attempt_id,
                prompt=prompt,
                ledger=ledger,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            # 不再在 dispatch 处 arm 锁，故注入失败时无锁可清（arm 已移交 UserPromptSubmit）。
            ledger.event(
                phase="fork",
                event="kanban_followup_failed",
                status="kanban-followup-unavailable",
                reason_code="kanban_followup_unavailable",
                repo=decision.repo,
                cwd=cwd,
                mode="kanban-followup",
                cline_kanban_task_id=task_id,
                cline_kanban_attempt_id=attempt_id,
                cline_kanban_task_title=source_origin.get("kanban_task_title"),
                cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                cline_kanban_task_lookup=task_lookup,
                **source_origin_fields,
                **dispatch_prep_fields,
                error=error,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )
            return ledger.hook_payload(
                status="kanban-followup-unavailable",
                reason_code="kanban_followup_unavailable",
                message=f"Cline Kanban follow-up user message was not injected: {error}",
                repo=decision.repo,
                cwd=cwd,
                backend=decision.backend,
                cline_kanban_task_id=task_id,
                cline_kanban_attempt_id=attempt_id,
                cline_kanban_project_path=project_path,
                cline_kanban_task_title=source_origin.get("kanban_task_title"),
                cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                cline_kanban_task_lookup=task_lookup,
                error=error,
                **source_origin_fields,
                **dispatch_prep_fields,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )

        # 旧设计在此把 message_id/turn_id 回写进 in-progress 锁；现在 arm 由目标 session
        # 的 UserPromptSubmit hook 在投递落地时完成，这里不再写锁。
        raw_status = str(message_payload.get("status") or "").strip().lower()
        message_id = message_payload.get("message_id")
        delivery_channel = _kanban_followup_delivery_channel(message_id)
        delivery_confirmed = delivery_channel == "app-server"
        if raw_status in {"started", "running", "in_progress", "in-progress"}:
            # app-server（可确认）→ started；terminal fallback（未确认）→ dispatched-unconfirmed，
            # 诚实表达「已交付但未确认成为真实 turn」，不再谎报 injected。
            status = (
                "kanban-followup-started"
                if delivery_confirmed
                else "kanban-followup-dispatched-unconfirmed"
            )
        else:
            status = "kanban-followup-enqueued"
        reason_code = status.replace("-", "_")
        # 未确认投递（terminal / 非 started 回执）写 pending marker，作为「dispatch 已发、尚未
        # 确认落地」的对账依据：UPS arm 落地时按 token 清掉它；超时仍在 → 下次 Stop 判定上次
        # 静默丢投并放行重投，同时 active 期间为 dispatch→delivery 在途窗口提供去重保护。
        pending_marker_path: str | None = None
        if not delivery_confirmed:
            pending_task_id = message_payload.get("task_id") or task_id
            try:
                pending_path = write_kanban_followup_pending(
                    task_id=pending_task_id,
                    session_id=source_session_id,
                    run_id=str(ledger.run_id),
                    run_dir=str(ledger.run_dir),
                    repo=decision.repo,
                    cwd=cwd,
                    token=dispatch_prep.token,
                    delivery_channel=delivery_channel,
                    attempt_id=message_payload.get("attempt_id") or attempt_id,
                    message_id=message_id if isinstance(message_id, str) else None,
                    turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
                    prompt_path=message_payload.get("prompt_path"),
                    # S0：快照 deep-link 与可选 verify-consumed 所需字段，供 S1a/S1b 复用。
                    kanban_project_path=project_path,
                    kanban_task_title=source_origin.get("kanban_task_title"),
                    kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                    origin_transcript_path=source_origin.get("transcript_path"),
                )
                pending_marker_path = str(pending_path) if pending_path else None
            except Exception:  # best-effort：写 pending 失败不阻断 dispatch 上报
                pending_marker_path = None
            # S1a：首次未确认派发即发一条强可见 OS 通知（hook systemMessage 易被忽略），
            # 并盖 last_notified_at 防随后任意会话 Stop 的 stranded-sweep 立刻重复通知。
            # 整段 best-effort，永不抛——通知失败不影响 dispatch 上报。
            if pending_marker_path:
                try:
                    stranded_task_url = resolve_kanban_task_url(project_path, pending_task_id)
                    stranded_notify = notify_kanban_followup_stranded(
                        task_id=pending_task_id,
                        task_title=source_origin.get("kanban_task_title"),
                        task_url=stranded_task_url,
                        reason="dispatched-unconfirmed",
                    )
                    stamp_kanban_followup_pending_notified(
                        task_id=pending_task_id,
                        token=dispatch_prep.token,
                    )
                    ledger.event(
                        phase="fork",
                        event="kanban_followup_stranded_notified",
                        status=status,
                        reason_code="kanban_followup_dispatched_unconfirmed_notified",
                        repo=decision.repo,
                        cwd=cwd,
                        mode="kanban-followup",
                        cline_kanban_task_id=pending_task_id,
                        cline_kanban_task_title=source_origin.get("kanban_task_title"),
                        kanban_followup_pending_marker_path=pending_marker_path,
                        kanban_followup_task_url=stranded_task_url,
                        kanban_followup_stranded_notified=bool(stranded_notify.get("notified")),
                        kanban_followup_stranded_notify_reason=stranded_notify.get("reason"),
                    )
                except Exception:
                    pass
        paths = {
            key: value
            for key, value in {
                "prompt": message_payload.get("prompt_path"),
                "message_command": message_payload.get("command_artifact_path"),
                "dispatch_prep_file": str(dispatch_prep.path),
            }.items()
            if value
        }
        ledger.event(
            phase="fork",
            event="kanban_followup_completed",
            status=status,
            reason_code=reason_code,
            repo=decision.repo,
            cwd=cwd,
            paths=paths,
            mode="kanban-followup",
            cline_kanban_task_id=message_payload.get("task_id"),
            cline_kanban_attempt_id=message_payload.get("attempt_id"),
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            cline_kanban_message_id=message_payload.get("message_id"),
            cline_kanban_turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
            cline_kanban_checkpoint_id=message_payload.get("checkpoint_id") or message_payload.get("checkpointId"),
            **source_origin_fields,
            **dispatch_prep_fields,
            kanban_followup_in_progress_marker_path=in_progress_marker_path,
            kanban_followup_delivery_channel=delivery_channel,
            kanban_followup_delivery_confirmed=delivery_confirmed,
            kanban_followup_pending_marker_path=pending_marker_path,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
        )
        return ledger.hook_payload(
            status=status,
            reason_code=reason_code,
            message=(
                "Cline Kanban follow-up user message was injected."
                if delivery_confirmed
                else (
                    "Cline Kanban follow-up 已经 terminal fallback 交给 Kanban，但未确认成为真实 "
                    "turn（dispatch 时无 app-server socket / 目标 session 可能已停止）；"
                    "请打开或恢复该 task，让排队中的 $review-validate-fix 被消费。"
                    "若未落地，下一次该 task 的 Stop 会判定丢投并自动重投。"
                )
            ),
            repo=decision.repo,
            cwd=cwd,
            backend=decision.backend,
            mode="kanban-followup",
            prompt_path=message_payload.get("prompt_path"),
            cline_kanban_task_id=message_payload.get("task_id"),
            cline_kanban_attempt_id=message_payload.get("attempt_id"),
            cline_kanban_project_path=project_path,
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            cline_kanban_message_id=message_payload.get("message_id"),
            cline_kanban_turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
            cline_kanban_checkpoint_id=message_payload.get("checkpoint_id") or message_payload.get("checkpointId"),
            kanban_followup_payload=message_payload,
            kanban_followup_in_progress_marker_path=in_progress_marker_path,
            kanban_followup_delivery_channel=delivery_channel,
            kanban_followup_delivery_confirmed=delivery_confirmed,
            kanban_followup_pending_marker_path=pending_marker_path,
            **source_origin_fields,
            **dispatch_prep_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
            **{
                key: value
                for key, value in (decision.summary_fields or {}).items()
                if key != "rvf_state" and not key.startswith("rvf_")
            },
        )
    if not decision.parent_thread_id:
        return skip_payload(
            "Stop event did not expose a parent thread id.",
            ledger,
            "missing_parent_thread_id",
            repo=decision.repo,
            cwd=decision.cwd,
            backend=decision.backend,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend=decision.backend,
                backend_raw=decision.backend,
            ),
        )

    prompt = fork_review_validate_fix_prompt(decision.parent_thread_id, cwd, decision.repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=decision.parent_thread_id,
        cwd=cwd,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=decision.parent_thread_path,
        fallback_failure_reason=fork_failure_report(decision.repo),
        ledger=ledger,
        launch_mode=launch_mode_for_backend(decision.backend),
        extra_summary={
            "backend": decision.backend,
            **(decision.summary_fields or {}),
        },
        origin_repo=decision.repo,
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


class _StopContextError(Exception):
    """Raised by `resolve_stop_context` when the Stop event provided a session
    transcript path but it isn't readable. The orchestrator unwraps the
    `skip_payload` attribute and short-circuits the gate."""

    def __init__(self, skip_payload_value: dict[str, Any]) -> None:
        super().__init__("stop context unresolved")
        self.skip_payload = skip_payload_value


@dataclass
class SessionScopePrecheck:
    checked: bool = False
    context: dict[str, Any] | None = None
    skip_payload: dict[str, Any] | None = None
    route_paths: list[str] | None = None


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def session_change_type_from_manifest(manifest: dict[str, Any] | None) -> str | None:
    if not isinstance(manifest, dict):
        return None
    owned_paths = _string_list(manifest.get("owned_paths"))
    owned_dirty_paths = _string_list(manifest.get("owned_dirty_paths"))
    if not owned_paths:
        return "no_codebase_changes"
    if not owned_dirty_paths:
        return "no_dirty_codebase_changes"
    return "dirty_codebase_changes"


def resolve_stop_context(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    """Pull the structured fields the rest of the gate flow needs out of the
    Stop event. Raises `_StopContextError` when a session transcript path was
    provided but unreadable — caller maps that to a `transcript_unavailable`
    skip payload."""
    session_paths = event_session_scope_paths(event)
    transcript: Path | None = None
    if session_paths:
        transcript = first_readable_session_path(event)
        if transcript is None:
            ledger.event(
                phase="gate",
                event="session_scope_unavailable",
                status="skipped",
                reason_code="transcript_unavailable",
                repo=repo,
                cwd=event.get("cwd"),
                paths={"transcripts": [str(path) for path in session_paths]},
            )
            raise _StopContextError(
                skip_payload(
                    "session transcript path was provided but is not readable; skipped RVF fork/review.",
                    ledger,
                    "transcript_unavailable",
                    repo=repo,
                )
            )
    return {
        "event": event,
        "repo": repo,
        "cwd": event.get("cwd"),
        "session_id": session_hook_id_from_event(event),
        "parent_session_id": parent_thread_id_from_event(event),
        "session_paths": session_paths,
        "transcript": transcript,
        "latest_user": latest_user_message_from_event(event),
        "session_hook_control": parse_session_hook_control(latest_user_message_from_event(event)),
    }


def refresh_global_diff_tracker(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    """Emit the Slice 3 shape-compliance ledger event and seed session-unit
    attribution from the transcript via `build_manifest`. The manifest helper
    transitively calls `reviewable_unit_diff_tracker.register_claims`, which writes
    `session_units` rows for transcript-attributed owned paths. The allocator
    then reads those rows directly without re-deriving them.

    Stays a light step in spirit: nothing here touches the SQLite store
    directly (D10) — observation is still consolidated inside the allocator's
    BEGIN IMMEDIATE transaction. The `build_manifest` invocation pre-populates
    `session_units` for transcript-aware sessions; manual CLI invocations (no
    transcript) bypass this entirely and rely on the allocator's
    `auto_claim_observed=True` fallback."""
    repo = context.get("repo")
    transcript = context.get("transcript")
    ledger.event(
        phase="gate",
        event="tracker_refresh_started",
        status="in_progress",
        reason_code="tracker_refresh_started",
        repo=repo,
        cwd=context.get("cwd"),
        session_id=context.get("session_id"),
    )
    if not isinstance(repo, str) or not repo or transcript is None:
        # 没 transcript / 没 repo：合法的"无 seeding"分支，不算失败。调用方
        # 会继续走 allocator 的 auto-claim fallback 或返回 None。
        return {"observed": False, "manifest": None}
    # 本轮 committed-change 下界：优先「最近一次完成 review 的高水位」（review_highwater_marker，
    # 仅当为 HEAD 祖先时），无高水位才回退 UserPromptSubmit 写的 round-baseline marker——与
    # detection 端 `maybe_route_committed_round_scope` 走 **同一个** `_resolve_committed_round_baseline`，
    # 否则 detection 用高水位 C1 选中孤儿、而这里 refresh 仍用被 prompt 顶到 HEAD 的 round-baseline
    # C2 → 窗口空 → 无 committed 观测 → 误判 no_session_owned_dirty（detection 与 allocation 必须
    # 用同一下界）。无任一可用下界 → None ⇒ committed 观测整支静默关闭、与今日 dirty-only 一致。
    # best-effort，绝不阻断 refresh。
    committed_baseline: str | None = None
    try:
        event = context.get("event")
        if isinstance(event, dict):
            committed_baseline = _resolve_committed_round_baseline(
                Path(repo).expanduser().resolve(),
                current_kanban_task_id(event),
                session_hook_id_from_event(event),
            )
    except Exception:
        committed_baseline = None
    context["committed_baseline"] = committed_baseline
    try:
        manifest = build_manifest(
            Path(repo).expanduser().resolve(),
            transcript,
            committed_baseline=committed_baseline,
        )
    except Exception as exc:
        # build_manifest 失败时 emit `tracker_refresh_failed`（保持原 ledger
        # event，不双重 log），并把错误信息回传给调用方，让 orchestrator /
        # dispatcher 显式 emit `session_manifest_failed` skip_payload。
        # 历史上的 legacy_session_scope_gate_payload 在同样条件下也是 fail-loud
        # 跳过 fork；新版 4-function split 必须保留这个语义，否则 manifest
        # 失败会被 allocator 的空 session_units 静默降级为 "no scope" 误判。
        error_message = f"{type(exc).__name__}: {exc}"
        ledger.event(
            phase="gate",
            event="tracker_refresh_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=repo,
            cwd=context.get("cwd"),
            error=error_message,
        )
        return {"observed": False, "manifest": None, "error": error_message}
    context["manifest"] = manifest
    context["session_change_type"] = session_change_type_from_manifest(manifest)
    return {"observed": True, "manifest": manifest}


def evaluate_session_gate(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Hand back a skip payload when the session-level gate already says
    "don't fork" (manual marker present, RVF_STOP_HOOK=disable). Returns None
    to continue with allocator-driven scope check."""
    if context.get("session_hook_control") == "disable":
        # When the user disabled the stop hook for this session via
        # `RVF_STOP_HOOK: disable`, suppress the auto fork. Note the dispatch
        # higher up in main() also catches this; this is a defense-in-depth
        # check so direct callers of the gate flow get the same answer.
        ledger.event(
            phase="gate",
            event="session_hook_disabled_via_marker",
            status="skipped",
            reason_code="session_hook_disabled",
            repo=context.get("repo"),
            cwd=context.get("cwd"),
            session_id=context.get("session_id"),
        )
        return skip_payload(
            "RVF_STOP_HOOK marker disabled the auto fork for this session.",
            ledger,
            "session_hook_disabled",
            repo=context.get("repo"),
            session_id=context.get("session_id"),
        )
    marker_payload = manual_rvf_session_marker_payload(context["event"], ledger)
    if marker_payload is not None:
        return marker_payload
    # DB scope_hash suppression is wired at allocator entry; candidate unit_ids
    # are not available at this layer.
    return None


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def auto_review_lease_ttl_seconds(context: dict[str, Any]) -> int | None:
    event = context.get("event")
    backend = normalize_backend_from_env(event if isinstance(event, dict) else None)
    if backend == "kanban-followup" and isinstance(event, dict) and current_kanban_task_id(event):
        return _positive_int_from_env(
            KANBAN_FOLLOWUP_LEASE_TTL_ENV,
            DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS,
        )
    return None


REASON_PATCH_OWNERSHIP_INCOMPLETE = "patch_ownership_incomplete"


def _patch_ownership_expected(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {"unit_ids": [], "paths": [], "unresolved": []}
    ownership = manifest.get("patch_ownership")
    if not isinstance(ownership, dict):
        return {"unit_ids": [], "paths": [], "unresolved": []}
    return {
        "unit_ids": _string_list(ownership.get("expected_apply_patch_unit_ids")),
        "paths": _string_list(ownership.get("expected_apply_patch_paths")),
        "unresolved": (
            [item for item in ownership.get("unresolved_owned_patch_hunks", []) if isinstance(item, dict)]
            if isinstance(ownership.get("unresolved_owned_patch_hunks"), list)
            else []
        ),
    }


def _allocated_unit_ids_from_result(result: dict[str, Any]) -> list[str]:
    scope = result.get("scope")
    if isinstance(scope, dict):
        return _string_list(scope.get("unit_ids"))
    return _string_list(result.get("unit_ids"))


def _allocated_paths_from_result(result: dict[str, Any]) -> list[str]:
    scope = result.get("scope")
    if isinstance(scope, dict):
        return _string_list(scope.get("paths"))
    return _string_list(result.get("paths"))


def patch_ownership_incomplete_details(
    manifest: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    expected = _patch_ownership_expected(manifest)
    expected_unit_ids = set(expected["unit_ids"])
    allocated_unit_ids = set(_allocated_unit_ids_from_result(result))
    missing_unit_ids = sorted(expected_unit_ids - allocated_unit_ids)
    expected_paths = set(expected["paths"])
    allocated_paths = set(_allocated_paths_from_result(result))
    missing_paths = sorted(expected_paths - allocated_paths)
    unresolved = expected["unresolved"]
    if not unresolved and not missing_unit_ids:
        return None
    return {
        "reason_code": REASON_PATCH_OWNERSHIP_INCOMPLETE,
        "unresolved_owned_patch_hunks": unresolved,
        "missing_apply_patch_unit_ids": missing_unit_ids,
        "missing_apply_patch_paths": missing_paths,
        "expected_apply_patch_unit_count": len(expected_unit_ids),
        "allocated_unit_count": len(allocated_unit_ids),
    }


def patch_ownership_incomplete_skip_payload(
    *,
    context: dict[str, Any],
    ledger: RunLedger,
    result: dict[str, Any],
    details: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    repo = context.get("repo")
    session_id = context.get("session_id")
    lease_id = result.get("lease_id")
    release_result: dict[str, Any] | None = None
    if isinstance(lease_id, str) and lease_id and isinstance(repo, str) and repo:
        try:
            release_result = lease_release(
                repo=repo,
                lease_id=lease_id,
                reason=REASON_PATCH_OWNERSHIP_INCOMPLETE,
            )
        except Exception as exc:
            release_result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    event_details = dict(details)
    event_details.pop("reason_code", None)
    ledger.event(
        phase="gate",
        event=REASON_PATCH_OWNERSHIP_INCOMPLETE,
        status="skipped",
        reason_code=REASON_PATCH_OWNERSHIP_INCOMPLETE,
        repo=repo,
        cwd=context.get("cwd"),
        session_id=session_id,
        dry_run=dry_run,
        tracker_lease_id=lease_id,
        tracker_scope_hash=result.get("scope_hash"),
        lease_release_result=release_result,
        **event_details,
    )
    if dry_run:
        return {
            "would_proceed": False,
            "candidate_unit_count": result.get("candidate_unit_count", 0),
            "result": result,
            "reason": REASON_PATCH_OWNERSHIP_INCOMPLETE,
            "patch_ownership": details,
        }
    return skip_payload(
        "patch ownership incomplete; skipped RVF fork/review so a partial tracker scope is not reported as clean.",
        ledger,
        REASON_PATCH_OWNERSHIP_INCOMPLETE,
        repo=repo,
        session_id=session_id,
        tracker_lease_id=lease_id,
        tracker_scope_hash=result.get("scope_hash"),
        patch_ownership=details,
    )


def sweep_stale_tracker_leases(
    *,
    repo_path: Path,
    context: dict[str, Any],
    ledger: RunLedger,
    dry_run: bool,
) -> list[dict[str, Any]]:
    try:
        released = sweep_stale(repo=repo_path)
    except Exception as exc:
        ledger.event(
            phase="gate",
            event="tracker_stale_sweep_failed",
            status="failed",
            reason_code="tracker_stale_sweep_failed",
            repo=str(repo_path),
            cwd=context.get("cwd"),
            session_id=context.get("session_id"),
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )
        return []
    ledger.event(
        phase="gate",
        event="tracker_stale_sweep_completed",
        status="completed",
        reason_code="tracker_stale_sweep_completed",
        repo=str(repo_path),
        cwd=context.get("cwd"),
        session_id=context.get("session_id"),
        dry_run=dry_run,
        released_count=len(released),
        released_lease_ids=[
            item.get("lease_id")
            for item in released
            if isinstance(item, dict) and isinstance(item.get("lease_id"), str)
        ],
    )
    return released


def allocate_auto_review_scope(
    context: dict[str, Any],
    ledger: RunLedger,
    *,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Producer-side gate. Tracker-disabled fallback delegates to the legacy
    manifest-based gate. Otherwise runs `allocate_review_scope` and converts
    the outcome into:
        * None       — allocator acquired a lease (or dry-run says one would
                       be acquired); the Stop hook continues to fork.
        * skip_payload — empty allocator scope or unrecoverable error.
        * dry_run dict — when `dry_run=True` the dispatcher gets the candidate
                         metadata without any tracker writes.
    """
    repo = context.get("repo")
    if _tracker_disabled():
        if dry_run:
            # The dispatcher dry-run path also predates the tracker — feed it
            # back through the manifest gate so disable-mode dispatchers see
            # exactly the same answer they did before Slice 3.
            return _dispatcher_dry_run_via_legacy(context, ledger)
        return legacy_session_scope_gate_payload(context["event"], repo, ledger)

    session_id = context.get("session_id")
    if not session_id:
        # Without a session id we can't meaningfully bind the allocator.
        # Fall back to the legacy gate so behavior matches Phase 0/1 for
        # transcript-less events.
        if dry_run:
            return _dispatcher_dry_run_via_legacy(context, ledger)
        return legacy_session_scope_gate_payload(context["event"], repo, ledger)

    repo_path = Path(repo).expanduser().resolve() if isinstance(repo, str) and repo else None
    if repo_path is None:
        return None

    event = context.get("event")
    backend = normalize_backend_from_env(event if isinstance(event, dict) else None)
    if backend == "kanban-followup" and (
        not isinstance(event, dict) or not current_kanban_task_id(event)
    ):
        ledger.event(
            phase="gate",
            event="kanban_followup_missing_task_id",
            status="skipped",
            reason_code="kanban_followup_missing_task_id",
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_missing_task_id",
            ),
        )
        if dry_run:
            return {
                "would_proceed": False,
                "candidate_unit_count": 0,
                "result": None,
                "reason": "kanban_followup_missing_task_id",
            }
        return skip_payload(
            "kanban-followup backend requires the current Cline Kanban task id.",
            ledger,
            "kanban_followup_missing_task_id",
            repo=repo,
            session_id=session_id,
            backend="kanban-followup",
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_missing_task_id",
            ),
        )

    run_id = ledger.run_id if hasattr(ledger, "run_id") else "stop-hook-run"
    reviewer_id = "stop-hook" if not dry_run else None
    parent_session_id = context.get("parent_session_id")
    if parent_session_id == session_id:
        parent_session_id = None
    lease_ttl_seconds = auto_review_lease_ttl_seconds(context)
    manifest = context.get("manifest")
    patch_ownership = manifest.get("patch_ownership") if isinstance(manifest, dict) else None
    transcript_max_line_number = (
        patch_ownership.get("transcript_max_line_number")
        if isinstance(patch_ownership, dict)
        and isinstance(patch_ownership.get("transcript_max_line_number"), int)
        else None
    )
    try:
        sweep_stale_tracker_leases(
            repo_path=repo_path,
            context=context,
            ledger=ledger,
            dry_run=dry_run,
        )
        manual_probe = _manual_suppression_scope_probe(
            repo=repo_path,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )
        scope_hash = manual_probe.get("scope_hash") if isinstance(manual_probe, dict) else None
        if isinstance(scope_hash, str) and scope_hash:
            manual_match = find_manual_rvf_run_for_scope_hash(
                repo=repo_path,
                scope_hash=scope_hash,
            )
            if manual_match is not None:
                ledger.event(
                    phase="gate",
                    event="manual_scope_hash_match",
                    status="skipped",
                    reason_code=REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    repo=repo,
                    cwd=context.get("cwd"),
                    session_id=session_id,
                    tracker_scope_hash=scope_hash,
                    manual_rvf_session_id=manual_match.get("session_id"),
                    manual_rvf_run_id=manual_match.get("run_id"),
                    manual_rvf_completed_at=manual_match.get("completed_at"),
                )
                if dry_run:
                    return {
                        "would_proceed": False,
                        "candidate_unit_count": manual_probe.get("candidate_unit_count", 0),
                        "result": manual_probe,
                        "reason": REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    }
                return skip_payload(
                    "manual RVF already completed for this tracker scope",
                    ledger,
                    REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    repo=repo,
                    session_id=session_id,
                    tracker_scope_hash=scope_hash,
                    manual_rvf_session_id=manual_match.get("session_id"),
                    manual_rvf_run_id=manual_match.get("run_id"),
                    manual_rvf_completed_at=manual_match.get("completed_at"),
                )
        if dry_run:
            result = allocate_review_scope(
                repo=repo_path,
                session_id=session_id,
                run_id=run_id,
                reviewer_id=reviewer_id,
                output_scope_path=None,
                parent_session_id=parent_session_id,
                holder_kind="reviewer",
                dry_run=True,
                auto_claim_observed=False,
                lease_ttl_seconds=lease_ttl_seconds,
                transcript_max_line_number=transcript_max_line_number,
                committed_baseline=context.get("committed_baseline"),
            )
            incomplete = patch_ownership_incomplete_details(manifest if isinstance(manifest, dict) else None, result)
            if incomplete is not None:
                return patch_ownership_incomplete_skip_payload(
                    context=context,
                    ledger=ledger,
                    result=result,
                    details=incomplete,
                    dry_run=True,
                )
            status = result.get("status")
            if status == "dry_run":
                return {
                    "would_proceed": bool(result.get("would_acquire")),
                    "candidate_unit_count": result.get("candidate_unit_count", 0),
                    "result": result,
                }
            if status == "empty":
                return {
                    "would_proceed": False,
                    "candidate_unit_count": result.get("candidate_unit_count", 0),
                    "result": result,
                }
        result = allocate_review_scope(
            repo=repo_path,
            session_id=session_id,
            run_id=run_id,
            reviewer_id=reviewer_id,
            output_scope_path=None,  # Stop-hook stamps via ledger.artifact below.
            parent_session_id=parent_session_id,
            holder_kind="reviewer",
            dry_run=dry_run,
            lease_ttl_seconds=lease_ttl_seconds,
            transcript_max_line_number=transcript_max_line_number,
            # Auto Stop-hook attribution comes from
            # `refresh_global_diff_tracker` → `build_manifest` →
            # `register_claims`; auto-claim here would broaden scope past
            # transcript intent.
            auto_claim_observed=False,
            committed_baseline=context.get("committed_baseline"),
        )
    except Exception as exc:
        ledger.event(
            phase="gate",
            event="allocate_review_scope_failed",
            status="failed",
            reason_code="allocator_error",
            repo=repo,
            cwd=context.get("cwd"),
            error=f"{type(exc).__name__}: {exc}",
        )
        if dry_run:
            return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
        return skip_payload(
            "allocator raised; skipped RVF fork/review.",
            ledger,
            "allocator_error",
            repo=repo,
            error=f"{type(exc).__name__}: {exc}",
        )

    status = result.get("status")
    incomplete = patch_ownership_incomplete_details(manifest if isinstance(manifest, dict) else None, result)
    if incomplete is not None and status in {"allocated", "dry_run", "empty"}:
        return patch_ownership_incomplete_skip_payload(
            context=context,
            ledger=ledger,
            result=result,
            details=incomplete,
            dry_run=dry_run,
        )
    if status == "allocated":
        scope_payload = result.get("scope")
        artifact_path = ledger.artifact("tracker-scope.json", scope_payload) if scope_payload else None
        # D12: stash the tracker scope meta on the ledger so subsequent hook
        # payload builders (Slice 6 fork-prompt splice) can pick it up. The
        # field is a convention; nothing in Slice 3 reads it back yet.
        if artifact_path is not None:
            try:
                meta = getattr(ledger, "tracker_scope_meta", None)
                if not isinstance(meta, dict):
                    meta = {}
                    setattr(ledger, "tracker_scope_meta", meta)
                meta["tracker_scope_path"] = artifact_path
                meta["tracker_lease_id"] = result.get("lease_id")
                meta["tracker_scope_hash"] = result.get("scope_hash")
                meta["tracker_dir"] = result.get("tracker_dir")
                meta["tracker_lease_ttl_seconds"] = lease_ttl_seconds
            except (AttributeError, TypeError):
                pass
        ledger.event(
            phase="gate",
            event="tracker_scope_allocated",
            status="allocated",
            reason_code=REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
            reason_code_legacy_alias=LEGACY_REASON_SESSION_OWNED_DIRTY,
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            tracker_scope_path=artifact_path,
            tracker_lease_id=result.get("lease_id"),
            tracker_scope_hash=result.get("scope_hash"),
            tracker_unit_count=len(result.get("scope", {}).get("unit_ids", []) if scope_payload else []),
            tracker_lease_ttl_seconds=lease_ttl_seconds,
        )
        if dry_run:
            return {
                "would_proceed": True,
                "candidate_unit_count": result.get("candidate_unit_count", 0),
                "result": result,
            }
        return None
    if status == "dry_run":
        return {
            "would_proceed": bool(result.get("would_acquire")),
            "candidate_unit_count": result.get("candidate_unit_count", 0),
            "result": result,
        }
    if status == "empty":
        manifest = context.get("manifest")
        session_change_type = context.get("session_change_type")
        session_owned_paths = _string_list(manifest.get("owned_paths")) if isinstance(manifest, dict) else []
        session_owned_dirty_paths = (
            _string_list(manifest.get("owned_dirty_paths")) if isinstance(manifest, dict) else []
        )
        ledger.event(
            phase="gate",
            event="session_scope_clean",
            status="skipped",
            reason_code=REASON_NO_UNASSIGNED_REVIEW_SCOPE,
            reason_code_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            candidate_unit_count=result.get("candidate_unit_count", 0),
            leased_excluded_count=result.get("leased_excluded_count", 0),
            session_change_type=session_change_type,
            session_owned_paths=session_owned_paths,
            session_owned_dirty_paths=session_owned_dirty_paths,
        )
        if dry_run:
            return {
                "would_proceed": False,
                "candidate_unit_count": result.get("candidate_unit_count", 0),
                "result": result,
                "session_change_type": session_change_type,
            }
        return skip_payload(
            "no unassigned review scope",
            ledger,
            REASON_NO_UNASSIGNED_REVIEW_SCOPE,
            # D4: keep legacy `reason=no_session_owned_dirty` substring in the
            # hook systemMessage for one release so dispatcher / installed-hook
            # downstream assertions don't all churn at once. The structured
            # `reason_code` field has already flipped to the new name.
            detail=f"reason={LEGACY_REASON_NO_SESSION_OWNED_DIRTY}",
            repo=repo,
            session_id=session_id,
            reason_code_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            candidate_unit_count=result.get("candidate_unit_count", 0),
            leased_excluded_count=result.get("leased_excluded_count", 0),
            session_change_type=session_change_type,
            session_owned_paths=session_owned_paths,
            session_owned_dirty_paths=session_owned_dirty_paths,
        )
    # Other statuses (lock_timeout / error / disabled / unsupported_repo) all
    # degrade gracefully: the allocator already emitted its own events.jsonl
    # marker, and we let the Stop hook continue (returning None) so callers
    # see Phase-0 behavior rather than a hard skip on transient lock issues.
    if dry_run:
        return {
            "would_proceed": False,
            "candidate_unit_count": result.get("candidate_unit_count", 0),
            "result": result,
        }
    return None


def _dispatcher_dry_run_via_legacy(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    """Dispatcher dry-run helper for tracker-disabled / session-id-less events.
    Builds the manifest exactly like the legacy gate and reports whether the
    legacy `owned_dirty_paths` set is non-empty."""
    repo = context.get("repo")
    transcript = context.get("transcript")
    if not isinstance(repo, str) or not repo or transcript is None:
        return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
    try:
        manifest = build_manifest(Path(repo).expanduser().resolve(), transcript)
    except Exception:
        return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
    owned_dirty = manifest.get("owned_dirty_paths")
    has_dirty = isinstance(owned_dirty, list) and bool(owned_dirty)
    return {
        "would_proceed": has_dirty,
        "candidate_unit_count": len(owned_dirty) if isinstance(owned_dirty, list) else 0,
        "result": {"status": "legacy", "manifest": manifest},
    }


def legacy_session_scope_gate_payload(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Phase-0/1 gate body, kept verbatim so `RVF_TRACKER_DISABLE=1`
    users see no behavior change. Reason-code literals stay
    `no_session_owned_dirty` / `session_owned_dirty` here on purpose."""
    session_paths = event_session_scope_paths(event)
    if not session_paths:
        return None

    transcript = first_readable_session_path(event)
    if transcript is None:
        ledger.event(
            phase="gate",
            event="session_scope_unavailable",
            status="skipped",
            reason_code="transcript_unavailable",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"transcripts": [str(path) for path in session_paths]},
        )
        return skip_payload(
            "session transcript path was provided but is not readable; skipped RVF fork/review.",
            ledger,
            "transcript_unavailable",
            repo=repo,
        )

    try:
        manifest = build_manifest(Path(repo).expanduser().resolve(), transcript)
    except Exception as exc:
        ledger.event(
            phase="gate",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"transcript": str(transcript)},
            error=f"{type(exc).__name__}: {exc}",
        )
        return skip_payload(
            "session manifest failed; skipped RVF fork/review.",
            ledger,
            "session_manifest_failed",
            repo=repo,
            error=f"{type(exc).__name__}: {exc}",
        )

    manifest_path = ledger.artifact("session-manifest.json", manifest)
    owned_dirty = manifest.get("owned_dirty_paths")
    if isinstance(owned_dirty, list) and owned_dirty:
        ledger.event(
            phase="gate",
            event="session_scope_detected",
            status="dirty",
            reason_code="session_owned_dirty",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"manifest": manifest_path} if manifest_path else {},
            owned_dirty_paths=owned_dirty,
        )
        return None

    ledger.event(
        phase="gate",
        event="session_scope_clean",
        status="skipped",
        reason_code="no_session_owned_dirty",
        repo=repo,
        cwd=event.get("cwd"),
        paths={"manifest": manifest_path} if manifest_path else {},
        unattributed_dirty_paths=manifest.get("unattributed_dirty_paths"),
    )
    return skip_payload(
        "no session-owned dirty paths",
        ledger,
        "no_session_owned_dirty",
        repo=repo,
        unattributed_dirty_paths=manifest.get("unattributed_dirty_paths"),
    )


def session_scope_gate_payload(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Thin orchestrator over the 4 split functions. Preserves the historical
    contract: returns None to continue, or a hook payload dict to skip."""
    if _tracker_disabled():
        return legacy_session_scope_gate_payload(event, repo, ledger)
    try:
        context = resolve_stop_context(event, repo, ledger)
    except _StopContextError as exc:
        return exc.skip_payload
    if not context.get("session_paths"):
        # Match legacy behavior: no transcript-derived paths means no gate.
        return None
    refresh_result = refresh_global_diff_tracker(context, ledger)
    refresh_error = refresh_result.get("error") if isinstance(refresh_result, dict) else None
    if refresh_error:
        # 与 legacy_session_scope_gate_payload 的 build_manifest 异常分支一致：
        # manifest 构建失败时返回 `session_manifest_failed` skip payload，让
        # Stop hook 显式跳过 fork（fail-loud），避免下游 allocator 看到空的
        # session_units 后被静默判为 `no_unassigned_review_scope` 干净跳过。
        return skip_payload(
            "session manifest failed; skipped RVF fork/review.",
            ledger,
            "session_manifest_failed",
            repo=context.get("repo"),
            session_id=context.get("session_id"),
            error=refresh_error,
        )
    gated = evaluate_session_gate(context, ledger)
    if gated is not None:
        return gated
    return allocate_auto_review_scope(context, ledger, dry_run=False)


def precheck_session_scope_for_dirty_route(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> SessionScopePrecheck:
    """Run the transcript-aware part of the session gate before whole-repo
    dirty-route shortcuts. This keeps background dirty files from deciding
    route type for a read-only chat session."""
    if not event_session_scope_paths(event):
        return SessionScopePrecheck()

    if _tracker_disabled():
        payload = legacy_session_scope_gate_payload(event, repo, ledger)
        return SessionScopePrecheck(checked=True, skip_payload=payload)

    try:
        context = resolve_stop_context(event, repo, ledger)
    except _StopContextError as exc:
        return SessionScopePrecheck(checked=True, skip_payload=exc.skip_payload)

    refresh_result = refresh_global_diff_tracker(context, ledger)
    refresh_error = refresh_result.get("error") if isinstance(refresh_result, dict) else None
    if refresh_error:
        return SessionScopePrecheck(
            checked=True,
            skip_payload=skip_payload(
                "session manifest failed; skipped RVF fork/review.",
                ledger,
                "session_manifest_failed",
                repo=context.get("repo"),
                session_id=context.get("session_id"),
                error=refresh_error,
            ),
        )

    gated = evaluate_session_gate(context, ledger)
    if gated is not None:
        return SessionScopePrecheck(checked=True, context=context, skip_payload=gated)

    manifest = context.get("manifest")
    route_paths = (
        _string_list(manifest.get("owned_dirty_paths"))
        if isinstance(manifest, dict)
        else None
    )
    return SessionScopePrecheck(checked=True, context=context, route_paths=route_paths)


def manual_rvf_session_marker_payload(
    event: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any] | None:
    session_id = session_hook_id_from_event(event)
    if not session_id:
        return None

    cwd = event.get("cwd")
    marker = read_manual_rvf_session_marker(session_id, cwd if isinstance(cwd, str) and cwd else None)
    if marker is None:
        return None

    run_id = marker[MANUAL_RVF_RUN_ID_KEY]
    completed_at = marker[MANUAL_RVF_COMPLETED_AT_KEY]
    return skip_payload(
        "当前 chat session 已完成手动 $review-validate-fix；"
        "installed Stop hook 跳过自动 RVF fork/review，"
        "但这不是 RVF_SUPPRESS_STOP_HOOK 全 hook suppress。"
        f"session_id={session_id}; manual_rvf_run_id={run_id}; "
        f"manual_rvf_completed_at={completed_at}",
        ledger,
        "manual_rvf_already_ran",
        session_id=session_id,
        manual_rvf_run_id=run_id,
        manual_rvf_completed_at=completed_at,
        manual_rvf_expires_at=marker.get("manual_rvf_expires_at"),
        manual_rvf_repo=marker.get("manual_rvf_repo"),
        manual_rvf_dirty_hash=marker.get("manual_rvf_dirty_hash"),
        manual_rvf_marker_path=marker.get("state_path"),
        **stop_hook_rvf_state_fields(
            phase="complete",
            backend="manual",
            backend_raw="manual",
            completion_gate="manual_rvf_already_ran",
        ),
    )


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

    if session_user_message_contains(event, SUPPRESS_STOP_HOOK_MARKER):
        return True

    task_id = current_kanban_task_id(event)
    if task_id:
        marker = read_kanban_task_suppression(task_id)
        if marker and marker.get("suppress_stop_hook") is True:
            return True

    if event.get("suppress_review_validate_fix") is True:
        return True
    if event.get("review_validate_fix_suppressed") is True:
        return True

    return False


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


def changed_paths_from_gate_output(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines()[1:]:
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if not path:
            continue
        if " -> " in path:
            old_path, new_path = path.rsplit(" -> ", 1)
            paths.extend([old_path.strip(), new_path.strip()])
            continue
        paths.append(path)
    return paths


def plan_doc_review_classification(paths: list[str]) -> dict[str, Any]:
    normalized = [path.replace("\\", "/") for path in paths if path]
    doc_paths = [
        path
        for path in normalized
        if path.startswith(PLAN_DOC_REVIEW_DIR_PREFIXES)
        and path.lower().endswith((".md", ".mdx", ".rst", ".txt"))
    ]
    plan_like_paths = [
        path
        for path in doc_paths
        if any(marker in Path(path).name.lower() for marker in PLAN_DOC_REVIEW_NAME_MARKERS)
    ]
    return {
        "changed_paths": normalized,
        "doc_paths": doc_paths,
        "plan_like_paths": plan_like_paths,
        "should_route": bool(normalized)
        and len(doc_paths) == len(normalized)
        and bool(plan_like_paths),
    }


def fork_failure_report(repo: str) -> str:
    return (
        "review-validate-fix Stop hook 未运行：无法创建 Codex GUI fork，"
        "且 Stop continuation prompt 已禁用，因为它不会创建真正的新用户 prompt，"
        "只会作为 hook system context 出现在当前轨迹中。"
        f" target_repo={repo}。请检查 Codex Desktop control socket / app-server fork 能力；"
        "修复前需要用户手动触发 $review-validate-fix。"
    )


def payload_decision(
    payload: dict[str, Any],
    *,
    reason_code: str,
    repo: str | None = None,
    cwd: str | None = None,
    backend: str = "off",
    status: str = "skipped",
) -> StopDecision:
    return StopDecision(
        action="emit",
        reason_code=reason_code,
        repo=repo,
        cwd=cwd,
        backend=backend,
        payload=payload,
        status=status,
    )


def skip_decision(
    message: str,
    ledger: RunLedger,
    reason_code: str,
    *,
    repo: str | None = None,
    cwd: str | None = None,
    backend: str = "off",
    **summary_fields: Any,
) -> StopDecision:
    payload_fields = dict(summary_fields)
    if repo is not None:
        payload_fields.setdefault("repo", repo)
    if cwd is not None:
        payload_fields.setdefault("cwd", cwd)
    if backend != "off":
        payload_fields.setdefault("backend", backend)
    payload = skip_payload(
        message,
        ledger,
        reason_code,
        **payload_fields,
    )
    return StopDecision(
        action="emit",
        reason_code=reason_code,
        repo=repo,
        cwd=cwd,
        backend=backend,
        message=message,
        summary_fields=payload_fields,
        payload=payload,
        status="skipped",
    )


def session_hook_control_decision(
    event: dict[str, Any],
    latest_user: str | None,
    ledger: RunLedger,
) -> StopDecision | None:
    session_control = session_hook_control_payload(event, latest_user)
    if session_control is None:
        return None

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
    if (
        session_control.get("control_action") == "on"
        and session_control_reason == "session_hook_gate_enabled"
    ):
        ledger.event(
            phase="gate",
            event="session_hook_control_continue",
            status="completed",
            reason_code=session_control_reason,
            session_id=session_hook_id_from_event(event),
            control_action=session_control.get("control_action"),
            session_hook_gate_state=session_control.get("session_hook_gate_state"),
            state_path=session_control.get("state_path"),
            message=(
                "RVF_STOP_HOOK:on re-enabled this session; continuing the same "
                "Stop event through the normal RVF gate."
            ),
        )
        return None
    ledger.summary(
        status="session-hook-control",
        reason_code=session_control_reason,
        message=session_control_message,
        session_id=session_hook_id_from_event(event),
        control_action=session_control.get("control_action"),
        session_hook_gate_state=session_control.get("session_hook_gate_state"),
        state_path=session_control.get("state_path"),
    )
    payload = ledger.hook_payload(
        status="session-hook-control",
        reason_code=session_control_reason,
        message=session_control_message,
        session_id=session_hook_id_from_event(event),
        control_action=session_control.get("control_action"),
        session_hook_gate_state=session_control.get("session_hook_gate_state"),
        state_path=session_control.get("state_path"),
    )
    return payload_decision(
        payload,
        reason_code=session_control_reason,
        status="session-hook-control",
    )


def report_only_decision(repo: str, ledger: RunLedger) -> StopDecision:
    report = fork_failure_report(repo)
    ledger.event(
        phase="fork",
        event="skipped",
        status="skipped",
        reason_code="continuation_disabled",
        repo=repo,
        message=report,
    )
    payload = ledger.hook_payload(
        status="skipped",
        reason_code="continuation_disabled",
        message=report,
        repo=repo,
        backend="report-only",
    )
    return payload_decision(
        payload,
        reason_code="continuation_disabled",
        repo=repo,
        backend="report-only",
    )


def route_reviewable_scope(
    event: dict[str, Any],
    repo: str,
    candidate_paths: list[str],
    ledger: RunLedger,
    cwd: str | None,
    *,
    source: str,
    gate_status: str,
) -> StopDecision:
    """gate 通过后的共享路由：session precheck → reopen marker → plan/doc 路由
    → allocate → backend 选择 → launch。

    被两个上游复用，唯一区别是 ``candidate_paths`` 的来源：
      * dirty 工作区（``source="dirty_working_tree"``，paths 来自 gate stdout 的
        ``git status`` 摘要）；
      * 本轮已提交但未审改动（``source="committed_round"``，paths 来自
        ``baseline..HEAD`` first-parent 净 diff）。
    两条路径共用同一 precheck/allocate 管线：``precheck_session_scope_for_dirty_route``
    与回退的 ``session_scope_gate_payload`` 都会调 ``refresh_global_diff_tracker``，
    后者自解析 round-baseline marker 把 committed 单元喂进 manifest，因此 committed
    改动与 dirty 改动的归属 / 分配 / 去重完全一致。
    """
    all_changed_paths = candidate_paths
    session_precheck = precheck_session_scope_for_dirty_route(
        event,
        repo,
        ledger,
    )
    if session_precheck.skip_payload is not None:
        return payload_decision(
            session_precheck.skip_payload,
            reason_code="session_scope_skipped",
            repo=repo,
            cwd=cwd,
        )

    # 失败再入：若上游 agent 经 $rvf-reopen 武装了 rescope marker，在 allocate /
    # kanban-followup launch 之前按 target_run_id 把那次实现仍存在的 reviewed
    # units 翻回 available（run-scoped），使本轮 RVF 全量重审「实现 ∪ fix」。
    # precheck 已做 tracker refresh；此处翻转的 DB 状态会被紧接着的本进程
    # allocate 或 kanban-followup 后台 run 的 refresh+allocate 一并 reconcile。
    consume_review_reopen_marker(event, repo, ledger, cwd=cwd)

    route_candidate_paths = (
        session_precheck.route_paths
        if session_precheck.route_paths is not None
        else all_changed_paths
    )
    doc_review = plan_doc_review_classification(route_candidate_paths)
    if doc_review["should_route"]:
        ledger.event(
            phase="gate",
            event="plan_doc_review_routed",
            status="skipped",
            reason_code="plan_document_only",
            repo=repo,
            cwd=cwd,
            changed_paths=doc_review["changed_paths"],
            plan_like_paths=doc_review["plan_like_paths"],
            route="plan-doc-maintainer-review",
        )
        return skip_decision(
            "plan/document-only dirty scope should route to Plan/Doc Maintainer Review, "
            "not full review-validate-fix.",
            ledger,
            "plan_document_only",
            repo=repo,
            cwd=cwd,
            route="plan-doc-maintainer-review",
            changed_paths=doc_review["changed_paths"],
            doc_paths=doc_review["doc_paths"],
            plan_like_paths=doc_review["plan_like_paths"],
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="plan-doc-review",
                backend_raw="plan-doc-review",
                completion_gate="plan_document_only",
            ),
        )
    if session_precheck.context is not None:
        session_scope_payload = allocate_auto_review_scope(
            session_precheck.context,
            ledger,
            dry_run=False,
        )
    elif session_precheck.checked:
        session_scope_payload = None
    else:
        session_scope_payload = session_scope_gate_payload(event, repo, ledger)
    if session_scope_payload is not None:
        return payload_decision(
            session_scope_payload,
            reason_code="session_scope_skipped",
            repo=repo,
            cwd=cwd,
        )

    backend = normalize_backend_from_env(event)
    backend_selection_mode = fork_mode_selection_from_env()
    if backend == "off":
        return skip_decision(
            "CODEX_RVF_MODE=off",
            ledger,
            "mode_off",
            repo=repo,
            cwd=cwd,
            backend=backend,
        )
    if backend == "report-only":
        return report_only_decision(repo, ledger)

    parent_thread_id = parent_thread_id_from_event(event)
    parent_thread_path = parent_thread_path_from_event(event)
    if backend != "kanban-followup" and not parent_thread_id:
        return skip_decision(
            "Stop event did not expose a parent thread id.",
            ledger,
            "missing_parent_thread_id",
            repo=repo,
            cwd=cwd,
            backend=backend,
            log_prefix="review-validate-fix-fork",
        )
    if backend == "kanban":
        parent_thread_path = first_readable_session_path(event)
        if parent_thread_path is None and backend_selection_mode != "auto":
            return skip_decision(
                "Cline Kanban backend requires a readable parent transcript/session "
                "scope anchor; skipped to avoid starting with an empty session-owned "
                "worktree bootstrap.",
                ledger,
                "cline_kanban_missing_scope_anchor",
                repo=repo,
                cwd=cwd,
                backend=backend,
            )

    return StopDecision(
        action="launch",
        reason_code="backend_selected",
        repo=repo,
        cwd=fork_cwd_for_event(event, repo),
        parent_thread_id=parent_thread_id,
        parent_thread_path=parent_thread_path,
        backend=backend,
        message="RVF backend selected.",
        summary_fields={
            "gate_status": gate_status,
            "review_scope_source": source,
            "backend_selection_mode": backend_selection_mode,
            "legacy_gui_fallback_role": "backup-of-backup"
            if backend_selection_mode == "auto"
            else None,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend=backend,
                backend_raw=backend,
            ),
        },
        status="started",
    )


def _commit_is_ancestor_of_head(repo_path: Path, commit: str) -> bool:
    """``commit`` 是否为当前 HEAD 的祖先（含等于 HEAD）。best-effort：任何失败返回 False。

    high-water 仅在通过本判定时才被 committed-round 采纳为窗口下界——分支被 reset/rebase
    到 high-water 之前时，旧 high-water 不再是 HEAD 祖先，``baseline..HEAD`` 会退化，故视为
    失效、回退 round-baseline。
    """
    if not commit:
        return False
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _resolve_committed_round_baseline(
    repo_path: Path,
    task_id: str | None,
    session_id: str | None,
) -> str | None:
    """committed-round 窗口下界：优先「最近一次完成 review 覆盖到的 HEAD」高水位
    （``review_highwater_marker``，仅当其为当前 HEAD 祖先时采用），仅当某 task/session
    从未完成过 review（无高水位）时才回退 round-baseline（上一条 prompt 的 HEAD）作 bootstrap。

    这样『在某轮提交、却没在该轮自己的 Stop 里被审』的孤儿提交不会因后续任意 prompt 推进
    round-baseline 而落出窗口——它们留在 ``high-water..HEAD`` 内直到真被审，修掉
    "since last user prompt" 语义下的第三面漏审盲区（见 ``review_highwater_marker`` 模块头）。
    best-effort：高水位读取异常时静默回退 round-baseline。
    """
    try:
        highwater = review_highwater_marker.resolve_review_highwater_head(
            task_id=task_id,
            session_id=session_id,
        )
    except Exception:  # pragma: no cover - 高水位读取永不阻断
        highwater = None
    if highwater and _commit_is_ancestor_of_head(repo_path, highwater):
        return highwater
    return resolve_round_baseline_head(task_id=task_id, session_id=session_id)


def maybe_route_committed_round_scope(
    event: dict[str, Any],
    repo: str | None,
    ledger: RunLedger,
    cwd: str | None,
) -> StopDecision | None:
    """工作区 clean 时，把「本轮已提交但未审」的改动接回 review 管线。

    这是 commit ``9127824`` 缺失的前置 gate：committed 检测的全部下游
    （``refresh_global_diff_tracker`` / ``build_manifest`` / ``allocate_auto_review_scope``）
    本就 committed-aware，却被 dirty-only gate 挡在 ``if status == "DIRTY"`` 之外，
    导致「agent commit 掉本轮工作 → 工作区变 clean」这一主目标场景永远走不到。

    行为零回归：缺 round-baseline marker（None）/ baseline..HEAD 无本轮提交（空）
    → 返回 None，调用方维持今日的 ``clean_repo`` skip。仅当确有本轮 committed 改动时
    才经 ``route_reviewable_scope`` 进入与 dirty 完全一致的 precheck/allocate/launch。
    """
    if not repo:
        return None
    repo_path = Path(repo).expanduser().resolve()
    baseline = _resolve_committed_round_baseline(
        repo_path,
        current_kanban_task_id(event),
        session_hook_id_from_event(event),
    )
    if not baseline:
        return None
    # Per-commit opt-out audit: surface which round commits the `RVF-Skip-Review`
    # trailer excluded BEFORE the empty-set early return, so even a round whose
    # commits are all opted-out (committed_paths == []) leaves an audit trace
    # instead of looking indistinguishable from "no committed work".
    try:
        skip_review_shas = sorted(_list_round_skip_review_commit_shas(repo_path, baseline))
    except Exception:
        skip_review_shas = []
    if skip_review_shas:
        ledger.event(
            phase="gate",
            event="committed_round_skip_excluded",
            status="committed",
            reason_code="rvf_skip_review_trailer",
            repo=repo,
            cwd=cwd,
            committed_baseline=baseline,
            skipped_commit_shas=skip_review_shas,
            skipped_commit_count=len(skip_review_shas),
        )
    try:
        committed_paths = _list_committed_round_changed_paths(
            repo_path,
            baseline,
        )
    except Exception:
        committed_paths = []
    if not committed_paths:
        return None
    ledger.event(
        phase="gate",
        event="committed_round_route_selected",
        status="committed",
        reason_code="committed_round_changes",
        repo=repo,
        cwd=cwd,
        committed_baseline=baseline,
        committed_path_count=len(committed_paths),
    )
    return route_reviewable_scope(
        event,
        repo,
        committed_paths,
        ledger,
        cwd,
        source="committed_round",
        gate_status="CLEAN",
    )


RVF_ANALYZE_MANUAL_SKILL = "rvf-analyze"


def _rvf_analyze_manually_invoked(event: dict[str, Any], latest_user: str | None) -> bool:
    """本 turn 用户是否**显式调用了** ``$rvf-analyze``（只读复盘 skill）？

    turn-scoped：只看本 turn 的 invoked-skill / 最新用户消息——下一个真实改动的
    user turn 天然不命中，故只抑制 analyze 自身那一次 Stop，绝不波及后续轮。这取代
    了已退役的 task-keyed + 6h ``post_analyze_quiet`` marker（旧 marker 会顺带把父
    会话后续真实改动也误抑制）。自动 / detached analyze 线程的再入由其注入的
    ``RVF_ANALYZE_THREAD`` env 守卫（本函数上方早退）覆盖，不依赖本检测。

    两路检测（任一命中即真），均复用 vendored ``codex_invoked_skill``，避免再造第三套
    anchored-regex：
    1. 结构化（Codex）：``was_skill_invoked`` 读 rollout 的显式 ``$skill`` 调用，
       命中含命名空间的 ``$rvf:rvf-analyze``——authoritative，无 handoff 正文误判。
    2. 文本兜底（Claude / 无结构化读）：``match_invocation_in_text`` 对最新用户消息做
       行首/空白锚定的 ``$``/``/``/``:`` + skill 名匹配（带词边界，避免误吞 prose /
       packet 里的字面量）。即便偶发误判，后果也仅是本 turn 跳过一次自动 RVF，下一轮
       自愈。
    best-effort：结构化 / 文本读取异常绝不阻断，按未命中处理。
    """
    if codex_invoked_skill is None:
        return False
    try:
        if codex_invoked_skill.was_skill_invoked(event, RVF_ANALYZE_MANUAL_SKILL):
            return True
        if latest_user and codex_invoked_skill.match_invocation_in_text(
            latest_user, (RVF_ANALYZE_MANUAL_SKILL,)
        ):
            return True
    except Exception:  # pragma: no cover - 结构化/文本读取永不阻断
        return False
    return False


def evaluate_stop_event(event: dict[str, Any], ledger: RunLedger) -> StopDecision:
    latest_user = latest_user_message_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None

    if event.get("stop_hook_active") is True:
        return skip_decision(
            "检测到 stop_hook_active=true，为避免递归已跳过。",
            ledger,
            "stop_hook_active",
            cwd=cwd,
            detail="Codex 已在执行 Stop hook，RVF 跳过以避免递归",
        )

    if is_truthy(os.environ.get(RVF_ANALYZE_THREAD)):
        return skip_decision(
            "检测到 RVF_ANALYZE_THREAD：当前 Stop event 来自 detached "
            "$rvf-analyze 后台线程自身，短路所有 gate 跳过，避免后台 analyze "
            "递归触发新一轮 RVF。",
            ledger,
            "rvf_analyze_thread_self_stop",
            cwd=cwd,
        )

    codex_goal_mode = codex_goal_mode_context_from_event(event)
    if codex_goal_mode is not None:
        return skip_decision(
            "检测到 Codex 主会话处于 /goal mode；临时跳过 RVF Stop hook。"
            "后续会单独支持 /goal mode 下的 RVF 调度。",
            ledger,
            "codex_goal_mode",
            cwd=cwd,
            detail="检测到 Codex /goal mode，临时跳过 RVF Stop hook。",
            **codex_goal_mode,
        )

    handoff_path_value = handoff_path_from_event(event)
    if handoff_path_value is not None:
        payload = handoff_completion_payload(event, ledger, cwd=cwd)
        if payload is not None:
            try:
                finalize_record = finalize_for_handoff(
                    handoff_path=handoff_path_value,
                    event=event,
                    decision_kind="handoff-advisory",
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
            clear_kanban_followup_lock_for_event(
                event,
                ledger,
                cwd=cwd,
                handoff_path=str(handoff_path_value),
            )
            return payload_decision(payload, reason_code="handoff_file_ready", cwd=cwd)

    in_progress_decision = kanban_followup_in_progress_decision(event, ledger, cwd=cwd)
    if in_progress_decision is not None:
        return in_progress_decision

    if latest_user and RVF_ANALYZE_FOLLOWUP_MARKER in latest_user:
        return skip_decision(
            "当前最新用户消息是 Cline Kanban 注入的 RVF analyze follow-up trigger；"
            "本次 Stop 跳过自动 RVF，避免复盘消息结束后递归触发主 review loop。",
            ledger,
            "rvf_analyze_followup_trigger_turn",
            cwd=cwd,
        )

    if _rvf_analyze_manually_invoked(event, latest_user):
        return skip_decision(
            "本轮用户显式调用了 $rvf-analyze（只读复盘 skill）；本次 Stop 一次性跳过自动 "
            "RVF dispatch，避免把复盘那一轮误当成新改动触发 review。仅作用于本 turn——"
            "下一轮真实改动的 Stop 会正常触发 RVF。",
            ledger,
            "rvf_analyze_manual_turn",
            cwd=cwd,
        )

    if latest_user and KANBAN_FOLLOWUP_MARKER in latest_user:
        return skip_decision(
            "当前最新用户消息是 Cline Kanban 注入的 RVF follow-up trigger；"
            "本次 Stop 跳过自动 RVF，避免同一 synthetic user turn 结束后递归触发。",
            ledger,
            "kanban_followup_trigger_turn",
            cwd=cwd,
            backend="kanban-followup",
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_trigger_turn",
            ),
        )

    fork_context = rvf_fork_context(latest_user) or rvf_fork_context_from_event(event)
    if fork_context is not None:
        return skip_decision(
            "当前会话已是 review-validate-fix fork，会等待最终 RVF_HANDOFF_FILE，不会再次 fork。",
            ledger,
            "already_rvf_fork",
            cwd=cwd,
        )

    if event_marks_subagent(event):
        return skip_decision(
            "Stop event 来自 Codex subagent，post-work review 只允许主会话触发。",
            ledger,
            "subagent_stop_event",
            cwd=cwd,
        )

    session_control = session_hook_control_decision(event, latest_user, ledger)
    if session_control is not None:
        return session_control

    manual_marker_payload = manual_rvf_session_marker_payload(event, ledger)
    if manual_marker_payload is not None:
        return payload_decision(
            manual_marker_payload,
            reason_code="manual_rvf_already_ran",
            cwd=cwd,
        )

    session_id = session_hook_id_from_event(event)
    if session_id and session_hook_disabled(session_id):
        return skip_decision(
            "当前 chat session 已禁用 RVF_STOP_HOOK；"
            "只跳过 RVF fork/continuation/review gate，"
            f"不控制 dispatcher 的 dev sync。session_id={session_id}",
            ledger,
            "session_hook_disabled",
            cwd=cwd,
            session_id=session_id,
        )

    if should_suppress(event, latest_user):
        return skip_decision("检测到 suppress 标记或环境变量。", ledger, "suppressed", cwd=cwd)

    cwd_result: GateResult | None = None
    if cwd:
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
            return route_reviewable_scope(
                event,
                cwd_result.repo,
                changed_paths_from_gate_output(cwd_result.output),
                ledger,
                cwd,
                source="dirty_working_tree",
                gate_status=cwd_result.status,
            )
        if cwd_result.status == "CLEAN":
            # gate 只看 dirty 工作区；工作区 clean 时再看本轮有没有「已提交但未审」
            # 的改动（commit 9127824 的下游 committed 检测就是为这一幕而做，却一直被
            # 这个 dirty-only gate 挡在外面）。有则照常进 review，没有才 clean skip。
            committed_decision = maybe_route_committed_round_scope(
                event,
                cwd_result.repo or cwd,
                ledger,
                cwd,
            )
            if committed_decision is not None:
                return committed_decision
            return skip_decision(
                f"当前 cwd 仓库是 clean。repo={cwd_result.repo or cwd}",
                ledger,
                "clean_repo",
                repo=cwd_result.repo or cwd,
                cwd=cwd,
            )

    if cwd_result is not None:
        return skip_decision(
            "当前 cwd 不在 git repo/worktree 内，未自动选择目标仓库。"
            f"cwd gate={cwd_result.status}; cwd={cwd}。"
            "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
            ledger,
            "cwd_not_git_repo",
            cwd=cwd,
            gate_status=cwd_result.status,
        )

    return skip_decision(
        "Stop event 未提供可检查的 cwd，未自动选择目标仓库。"
        "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
        ledger,
        "missing_cwd",
    )


def start_stop_hook_ledger(event: dict[str, Any]) -> RunLedger:
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
    return ledger


def suppressed_decision(event: dict[str, Any], ledger: RunLedger) -> StopDecision:
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    message = "检测到 suppress 标记或环境变量。"
    ledger.event(
        phase="gate",
        event="suppressed",
        status="skipped",
        reason_code="suppressed",
        cwd=cwd,
        session_id=session_id_from_event(event),
        message=message,
    )
    return skip_decision(
        message,
        ledger,
        "suppressed",
        cwd=cwd,
        detail="检测到 suppress 标记或环境变量，已跳过 RVF Stop hook。",
    )


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    latest_user = latest_user_message_from_event(event)
    ledger = start_stop_hook_ledger(event)
    # S1b：与 backend 无关的跨 task stranded-pending 扫荡，必须早于 suppress/launch 分支——
    # 即便本次 Stop 自身被 suppress 或不 launch，也要让它把**别的** task 遗留的 stale pending
    # 浮现给用户（打破 flow-1-self-rising 自升循环）。整段 best-effort，永不抛、不改主流程结果。
    sweep_stranded_kanban_followup_pending(event, ledger)
    # 整体 suppress 必须早于 handoff、dirty gate 和 backend launch，避免子会话停止时继续生成 review/fork artifact。
    if explicit_suppress_requested(event, latest_user) and parse_session_hook_control(latest_user) is None:
        decision = suppressed_decision(event, ledger)
        if decision.payload is not None:
            emit(decision.payload)
        return 0

    decision = evaluate_stop_event(event, ledger)
    if decision.action == "launch":
        provider_health_decision = provider_health_guard_decision(decision, event, ledger)
        if provider_health_decision is not None and provider_health_decision.payload is not None:
            emit(provider_health_decision.payload)
            return 0
        workspace_guard_payload = kanban_task_workspace_guard_payload(
            event=event,
            cwd=decision.cwd,
            ledger=ledger,
        )
        if workspace_guard_payload is not None:
            emit(workspace_guard_payload)
            return 0
        emit(launch_backend(decision, event, ledger))
        return 0
    if decision.payload is not None:
        emit(decision.payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
