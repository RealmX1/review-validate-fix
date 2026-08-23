#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex  # 决策点横幅里渲染 --transcript-path 命令样例（路径可能含空格）
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
from adapters.codex.codex_provider_login import (  # noqa: E402 — codex provider 登录/二进制发现缝（S10f 抽出）
    codex_bin,
    codex_login_output_indicates_failure,
    is_codex_agent_id,
    maybe_start_codex_login,
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
from core.rvf_user_local_data_paths import (
    resolved_rvf_session_hook_state_dir,
    resolved_rvf_user_local_data_paths,
)
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import (
    CLINE_KANBAN_TASK_MARKER,
    CLINE_KANBAN_WORKTREE_MODES,
    DEFAULT_CLINE_KANBAN_WORKTREE_MODE,
    FORK_EXPERIMENT_MARKER,
    HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION,
    KANBAN_FOLLOWUP_MARKER,
    MANUAL_RVF_COMPLETED_AT_KEY,
    MANUAL_RVF_MARKER_KEYS,
    MANUAL_RVF_MARKER_TTL_SECONDS,
    MANUAL_RVF_RUN_ID_KEY,
    PARENT_CONTEXT_ARTIFACT_NAME,
    PARENT_CONTEXT_ENV,
    PARENT_CONTEXT_MAX_BYTES_ENV,
    PARENT_CONTEXT_PROMPT_KEY,
    RVF_FORK_MARKER,
    SESSION_HOOK_CONTROL_KEY,
    SESSION_PATH_KEYS,
    SESSION_SCOPE_PATH_KEYS,
    SUPPRESS_ENV_NAMES,
    SUPPRESS_STOP_HOOK_MARKER,
)
from core.provider_health.provider_health_guard import (  # noqa: E402 — provider 健康探测中性内核（S10f 抽出）
    ProviderHealthRequirement,
    command_output_text,
    run_provider_health_requirement,
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
import rvf_bootstrap_gate
from kanban_followup_trigger_transcript_presence import (
    TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND,
    TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN,
    TriggerPresence,
    classify_dispatch_trigger_presence,
    decision_token_literal,
    trigger_needle_for_lock_marker,
)
from kanban_followup_lock import (
    STATE_DELIVERED_AWAITING_AGENT_DECISION as KANBAN_FOLLOWUP_LOCK_STATE_AWAITING_DECISION,
    STATE_IN_PROGRESS as KANBAN_FOLLOWUP_LOCK_STATE_IN_PROGRESS,
    STATE_WAITING_BOOTSTRAP as KANBAN_FOLLOWUP_LOCK_STATE_WAITING_BOOTSTRAP,
    STATUS_ACTIVE as KANBAN_FOLLOWUP_LOCK_ACTIVE,
    STATUS_STALE as KANBAN_FOLLOWUP_LOCK_STALE,
    marker_state as kanban_followup_lock_state,
    promote_marker_out_of_awaiting_agent_decision as promote_kanban_followup_lock_out_of_awaiting,
    clear_marker as clear_kanban_followup_lock,
    revive_marker as revive_kanban_followup_lock,
    revive_count as kanban_followup_lock_revive_count,
    in_flight_run_shows_recent_activity as in_flight_rvf_run_shows_recent_activity,
    seconds_since_last_run_activity as seconds_since_last_in_flight_rvf_run_activity,
    marker_exceeds_revival_absolute_cap as kanban_followup_lock_exceeds_revival_absolute_cap,
    seconds_since_marker_armed as seconds_since_kanban_followup_lock_armed,
    stamp_bootstrap_gate as stamp_kanban_followup_bootstrap_gate,
    bootstrap_reengage_count as kanban_followup_bootstrap_reengage_count,
    bump_bootstrap_reengage_count as bump_kanban_followup_bootstrap_reengage_count,
    claim_bootstrap_replay_in_flight as claim_kanban_followup_bootstrap_replay_in_flight,
    release_bootstrap_replay_in_flight as release_kanban_followup_bootstrap_replay_in_flight,
    marker_status as kanban_followup_lock_status,
    read_marker as read_kanban_followup_lock,
    reengage_nudge_count as kanban_followup_lock_nudge_count,
    bump_reengage_nudge_count as bump_kanban_followup_lock_nudge_count,
    clear_pending_marker as clear_kanban_followup_pending,
    dispatch_single_flight_for_task as kanban_followup_dispatch_single_flight_for_task,
    iter_pending_markers as iter_kanban_followup_pending,
    pending_redispatch_attempt_count as kanban_followup_pending_redispatch_attempt_count,
    pending_status as kanban_followup_pending_status,
    read_pending_marker as read_kanban_followup_pending,
    stamp_pending_notified as stamp_kanban_followup_pending_notified,
    write_pending_marker as write_kanban_followup_pending,
    DECISION_POINT_DELIVERY_INTENT_STATE_RECORDED_BEFORE_SEND,
    DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
    clear_decision_point_delivery_intent,
    decision_point_delivery_intent_status,
    iter_decision_point_delivery_intents,
    read_decision_point_delivery_intent,
    update_decision_point_delivery_intent,
    write_decision_point_delivery_intent,
)
from rvf_awaiting_dispatched_agent_marker import (
    iter_active_awaiting_for_session as iter_active_awaiting_dispatched_agent_for_session,
)
from core.session_scope_allocation.session_change_manifest import build_manifest
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
    lease_release,
    select_committed_paths_reviewed_by_session,
    sweep_stale,
)
from review_reopen_marker import (
    REVIEW_REOPEN_CONSUMPTION_DISCARDED_NOT_ACTIVE,
    REVIEW_REOPEN_CONSUMPTION_NO_MARKER,
    STATUS_ACTIVE as REVIEW_REOPEN_ACTIVE,
    consume_review_reopen_marker_for_run_scope,
    read_review_reopen_marker,
    review_reopen_status,
)
from core.session_scope_allocation.committed_round_detection import (  # noqa: E402 — committed-round 窗口下界检测中性核（S10h-b 迁 core；内部消费已迁 core 的 round_baseline/review_highwater marker）
    _resolve_committed_round_base_ref,
    _resolve_committed_round_baseline,
)
from core.session_scope_allocation.session_scope_gate_pure_helpers import (  # noqa: E402 — stop-session-scope gate 纯 helper 中性核（S10h-c 精简迁；引擎留 event-边界解析器 + ledger 主体）
    REASON_PATCH_OWNERSHIP_INCOMPLETE,
    SessionScopePrecheck,
    _StopContextError,
    _positive_int_from_env,
    _string_list,
    patch_ownership_incomplete_details,
    session_change_type_from_manifest,
)
from cline_kanban_client import (
    DEFAULT_START_CMD as DEFAULT_CLINE_KANBAN_START_CMD,
    DEFAULT_START_TIMEOUT_SECONDS as DEFAULT_CLINE_KANBAN_START_TIMEOUT_SECONDS,
    DEFAULT_TASK_CMD as DEFAULT_CLINE_KANBAN_TASK_CMD,
    DEFAULT_TMUX_SESSION as DEFAULT_CLINE_KANBAN_TMUX_SESSION,
    KanbanTerminalDeliveryInterfaceUnavailableError,
    KanbanTerminalDeliveryRecordNotFoundError,
    KanbanTerminalDeliveryVerdictNotYetAvailableError,
    cancel_task_message_delivery,
    read_task_message_terminal_delivery_status,
    task_payload_from_list,
    task_session_exit_code,
    task_session_state,
    unpark_task as cline_kanban_unpark_task,
)
from core.cline_kanban_terminal_delivery.cline_kanban_terminal_delivery_receipt import (  # noqa: E402 — 终端投递回执契约的唯一判读点（cline-kanban task cdea3 契约；RVF 只读）
    REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY,
    REDISPATCH_DISPOSITION_NOT_NEEDED,
    REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
    REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION,
    ClineKanbanTerminalDeliveryReceipt,
    awaiting_receipt_exceeded_contract_convergence_budget,
    # 「取消回执」的两个判据必须在这里显式引入：它们只在 ``try/except/**else**`` 分支里被调用，
    # 而 ``else`` 里抛的 NameError **不会**被前面的 except 捕获 —— 漏引入的表征不是 ImportError，
    # 而是撤回在第 2 步炸掉、后面的放 lease / 清锁 / 清 pending 三步全部跳过（且 py_compile 看不见）。
    cancel_receipt_indicates_cancellation_rather_than_another_failure,
    cancel_receipt_indicates_message_already_reached_agent,
    classify_cline_kanban_terminal_delivery_receipt,
    describe_terminal_delivery_receipt_for_operator,
)
import rvf_post_tool_use
from session_label import (
    DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS,
    first_user_message,
    parent_conversation_fallback_chars,
    single_line_excerpt,
    strip_codex_user_message_preamble,
    text_from_message_payload,
)
import core.review_dispatch.review_validate_fix_dispatch_flow as dispatch_flow
import rvf_prep_file
import rvf_bootstrap_confirm
import rvf_withdrawn_dispatch_tombstone
from core.host_adapter.host_transcript_format_detection import (  # noqa: F401 — HOST_CODEX 经 module.X 给 parent-context 簇测试 re-export，随 S10e 簇迁移
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_KIMI_CODE,
    detect_transcript_format,
)
import rvf_parent_context
from host_adapter_resolver import resolve_host_adapter  # noqa: E402 — HostAdapter 装配 resolver（S10 注入面）
from core.stop_hook_reentry_budget import (  # noqa: E402 — Stop 驱动 agent 的跨 turn 再入预算（host-中性）
    consume_stop_hook_reentry_budget,
)
from core.stop_hook_output_audience import (  # noqa: E402 — Stop 出口受众分级契约（core 声明受众，adapter 决定线材）
    STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
    AgentDirectiveStopHookOutput,
    render_stop_hook_payload,
)
from core.parent_conversation_context.parent_conversation_origin_freeze import (  # noqa: E402 — 父会话来源冻结中性核（S10e 迁 core）
    freeze_parent_conversation_origin,
    parent_origin_summary_fields,
    source_origin_for_kanban_task,
)
from core.review_prompt_assembly.review_validate_fix_fork_prompt import (  # noqa: E402 — fork prompt 父会话来源块装配中性核（S10g 迁 core）
    add_parent_origin_to_rvf_fork_prompt as _core_add_parent_origin_to_rvf_fork_prompt,
    parent_origin_prompt_block as _core_parent_origin_prompt_block,
)
from rvf_dispatch_prompts import (
    cline_kanban_artifact_reference_lines,
    dispatch_scope_of_work_text,
)
# S4：Stop hook 降级为决策点信使——候选摘要（S2）+ 决策 token store（S1）+ 注入尝试 ledger（S4）。
# stop_decision_candidate_summary 顶层不反向 import 本模块（它用惰性 _load_stop_hook_module()
# 破 S4 环），故此处可安全 top-level import，不成环。
import rvf_decision_token_store as decision_token_store  # noqa: E402
import stop_decision_candidate_summary  # noqa: E402
import agentic_initiation_injection_attempt_ledger as injection_attempt_ledger  # noqa: E402


class BootstrapConfirmationRequired(Exception):
    """Raised when bootstrap dispatch is blocked pending user yes/Yes/YES confirmation."""

    def __init__(self, decision: rvf_bootstrap_confirm.Decision, marker_path: Path):
        self.decision = decision
        self.marker_path = marker_path
        super().__init__(decision.reason)


SKILL_DIR = Path(__file__).resolve().parents[1]
RVF_USER_LOCAL_DATA_PATHS = resolved_rvf_user_local_data_paths()
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = RVF_USER_LOCAL_DATA_PATHS.runtime_state_root
DEFAULT_CLINE_KANBAN_CLIENT = SKILL_DIR / "scripts" / "cline_kanban_client.py"
DEFAULT_CLINE_KANBAN_STATE_DIR = Path.home() / ".cline" / "kanban"
DEFAULT_PREPARE_REVIEW_RUN = SKILL_DIR / "scripts" / "prepare_review_run.py"
KANBAN_TASK_SUPPRESSIONS_DIRNAME = "kanban-task-suppressions"
# 以下为引擎簇私有常量（仅单簇引用），随其所属簇在 S10e–S10j 一并迁出；
# 被 ≥2 簇引用的共享常量已上提到 core/rvf_shared_runtime_constants（S10d，见顶部 import）。
# 与 ``kanban_followup_lock.DEFAULT_TTL_SECONDS`` /
# ``rvf_agent_decision_cli.AGENTIC_INITIATION_TRACKER_SCOPE_LEASE_TTL_SECONDS`` **同档**：
# 三者保护的是同一个事件（这一轮 RVF 还在跑），只是分别挡 Stop 重铸、挡 units 被 prune（就地
# 发起路径）、挡 units 被 prune（kanban-followup 路径）。任一方先到期，另两方就形同虚设——
# lease 一过期 units 回 available，下一次 Stop 立刻把同一批改动重新提议。
# 本轮实测一轮 RVF p50 = 84.3min、53% 超 60min，三者一并 1h → 2h（理由与分布见那把锁的注释）。
DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS = 2 * 60 * 60
KANBAN_FOLLOWUP_LEASE_TTL_ENV = "RVF_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS"
DEFAULT_RVF_MODE = "fork"
DEFAULT_FORK_LAUNCH_MODE = "auto"
# detached $rvf-analyze 线程注入的标记 env。语义上与 SUPPRESS_ENV_NAMES 区分开：
# 这是「这是 analyze 线程自己的 Stop event」的显式信号，用于 evaluate_stop_event
# 早退守卫，短路所有昂贵 gate，避免后台 analyze 递归触发新一轮 RVF。
RVF_ANALYZE_THREAD = "RVF_ANALYZE_THREAD"
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
    """一次 Stop 事件的裁决结果。

    出口受众由**构造决策的调用点显式声明**（``audience``），不靠 ``reason_code``
    白名单反查——``reengage_decision`` 的 ``reason_code`` 既有默认值也有
    ``f"kanban_followup_bootstrap_{…}"`` 这样动态生成的取值，任何静态清单都必漏。

    - ``audience == STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY``：正文在 ``payload``
      （``{"continue": True, "systemMessage": …}`` 形状，或 ``None`` = 静默停）。
    - ``audience == STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW``：正文在
      ``agent_directive``，``payload`` 必须为 ``None``；线上形状由 host adapter 在
      emit 时翻译（见 ``core.stop_hook_output_audience``）。
    """

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
    audience: str = STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    agent_directive: AgentDirectiveStopHookOutput | None = None


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
    # 单一裁决点见 core.rvf_user_local_data_paths；本函数语义即该裁决点的语义
    # （explicit → CODEX_RVF_STATE_DIR → runtime_state_root，刻意不回落 LOG_ROOT）。
    return resolved_rvf_session_hook_state_dir()


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


# short_identifier / short_run_ref / transcript_origin_label 已迁 core（S10e）：
# core.parent_conversation_context.parent_conversation_origin_freeze（顶部 re-import）。
# quoted_prompt_session_name（首条-user-message 摘录）已下沉 adapters/codex/codex_host_adapter
# 的 CodexHostAdapter.parent_transcript_quoted_session_name（Codex-schema 抽取归 adapter）。


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


SESSION_REFERENCE_LABEL_PREFIX_BY_HOST: dict[str, str] = {
    HOST_CODEX: "Codex",
    HOST_CLAUDE: "Claude",
    HOST_KIMI_CODE: "Kimi",
}
"""来源标签兜底级用的 host 短前缀（``<prefix> <session-ref>``）。

刻意与 ``HostAdapter.host_display_name()``（"Claude Code" / "Kimi Code CLI"）分开：这里要的
是能和 session ref 拼成一行的短名。未登记的 host 取 ``"Codex"``，与整条链路「未知兜底
Codex」的口径一致——这一层只影响人类可读标签，不选线材。
"""


def parent_conversation_origin(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    run_id: str,
    parent_thread_name: str | None = None,
    name_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """引擎 assembly wrapper（S10e）：预解析 host 元数据 + 注入 HostAdapter → core freeze。

    ``core.parent_conversation_context.freeze_parent_conversation_origin`` 持中性冻结逻辑；
    本 wrapper 只负责 host 相关的预解析（``detect_transcript_format`` 探测、``session_id``
    兜底 ``session_id_from_path``、Codex ``session_index`` 查名、session-ref 短前缀
    Codex/Claude）与返回后注入 ``host_kind`` 键——这些是 host-身份 / Codex-read 职责，随
    S10j 折入 ``codex_stop_hook_entry``，暂留引擎 assembly 层（core host-free 不承担）。
    """
    host_kind: str | None = None
    if parent_thread_path is not None:
        try:
            host_kind = detect_transcript_format(parent_thread_path)
        except Exception:
            host_kind = None
    session_id = parent_session_id or (
        session_id_from_path(parent_thread_path) if parent_thread_path is not None else None
    )
    origin = freeze_parent_conversation_origin(
        parent_thread_path=parent_thread_path,
        run_id=run_id,
        host_adapter=resolve_host_adapter(host_kind),
        session_id=session_id,
        thread_name_from_index=session_index_thread_name(session_id),
        session_ref_prefix=SESSION_REFERENCE_LABEL_PREFIX_BY_HOST.get(host_kind, "Codex"),
        parent_thread_name=parent_thread_name,
        name_lookup=name_lookup,
    )
    origin["host_kind"] = host_kind
    return origin


def parent_origin_prompt_block(
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    """assembly wrapper：解析 host 人类可读名后委派 core 装配父会话来源块。

    host 显示名（"Claude Code" / "Codex"）经注入的 ``HostAdapter.host_display_name()``
    从 ``parent_origin`` 的 host 归属键解析，与旧 ``parent_conversation_host_label`` 的
    三情形（Claude / Codex / host 未知兜底 Codex）逐字等价，再以 ``host_display_label``
    传入 core（守 R-1：host 判定留 assembly 层）。
    """
    host_display_label = resolve_host_adapter(parent_origin.get("host_kind")).host_display_name()
    return _core_parent_origin_prompt_block(
        parent_origin=parent_origin,
        origin_path=origin_path,
        host_display_label=host_display_label,
    )


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
    """assembly wrapper：解析 host 显示名后委派 core 追加父会话来源块（幂等不变）。"""
    host_display_label = resolve_host_adapter(parent_origin.get("host_kind")).host_display_name()
    return _core_add_parent_origin_to_rvf_fork_prompt(
        prompt,
        parent_origin=parent_origin,
        origin_path=origin_path,
        host_display_label=host_display_label,
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


# 幂等守卫必须匹配**带实际 token 的那一行**，不能只匹配裸标记名 `RVF_DISPATCH=token=`：
# 正文里合法地提到这个标记名（例如告诉 agent「token 取自本条消息里 `RVF_DISPATCH=token=`
# 后面那串」）会让裸子串守卫误判「已经加过了」，于是真正的 token 行永远不被追加，整条派发
# 在 UPS 侧就成了无 token 的空注入。形状与 UPS 的 ``DISPATCH_TOKEN_RE`` 一致。
DISPATCH_TOKEN_LINE_RE = re.compile(r"\bRVF_DISPATCH=token=[0-9A-Fa-f]{16}\b")


def add_dispatch_prep_to_prompt(prompt: str, record: rvf_prep_file.PrepFileRecord) -> str:
    if DISPATCH_TOKEN_LINE_RE.search(prompt):
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


def kanban_followup_decline_prompt_block() -> str:
    """终端投递 prompt 里的**谢绝出口**。

    P1 之后自动 RVF 的发起一律先经**发起决策点横幅**，谢绝已前移到那里（``rvf-agent decline``），
    所以这段不再是「agent 唯一能说不的地方」。它保留下来只服务一条路径：操作者显式设置
    ``CODEX_RVF_FORK_MODE=kanban-followup`` 时仍走的终端投递逃生口——那条祈使句依旧落地即执行，
    没有事前决策点，故仍需要一条事后的正当退出路径，而不是硬跑或干停。

    ``<dispatch-token>`` 就是本条 prompt 里 ``RVF_DISPATCH=token=`` 后面那串（**dispatch token，
    不是横幅上那枚 S3 决策 token**；后者用 ``rvf-agent decline``）。
    """
    return (
        "如果你判断这一轮**不该审**（例如本轮改动与你正在做的事无关、或用户已明确让你转做别的"
        "工作），不要硬跑、也不要干停，改用谢绝出口：\n"
        "```\n"
        "rvf-agent decline-dispatched-review --token <dispatch-token> --reason <一句话理由>\n"
        "```\n"
        "`<dispatch-token>` 取本条消息里 `RVF_DISPATCH=token=` 后面那串。默认语义是「这一轮"
        "不审」——本轮 scope 退回可审池，后续 Stop 仍可能重新提议同一批改动。\n"
        "若你确定这批 diff **根本不需要审核**，再加 "
        "`--mark-declined-scope-units-review-not-required`：后续 RVF 不再把它们纳入范围。"
        "该标记是 diff（hunk）级而非文件级——这些 diff 内容一旦被改动，会作为新的改动重新"
        "进入审核范围。默认不加此标记。\n"
        # 退出码语义必须写进注入正文：该命令的副作用（豁免 / 释放 lease / 清锁）是分步的，
        # 任一步没成都会以非 0 退出并列出 failed_steps。agent 若只看「命令跑完了」就当作已谢绝，
        # 会在锁其实还在、或豁免其实没生效的情况下继续往下走——那正是本轮要根治的「声称做了 A、
        # 实际做了 B」那一类。
        "**看退出码**：`0` 才表示谢绝完全生效。非 0（`7 = decline_incomplete`）表示副作用只做成"
        "了一部分——例如锁属于**另一轮** dispatch 而被保留、lease 未释放、或请求了豁免却没声明成。"
        "此时不要当作已谢绝：按输出里的 `failed_steps` / `next_steps` 补救，或如实告诉开发者哪一步"
        "没成。尤其带豁免时，只要退出码非 0，那批 diff 就**没有**被声明为「不需审核」。\n\n"
    )


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
    """终端投递给**当前会话**的 follow-up trigger 正文。

    ⚠️ 这是整个 RVF 里唯一一条「投给已在跑的那个 agent、且落地即执行」的祈使句。P1 之后它
    **不再是默认发起路径**：默认路径是发起决策点横幅（``_render_decision_point_injection_prompt``），
    由 agent 显式 ``rvf-agent start`` / ``decline``。本函数只在操作者显式设置
    ``CODEX_RVF_FORK_MODE=kanban-followup`` 时可达，属保留的逃生口/兼容入口。

    新增自注入正文时请注意区分收件人：``fork_review_validate_fix_prompt`` 与 cline-kanban 新
    task prompt 的收件人是**新会话**，祈使句形态是正确的；只有本函数是自注入。
    """
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
        "为什么你收到的是这条而不是发起决策点横幅：本轮被**显式**配置成了 "
        "`CODEX_RVF_FORK_MODE=kanban-followup`（保留的终端投递逃生口）。默认路径是横幅 + "
        "`rvf-agent start`/`decline`，由你决定发不发起；这条投递没有事前决策点，故谢绝出口在下方。\n\n"
        "请在当前 task worktree 中运行完整 review-validate-fix。目标仓库为上面的 "
        "`RVF_TARGET_REPO`；如果当前 task worktree 的 repo root 与该路径不同，以当前 task "
        "worktree 为执行位置，并在 handoff 中记录这一点。\n\n"
        f"{kanban_followup_decline_prompt_block()}"
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

    经 ``host_adapter_resolver.resolve_host_adapter`` 装配的具体 ``HostAdapter`` bundle
    索取 ``cline_kanban_agent_id``（Codex→``codex`` / Claude→``claude``）——host-镜像
    默认值这一 host 行为归注入面，引擎不再内联 ``host_kind ==`` 分支。host 识别仍复用
    ``detect_transcript_format``；无法识别（父 transcript 缺失 / 未知格式 / 探测异常）时
    resolver 兜底 ``CodexHostAdapter`` → ``codex``，保证既有 Codex-only 用例零回归。
    """
    host: str | None = None
    if parent_thread_path is not None:
        try:
            host = detect_transcript_format(parent_thread_path)
        except Exception:
            host = None
    return resolve_host_adapter(host).cline_kanban_agent_id()


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
                output_failure_predicate=codex_login_output_indicates_failure,
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
                    output_failure_predicate=codex_login_output_indicates_failure,
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
                    output_failure_predicate=codex_login_output_indicates_failure,
                )
            ]

    return []


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


def kanban_followup_lock_repository_anchor(
    event: dict[str, Any] | None = None,
    *,
    cwd: str | None = None,
    marker: dict[str, Any] | None = None,
) -> str | None:
    """in-progress 锁 key 里那一段仓身份的**解析锚点**（见 ``kanban_followup_lock.marker_paths``）。

    返回的只是「仓里的某个路径」，不是身份本身：身份由
    ``resolve_repository_identity_for_scope_marker_key`` 经 ``git_common_dir`` 归一，所以
    工作树内**任意**路径都解析到同一个仓身份——写侧（UPS arm）与读侧（Stop / rvf-agent CLI）
    只要各自给出一个仓内路径就天然一致，不需要彼此约定同一个字符串。

    取值优先级：显式 ``cwd`` → marker 自己记的 ``repo`` / ``cwd`` → Stop event 的 ``cwd``。
    marker 优先于 event 是刻意的：撤回 / 对账路径处理的是**那把锁**，锚点该跟着锁走。
    全部缺失返回 ``None`` ⇒ ``marker_paths`` 返回空候选（fail-closed，至多多征询一次）。
    """
    for candidate in (
        cwd,
        (marker or {}).get("repo"),
        (marker or {}).get("cwd"),
        (event or {}).get("cwd"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _kanban_followup_delivery_channel(message_id: Any) -> str:
    """从 Cline Kanban message_id 推断投递通道。**只作审计字段，禁止当作投递确认判据。**

    ⚠️ F2a 起这个函数**不再**回答「送到没有」。原因有二：

    1. 新契约下 ``message_id`` 恒为 ``terminal:<taskId>:<idempotencyKey>``，前缀不再区分通道；
    2. 更根本地，通道从来就不是投递结果——历史实现把「有 app-server socket」当成「已送达」，
       而那个回执是在 mutate **之前**读的快照推断，与实际投递结果无关。2026-08-08 那次
       49 分钟事故正是这条推断谎报成功造成的。

    唯一的投递真相是 ``terminal_delivery_status``，判读点是
    ``core.cline_kanban_terminal_delivery``。本函数保留下来只为把通道形态留在 ledger 里备查。
    """
    if isinstance(message_id, str) and message_id.strip().lower().startswith("terminal:"):
        return "terminal"
    return "app-server"


# launch_backend 返回的是 ``ledger.hook_payload(...)`` = ``{"continue","systemMessage"}`` 两键，
# **顶层没有 status 键**（status 只进 summary / systemMessage 文本）。故不能用 result["status"] 判断
# 是否成功派发。改由「成功派发 self-rising followup」那一条返回路径显式置内部键
# ``_rvf_keep_self_park=True`` 作为 main 的保留-park 信号（main 在 emit 前 pop 掉、不泄漏给 Claude）。
# 命中 → resume 在路上 → 保留 park；缺失（派发失败 / kanban 早退 skip / 非 followup backend）→ unpark。
_RVF_KEEP_SELF_PARK_FLAG = "_rvf_keep_self_park"


def _launch_result_keeps_self_park(result: Any) -> bool:
    return bool(isinstance(result, dict) and result.get(_RVF_KEEP_SELF_PARK_FLAG))


# skip 分支中「followup 仍活跃 / resume 仍在途」的 reason_code：这些 skip 不代表无 resume，
# 必须保留 park（绝不在 dispatch-in-flight / in-progress 期间提前 unpark，否则 followup 落地前
# 卡片抑制就被误解除）。其余 skip（handoff 完成、无 owned dirty 等）= 无 resume → 照常 unpark。
_KANBAN_FOLLOWUP_RESUME_PENDING_REASONS = frozenset(
    {
        "kanban_followup_in_progress",
        "kanban_followup_dispatch_in_flight",
        # re-engage force-continue：现有 RVF run 仍在进行、agent 被续跑令其收尾该轮，
        # resume 在路上 → 必须保留 park，否则 handoff 落地前卡片抑制会被误解除。
        "kanban_followup_in_progress_reengage",
        # P1 的兜底分支（``classify_review_initiation_lifecycle`` 判出
        # ``accepted_run_in_flight``）：语义与上面的 ``kanban_followup_in_progress`` 完全同类
        # ——一轮已被 agent 接受的 RVF 仍在途，handoff 尚未落地。集合名沿用历史的
        # ``KANBAN_FOLLOWUP_`` 前缀，但它承载的判据早已不限于 kanban-followup 那条 transport。
        "review_initiation_accepted_run_in_flight",
    }
)


def _skip_decision_should_unpark(decision: StopDecision) -> bool:
    """skip / 不 launch 分支：是否应触发 self-park 安全阀。

    resume 仍在途 / followup 仍活跃的 skip（``_KANBAN_FOLLOWUP_RESUME_PENDING_REASONS``），以及
    S4 决策点仍在途的 skip（``_DECISION_POINT_RESUME_PENDING_REASONS``：token 仍 minted/injected/
    acknowledged，等 agent start/decline）必须保留 park、返回 False；其余 skip（handoff 完成、无
    owned dirty、决策点 declined/熔断/无候选等无 resume）→ True（照常 unpark）。
    """
    if decision.reason_code in _KANBAN_FOLLOWUP_RESUME_PENDING_REASONS:
        return False
    if decision.reason_code in _DECISION_POINT_RESUME_PENDING_REASONS:
        return False
    return True


def _unpark_stranded_task(project_path: Any, task_id: str) -> bool:
    """跨 task 幂等 unpark（stranded-sweep 用）：直接按 marker 的 task_id/project_path unpark。

    与 ``unpark_self_parked_card`` 不同——这里处理的是**别的** task 遗留的泄漏 park（派发成功但
    resume 静默丢投），无本会话 self-park 标记可读，故直接按 marker 字段 unpark。best-effort、永不抛。
    """
    try:
        if not (isinstance(task_id, str) and task_id.strip()):
            return False
        repo = (
            Path(project_path).expanduser()
            if isinstance(project_path, str) and project_path.strip()
            else None
        )
        if repo is None:
            return False
        task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
        cline_kanban_unpark_task(task_cmd=task_cmd, repo=repo, task_id=task_id.strip())
        return True
    except Exception:
        return False


def unpark_self_parked_card(event: dict[str, Any], ledger: RunLedger, *, reason: str) -> None:
    """park 泄漏安全阀（proposer §②）：若本会话本轮经 PostToolUse self-park、但本次 Stop 不会有
    resume（派发失败 / 非 followup backend / skip / 不 launch），则幂等 unpark 父卡片并清 self-park
    标记。Kanban park 无 TTL（Option A 把 TTL 所有权留给编排层），故必须由 RVF 显式清，否则
    泄漏的 park 会把下一次**真实** Stop 的通知误抑制。

    成功派发（resume 在路上）的路径**不**调本函数——交 Turn2 UPS 的 to_in_progress 自动 unpark /
    stranded-sweep 兜底。整段 best-effort、永不抛、不改主流程结果；未 self-park 时是极廉价的一次
    小文件读后即返回。
    """
    try:
        session_id = event.get("session_id")
        state = rvf_post_tool_use.read_self_park_state(
            session_id if isinstance(session_id, str) else None
        )
        if not isinstance(state, dict):
            return
        task_id = state.get("task_id")
        if not (isinstance(task_id, str) and task_id.strip()):
            return
        project_path = state.get("project_path")
        repo = (
            Path(project_path).expanduser()
            if isinstance(project_path, str) and project_path.strip()
            else Path(str(event.get("cwd") or ".")).expanduser()
        )
        task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
        ok = True
        detail: str | None = None
        try:
            cline_kanban_unpark_task(task_cmd=task_cmd, repo=repo, task_id=task_id.strip())
        except Exception as exc:  # best-effort：unpark 失败仅记日志，不抛
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        # 无论 unpark 成败都清 self-park 标记：成功→已清；失败→避免每次 Stop 重复 unpark 刷屏，
        # 残留 park 仍有 kanban onExit / 磁盘重载 / stranded-sweep 作 backstop。
        rvf_post_tool_use.clear_self_park_state(session_id if isinstance(session_id, str) else None)
        ledger.event(
            phase="complete",
            event="kanban_followup_self_park_unparked",
            status="completed" if ok else "warning",
            reason_code=f"self_park_unpark_{reason}",
            level=None if ok else "warn",
            cline_kanban_task_id=task_id.strip(),
            cline_kanban_project_path=str(repo),
            kanban_self_park_unpark_ok=ok,
            kanban_self_park_unpark_error=detail,
        )
    except Exception:
        # 安全阀本身绝不能影响主 Stop 流程。
        pass


def clear_kanban_followup_lock_for_event(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | None,
    handoff_path: str | None = None,
) -> list[str]:
    task_id = current_kanban_task_id(event)
    session_id = kanban_followup_lock_session_id(event)
    removed = clear_kanban_followup_lock(
        task_id=task_id,
        session_id=session_id,
        repo=kanban_followup_lock_repository_anchor(event, cwd=cwd),
    )
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


def _query_kanban_terminal_delivery_receipt(
    marker: dict[str, Any] | None,
) -> tuple[ClineKanbanTerminalDeliveryReceipt | None, str | None]:
    """对一条 pending marker 现查 cline-kanban 的**真实**投递终态。

    返回 ``(receipt, degraded_reason)``：其一必为 None。``degraded_reason`` 的每一种取值都表示
    「拿不到结论」，**没有一种表示投递失败**——这正是本函数存在的理由：历史实现只有「TTL 到了
    还没被清 ⇒ 大概是丢了」这一种猜测，既无法区分「真丢了」与「对方还在有界收敛中」，也无法
    区分「对方没有这个接口」。契约第 8 条明确：接口不存在是**合法运行态**，退回纯 RVF 侧语义
    中和即可，不得阻塞派发、也不得记成一次投递失败。

    整段 best-effort，永不抛。
    """
    if not isinstance(marker, dict):
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_MISSING_QUERY_KEYS
    task_id = marker.get("kanban_task_id")
    project_path = marker.get("kanban_project_path")
    idempotency_key = marker.get("idempotency_key")
    if not (
        isinstance(task_id, str)
        and task_id.strip()
        and isinstance(project_path, str)
        and project_path.strip()
        and isinstance(idempotency_key, str)
        and idempotency_key.strip()
    ):
        # 迁移期的历史 marker 不带 idempotency_key（F2a 之前写的），查不了 —— 退回 TTL 对账。
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_MISSING_QUERY_KEYS
    timeout = _kanban_followup_env_float(
        KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_SECONDS,
    )
    try:
        payload = read_task_message_terminal_delivery_status(
            task_cmd=os.environ.get(
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD
            ),
            repo=Path(project_path).expanduser(),
            task_id=task_id.strip(),
            idempotency_key=idempotency_key.strip(),
            timeout=timeout if timeout > 0 else None,
        )
    except KanbanTerminalDeliveryInterfaceUnavailableError:
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_INTERFACE_UNAVAILABLE
    except KanbanTerminalDeliveryVerdictNotYetAvailableError:
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_VERDICT_NOT_YET_AVAILABLE
    except KanbanTerminalDeliveryRecordNotFoundError:
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_RECORD_NOT_FOUND
    except Exception:
        return None, TERMINAL_DELIVERY_QUERY_DEGRADED_QUERY_FAILED
    return classify_cline_kanban_terminal_delivery_receipt(payload), None


# 中和一条 follow-up 投递的三种结局。``withdrawn`` 这个布尔值从此**只**回答「这条投递被中和
# 了吗」，不再兼职回答「本地状态清干净了吗」：两者在失败路径上会分叉（既没抢到 tombstone、
# 物理取消也用不了，但 lease/锁/pending 照样清掉），把它们混成一个字段等于对调用方谎报。
KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED = "neutralized"
KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED = "already_consumed"
KANBAN_FOLLOWUP_DISPATCH_NOT_NEUTRALIZED = "not_neutralized"


def _neutralize_kanban_followup_dispatch_token(
    marker: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """撤回的**前两步**：语义中和（token tombstone）+ 物理取消（``message-cancel``）。

    刻意与「清理本地状态」（放 lease / 清锁 / 清 pending）分开，因为有两个调用方只需要中和：

    - ``_withdraw_unconfirmed_kanban_followup_dispatch``：中和 **+** 清理（这一轮不再继续）；
    - ``_maybe_redispatch_stranded_kanban_followup``：中和 **旧** token 后立刻用**新** token 重投，
      本轮 review 仍在继续 —— 它绝不能顺手把 lease 放掉、把 pending 删掉。

    返回诊断字典，``neutralization`` 取三值之一：

    - ``already_consumed``：token 终态已是 ``consumed`` —— UPS 正在消费这条投递，它**不是**未投递。
      调用方必须就此收手（撤回会拆掉一轮正在跑的 review；重投会投出第二条）。
    - ``neutralized``：抢到了 ``withdrawn`` 终态（语义中和），或物理取消确认「消息还没进 agent」。
    - ``not_neutralized``：两条路都没走通（tombstone 机制此刻不可用 **且** 取消失败/接口不存在）。
      此时那条投递若迟到落地仍会执行 —— 如实上报，绝不谎报成功。

    判「是不是被取消的」读 ``terminal_delivery_failure_reason``，**不读 ``cancel_result``**
    （契约的显式警告：后者只回答「消息是否已确认进入 agent」）。整段 best-effort，绝不抛。
    """
    token = marker.get("token")
    task_id = marker.get("kanban_task_id")
    diagnostics: dict[str, Any] = {"reason": reason, "token": token}
    semantically_neutralized = False

    try:
        claim = rvf_withdrawn_dispatch_tombstone.claim_dispatch_token_terminal_state(
            token,
            state=rvf_withdrawn_dispatch_tombstone.STATE_WITHDRAWN,
            reason=reason,
            details={
                "kanban_task_id": task_id,
                "run_id": marker.get("run_id"),
                "idempotency_key": marker.get("idempotency_key"),
            },
        )
    except Exception as exc:
        diagnostics["tombstone"] = {"claimed": False, "error": f"{type(exc).__name__}: {exc}"}
        claim = None
    if claim is not None:
        diagnostics["tombstone"] = {
            "claimed": claim.won,
            "applicable": claim.applicable,
            "existing_state": claim.existing_state,
            "path": str(claim.path) if claim.path else None,
        }
        if claim.applicable and not claim.won:
            # 输给了 ``consumed``：这条投递其实已经落地并被 agent 接手了。撤回它会把一轮
            # **正在进行**的 review 拆掉（放掉它的 lease、清掉它的锁），比不撤回坏得多。
            diagnostics["neutralization"] = KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED
            diagnostics["skipped_because"] = "dispatch_token_already_consumed"
            return diagnostics
        semantically_neutralized = bool(claim.applicable and claim.won)

    project_path = marker.get("kanban_project_path")
    idempotency_key = marker.get("idempotency_key")
    if (
        isinstance(project_path, str)
        and project_path.strip()
        and isinstance(task_id, str)
        and task_id.strip()
        and isinstance(idempotency_key, str)
        and idempotency_key.strip()
    ):
        cancel_timeout = _kanban_followup_env_float(
            KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_ENV,
            DEFAULT_KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_SECONDS,
        )
        try:
            cancel_payload = cancel_task_message_delivery(
                task_cmd=os.environ.get(
                    "CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD
                ),
                repo=Path(project_path).expanduser(),
                task_id=task_id.strip(),
                idempotency_key=idempotency_key.strip(),
                timeout=cancel_timeout if cancel_timeout > 0 else None,
            )
        except KanbanTerminalDeliveryInterfaceUnavailableError:
            diagnostics["physical_cancel"] = {
                "attempted": True,
                "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_INTERFACE_UNAVAILABLE,
            }
        except KanbanTerminalDeliveryRecordNotFoundError:
            diagnostics["physical_cancel"] = {
                "attempted": True,
                "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_RECORD_NOT_FOUND,
            }
        except Exception as exc:
            diagnostics["physical_cancel"] = {
                "attempted": True,
                "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_QUERY_FAILED,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            diagnostics["physical_cancel"] = {
                "attempted": True,
                "outcome": "completed",
                # 这两个判断问的是**两件不同的事**，故用两个具名函数分别问，
                # 绝不用 ``cancel_result`` 去回答「是不是被取消的」（契约的显式警告）。
                "message_already_reached_agent": (
                    cancel_receipt_indicates_message_already_reached_agent(cancel_payload)
                ),
                "cancelled_rather_than_failed_for_another_reason": (
                    cancel_receipt_indicates_cancellation_rather_than_another_failure(
                        cancel_payload
                    )
                ),
                "terminal_delivery_status": cancel_payload.get("terminal_delivery_status"),
                "terminal_delivery_failure_reason": cancel_payload.get(
                    "terminal_delivery_failure_reason"
                ),
            }
    else:
        diagnostics["physical_cancel"] = {
            "attempted": False,
            "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_MISSING_QUERY_KEYS,
        }

    # 物理取消只有在「取消调用真的跑完了 **且** 回执说消息还没进 agent」时才算中和成功。
    # 「已进 agent」不是中和 —— 那条消息已经在对面了，取消只是没赶上。
    physical_cancel = diagnostics.get("physical_cancel") or {}
    physically_cancelled = bool(
        physical_cancel.get("outcome") == "completed"
        and physical_cancel.get("message_already_reached_agent") is False
    )
    diagnostics["semantically_neutralized"] = semantically_neutralized
    diagnostics["physically_cancelled"] = physically_cancelled
    diagnostics["neutralization"] = (
        KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED
        if (semantically_neutralized or physically_cancelled)
        else KANBAN_FOLLOWUP_DISPATCH_NOT_NEUTRALIZED
    )
    return diagnostics


def _withdraw_unconfirmed_kanban_followup_dispatch(
    marker: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """把一条**不再指望它落地**的 follow-up 派发正式撤回，而不只是把 pending 记录删掉。

    「删掉 pending 就当没发生过」是不够的：那条投递可能**随后**才落地（实测迟到过 49 分钟），
    落地时它完全可执行，会跑出一轮无锁、无 lease、无 scope 契约的 review。所以撤回要做四件事：

    1. **抢 token 终态 ``withdrawn``**（``O_EXCL`` CAS）。抢不到且既有态是 ``consumed``，说明
       UPS 已经在消费它——那不是「未投递」，绝不能撤回（该走 active-run cancel，见 F2b）。
       这一步是**先决条件**：抢不到就整个放弃撤回。
    2. **物理取消**（``kanban task message-cancel``）。接口不存在是合法运行态（契约第 8 条），
       此时只有语义中和、没有物理取消——这正是契约边界所在，如实记录、不谎报。
    3. **释放 tracker lease**，让被这一轮占住的 scope unit 退回 ``available``；否则那批改动
       会以孤儿 ``assigned`` 的形态从此对候选查询不可见 —— 一种沉默的漏审。
    4. **清 in-progress 锁与 pending 记录**，让下一次 Stop 能按**当时**的工作树重新决策。

    第 1、2 步交 ``_neutralize_kanban_followup_dispatch_token``；本函数负责 3、4 与**诚实回执**。

    ## 中和失败时的失败方向

    tombstone 机制此刻不可用（``applicable=False``：token 形状怪 / 文件系统报错）**且**
    ``message-cancel`` 也失败或不存在时，既没有物理取消、也没有语义中和。此时：

    - 仍然做 3、4 步的清理 —— 不清理只会把这一轮**永久** park 住（lease 挂着、pending 挂着），
      而那正是本项目反复认定为最坏结局的「沉默的漏审」；何况留着锁并不能阻止那条投递执行。
    - 但回执里 ``withdrawn`` 置 **False** 并置 ``cleaned_up_without_neutralization=True``：
      「清理跑完了」与「那条投递被中和了」是两件事，一个 ``withdrawn=True`` 把它们混起来就是
      对调用方谎报。这两个字段随 withdrawal 字典整个进 ledger 事件，使「旧投递没被中和、仍
      可能迟到落地」可取证。
    - 这一支**不**据此拦下本轮的新 launch：清理已经做完，下一次 Stop 会在完全相同的认知下照常
      派发，拦这一轮买不到任何东西、只是无理由推迟评审。会拦的只有 ``already_consumed``
      （见 ``_kanban_followup_withdrawal_blocks_new_dispatch``），那一支撤回什么都没清。

    整段 best-effort：每一步的成败都如实落进返回的诊断字典，绝不抛、绝不阻断 Stop。
    """
    task_id = marker.get("kanban_task_id")
    marker_token = marker.get("token")
    diagnostics = _neutralize_kanban_followup_dispatch_token(marker, reason=reason)
    if diagnostics.get("neutralization") == KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED:
        # lease 与 in-progress 锁**绝不动** —— 那轮 review 正在跑，这两样是它的。
        #
        # 但 pending 记录要清（按本 marker 的 token 走防误清 guard）：token 终态已是 ``consumed``，
        # 这条「已派发、尚未成为 turn」的记录就此确定作废，去重责任已由 UPS arm 的 in-progress 锁
        # 接手。留着它的代价是实打实的：此后**每一次** Stop 都会重走这条路径、每一次都被拦下，
        # 直到 tombstone TTL（默认 6h）过期才自解 —— 一段没有任何收益的静默停摆。
        # （投递已确认送达时也写 pending 之后，这条路径才真的会被走到：UPS 可能在 dispatch 侧
        # 落盘 pending **之前**就已 arm 并清过一次，于是那条 pending 从诞生起就是孤儿。）
        try:
            diagnostics["removed_pending_marker_paths"] = clear_kanban_followup_pending(
                task_id=task_id if isinstance(task_id, str) else None,
                token=marker_token if isinstance(marker_token, str) else None,
            )
        except Exception as exc:
            diagnostics["removed_pending_marker_paths"] = []
            diagnostics["pending_clear_error"] = f"{type(exc).__name__}: {exc}"
        diagnostics["withdrawn"] = False
        return diagnostics

    lease_id, lease_diagnostic = release_tracker_lease_for_reconciled_kanban_followup_lock(
        marker, reason=f"kanban_followup_withdrawn:{reason}"
    )
    diagnostics["tracker_lease_id"] = lease_id
    diagnostics["tracker_lease_release"] = lease_diagnostic

    try:
        # **只清 task 维度的锁**，不传 session_id。``clear_marker`` 会把 marker_paths 里的每一条
        # 都删掉，而 sess-path 锁属于「没有 task_id 的那类派发」——把它一起删会误清一把与本次
        # 撤回无关的锁。撤回的边界就是「这个 task 的这一轮」。
        diagnostics["removed_in_progress_marker_paths"] = clear_kanban_followup_lock(
            task_id=task_id if isinstance(task_id, str) else None,
            session_id=None,
            repo=kanban_followup_lock_repository_anchor(marker=marker),
        )
    except Exception as exc:
        diagnostics["removed_in_progress_marker_paths"] = []
        diagnostics["in_progress_clear_error"] = f"{type(exc).__name__}: {exc}"
    try:
        diagnostics["removed_pending_marker_paths"] = clear_kanban_followup_pending(
            task_id=task_id if isinstance(task_id, str) else None
        )
    except Exception as exc:
        diagnostics["removed_pending_marker_paths"] = []
        diagnostics["pending_clear_error"] = f"{type(exc).__name__}: {exc}"

    neutralized = diagnostics.get("neutralization") == KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED
    diagnostics["withdrawn"] = neutralized
    if not neutralized:
        # 清理跑完了，但那条投递并没有被中和 —— 回执必须把这两件事分开说。
        diagnostics["cleaned_up_without_neutralization"] = True
    return diagnostics


def _kanban_followup_withdrawal_blocks_new_dispatch(withdrawal: Any) -> str | None:
    """撤回回执是否要求「本轮别放行新的 follow-up launch」；要求则返回原因码，否则 None。

    唯一要求收手的结局是 ``already_consumed``：原 review 其实已被 UPS 接手并在跑，再 launch
    就是**第二条** follow-up。这一支不会造成漏审——撤回在这种情形下**没动 lease、也没清锁**
    （它们属于那轮正在跑的 review），下一次 Stop 会对着 UPS arm 的 in-progress 锁重新判断。

    ``not_neutralized`` 刻意**不**在此列，尽管它的 ``withdrawn`` 也是 False。理由是那道闸不成立：
    清理已经做完（pending 没了），下一次 Stop 会在**完全相同的**认知下照常派发 —— 拦这一轮
    买不到任何东西，只是把评审无理由地推迟一轮。它的正确归宿是**诚实上报**：
    ``withdrawn=False`` + ``cleaned_up_without_neutralization=True`` 随 withdrawal 字典整个进
    ledger 事件，使「那条投递没被中和、仍可能迟到落地」这件事可取证，而不是被一个
    ``withdrawn=True`` 盖掉。
    """
    if not isinstance(withdrawal, dict):
        return None
    if withdrawal.get("withdrawn"):
        return None
    if withdrawal.get("neutralization") == KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED:
        return "kanban_followup_prior_dispatch_already_consumed"
    return None


#: 每种「对账完了但本轮不放行新 launch」的人类可读理由（reason_code → 文案）。
_KANBAN_FOLLOWUP_WITHHELD_DISPATCH_MESSAGES = {
    "kanban_followup_prior_dispatch_already_consumed": (
        "上一条 Cline Kanban RVF follow-up 其实已被目标会话接手（dispatch token 终态为 consumed）；"
        "本次 Stop 不再投第二条 —— 那轮 review 正在跑。"
    ),
    "kanban_followup_redispatch_disposition_withheld": (
        "上一条 Cline Kanban RVF follow-up 确定未送达，但该失败的契约处置是「不自动重投」"
        "（等会话起来 / 等用户答完 / 人正在输入框里打字，或文本可能仍残留在输入框中）；"
        "本次 Stop 不自动重投，交由人决定。"
    ),
}


def _kanban_followup_withheld_dispatch_decision(
    ledger: RunLedger,
    *,
    reason_code: str,
    withdrawal: dict[str, Any],
    pending: dict[str, Any],
    pending_path: Any,
    receipt_fields: dict[str, Any],
    task_id: str | None,
    session_id: str | None,
    cwd: str | None,
) -> StopDecision:
    """pending 对账已收尾、但**本轮不放行**新的 follow-up launch 时的统一 skip 决策。

    历史实现里这三条对账分支一律 ``return None``（= 放行），于是撤回回执里那句「其实已被
    UPS 接手」「根本没中和成功」完全没人听 —— 该收手的时候照样投出第二条 follow-up。
    """
    return skip_decision(
        _KANBAN_FOLLOWUP_WITHHELD_DISPATCH_MESSAGES.get(
            reason_code, "上一条 Cline Kanban RVF follow-up 的对账结果要求本次 Stop 不重复投递。"
        ),
        ledger,
        reason_code,
        cwd=cwd,
        backend="kanban-followup",
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_pending_marker=pending,
        kanban_followup_pending_marker_path=pending_path,
        kanban_followup_withdrawal=withdrawal,
        **receipt_fields,
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend="kanban-followup",
            backend_raw="kanban-followup",
            completion_gate=reason_code,
        ),
    )


def _kanban_followup_pending_decision(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    task_id: str | None,
    session_id: str | None,
    cwd: str | None,
) -> StopDecision | None:
    """无 in-progress 锁时，按 dispatched-unconfirmed(pending) marker 做对账。

    对账**先问对方**（``kanban task message-status``），拿不到结论才退回 TTL 猜测：

    - 回执说 ``delivery_failed``：这条 follow-up 确定没送到。清 pending 后**按 reason 的处置**
      决定是否放行正常流程重投（``wait_for_external_condition`` / ``requires_human_confirmation``
      两类一律不自动重投，见 ``redispatch_disposition_for_terminal_delivery_failure_reason``）。
    - 回执说 ``delivered_*``：已确认进入 agent，但 UPS 还没 arm 锁（agent 尚未提交那条 prompt，
      或提交后被中断/rewind）。保持 pending active 的语义、本轮不重投——重投会造成双注入。
    - 回执仍是非终态 ``accepted_pending_submit_confirmation``：契约保证 ≤196.5s 内转终态，
      故在预算内一律等待；**超出预算**才按「对方违约、结果未知」处置。
    - 查不到结论（接口不存在 / 硬超时 / 历史 marker 无 key）：完全退回历史的 TTL 对账路径。
    - 无 pending：返回 None，照常 dispatch。
    """
    pending = read_kanban_followup_pending(task_id=task_id)
    if pending is None:
        return None
    status = kanban_followup_pending_status(pending)
    pending_path = pending.get("_marker_path")

    receipt, degraded_reason = _query_kanban_terminal_delivery_receipt(pending)
    if receipt is not None:
        receipt_fields = {
            "kanban_terminal_delivery_status": receipt.terminal_delivery_status,
            "kanban_terminal_delivery_failure_reason": receipt.terminal_delivery_failure_reason,
            "kanban_terminal_delivery_receipt_field_source": receipt.receipt_field_source,
            "kanban_terminal_delivery_redispatch_disposition": receipt.redispatch_disposition,
        }
        if receipt.is_failed:
            # 契约说终态**写一次即定**，故这条已判 delivery_failed 的投递理论上不会再落地、
            # tombstone 是多余的。仍然照常撤回：多写一枚永不被命中的 tombstone 代价为零，
            # 而少写一枚正是本切片要根治的那个缺陷——失败方向必须偏保守。
            withdrawal = _withdraw_unconfirmed_kanban_followup_dispatch(
                pending, reason="prior_dispatch_delivery_failed"
            )
            ledger.event(
                phase="gate",
                event="kanban_followup_prior_dispatch_delivery_failed",
                status="completed",
                reason_code="kanban_followup_prior_dispatch_delivery_failed",
                cwd=cwd,
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_pending_marker=pending,
                removed_kanban_followup_pending_marker_paths=withdrawal.get(
                    "removed_pending_marker_paths"
                ),
                kanban_followup_withdrawal=withdrawal,
                **receipt_fields,
            )
            blocked = _kanban_followup_withdrawal_blocks_new_dispatch(withdrawal)
            if blocked:
                return _kanban_followup_withheld_dispatch_decision(
                    ledger,
                    reason_code=blocked,
                    withdrawal=withdrawal,
                    pending=pending,
                    pending_path=pending_path,
                    receipt_fields=receipt_fields,
                    task_id=task_id,
                    session_id=session_id,
                    cwd=cwd,
                )
            # 确定没送到，但**不是每种没送到都该自动重投**：契约给每种失败 reason 配了处置，
            # ``wait_for_external_condition``（会话没起来 / 人正在那个输入框里打字 / agent 等
            # 用户答完）与 ``requires_human_confirmation``（文本可能仍残留在输入框里）两类，
            # 自动连投只会把情况变糟 —— 前者必然再失败一次，后者会把同一段文本叠成两份。
            # 放行的只有 ``allowed_with_fresh_idempotency_key``（主派发本就每轮换新 key）与
            # ``not_needed``（结果由另一条更晚的投递承载 / 本就是 RVF 自己取消的）。
            if receipt.redispatch_disposition in {
                REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION,
                REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
            }:
                return _kanban_followup_withheld_dispatch_decision(
                    ledger,
                    reason_code="kanban_followup_redispatch_disposition_withheld",
                    withdrawal=withdrawal,
                    pending=pending,
                    pending_path=pending_path,
                    receipt_fields=receipt_fields,
                    task_id=task_id,
                    session_id=session_id,
                    cwd=cwd,
                )
            return None
        # 「继续等」的分支**一律以 pending marker 自己的 TTL 为上界**。这条不变量是承重的：
        # 没有它，一条「已确认送达、但 UPS 永远不会 arm」的 pending（prompt 落地后 agent 被
        # 中断、或用户 rewind 掉了那一回合）会让每一次 Stop 都查到 delivered、每一次都 skip
        # ——那轮 review 就此**永久静默搁浅**，且看起来一切正常。任何 wait 分支都不许没有上界。
        pending_still_within_its_own_ttl = status == KANBAN_FOLLOWUP_LOCK_ACTIVE
        if receipt.is_delivered and pending_still_within_its_own_ttl:
            # 已确认进入 agent，只是还没成为一条 prompt turn（UPS 未 arm）。重投必然双注入。
            return skip_decision(
                "上一条 Cline Kanban RVF follow-up 已被确认送达 agent、但尚未成为 prompt turn"
                f"（{describe_terminal_delivery_receipt_for_operator(receipt)}）；"
                "本次 Stop 不重复 dispatch。",
                ledger,
                "kanban_followup_dispatch_in_flight",
                cwd=cwd,
                backend="kanban-followup",
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_pending_marker=pending,
                kanban_followup_pending_marker_path=pending_path,
                **receipt_fields,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw="kanban-followup",
                    completion_gate="kanban_followup_dispatch_in_flight",
                ),
            )
        if (
            receipt.is_awaiting_terminal_status
            and pending_still_within_its_own_ttl
            and not awaiting_receipt_exceeded_contract_convergence_budget(receipt)
        ):
            # 契约保证的有界收敛期内：等它，别猜。（收敛预算 196.5s 远短于 pending TTL，故这条
            # 分支正常总是先于 TTL 到期；``pending_still_within_its_own_ttl`` 只是兜住「回执没带
            # 状态戳、预算判据无从生效」那种退化输入，不让它变成一条无上界的等待。）
            return skip_decision(
                "上一条 Cline Kanban RVF follow-up 仍在 cline-kanban 承诺的有界收敛期内"
                f"（{describe_terminal_delivery_receipt_for_operator(receipt)}）；"
                "本次 Stop 暂不重复 dispatch。",
                ledger,
                "kanban_followup_dispatch_in_flight",
                cwd=cwd,
                backend="kanban-followup",
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_pending_marker=pending,
                kanban_followup_pending_marker_path=pending_path,
                **receipt_fields,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw="kanban-followup",
                    completion_gate="kanban_followup_dispatch_in_flight",
                ),
            )
        # 走到这里只剩两种情形，处置相同：
        #   ① 超出对方承诺的收敛预算仍是非终态 = 「不知道有没有送到」；
        #   ② pending 已过自己的 TTL 而 UPS 始终没 arm —— 包括「回执说送达了，但它从未成为一个
        #      回合」（落地后被中断，或用户 rewind 掉了那一回合）。
        # 两者都意味着这一轮**不会再自己走完**，且那条投递仍**可能迟到落地**，故正式撤回
        # （tombstone + 物理取消 + 放 lease + 清锁），而不是删掉记录假装没发生过。
        # 「其实已被 agent 接手」这一种不会被误拆：撤回抢不到 token 终态（既有态 consumed）
        # 时会自行放弃。
        withdrawal = _withdraw_unconfirmed_kanban_followup_dispatch(
            pending,
            reason=(
                "prior_dispatch_delivered_but_never_became_a_turn"
                if receipt.is_delivered
                else "prior_dispatch_never_converged"
            ),
        )
        ledger.event(
            phase="gate",
            event="kanban_followup_prior_dispatch_never_converged",
            status="completed",
            reason_code="kanban_followup_prior_dispatch_never_converged",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_pending_marker=pending,
            removed_kanban_followup_pending_marker_paths=withdrawal.get(
                "removed_pending_marker_paths"
            ),
            kanban_followup_withdrawal=withdrawal,
            **receipt_fields,
        )
        blocked = _kanban_followup_withdrawal_blocks_new_dispatch(withdrawal)
        if blocked:
            return _kanban_followup_withheld_dispatch_decision(
                ledger,
                reason_code=blocked,
                withdrawal=withdrawal,
                pending=pending,
                pending_path=pending_path,
                receipt_fields=receipt_fields,
                task_id=task_id,
                session_id=session_id,
                cwd=cwd,
            )
        return None

    # 以下是**降级路径**：问不到对方的真实终态（接口不存在 / 硬超时 / 历史 marker 无 key），
    # 退回历史的纯 TTL 猜测。`degraded_reason` 落进事件，使「今天这轮是猜的还是查的」可取证。
    if status == KANBAN_FOLLOWUP_LOCK_ACTIVE:
        return skip_decision(
            "上一条 Cline Kanban RVF follow-up 刚 dispatch、尚未确认落地"
            f"（token={pending.get('token') or '<unknown>'}，"
            f"channel={pending.get('delivery_channel') or '<unknown>'}，"
            f"dispatched_at={pending.get('dispatched_at') or '<unknown>'}，"
            f"终态查询降级={degraded_reason or '<unknown>'}）；"
            "本次 Stop 暂不重复 dispatch，待其落地或在途窗口超时后再处理。",
            ledger,
            "kanban_followup_dispatch_in_flight",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_pending_marker=pending,
            kanban_followup_pending_marker_path=pending_path,
            kanban_terminal_delivery_query_degraded_reason=degraded_reason,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_dispatch_in_flight",
            ),
        )
    # 降级路径下的「在途窗口已过仍未确认」：拿不到对方的结论，故这条投递**仍可能迟到落地**，
    # 与上面 never_converged 同一处置——正式撤回，而不是删掉记录假装没发生过。
    withdrawal = _withdraw_unconfirmed_kanban_followup_dispatch(
        pending, reason="prior_dispatch_unconfirmed_ttl_elapsed"
    )
    ledger.event(
        phase="gate",
        event="kanban_followup_prior_dispatch_unconfirmed",
        status="completed",
        reason_code="kanban_followup_prior_dispatch_unconfirmed",
        cwd=cwd,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_pending_marker=pending,
        removed_kanban_followup_pending_marker_paths=withdrawal.get(
            "removed_pending_marker_paths"
        ),
        kanban_followup_withdrawal=withdrawal,
        kanban_terminal_delivery_query_degraded_reason=degraded_reason,
    )
    blocked = _kanban_followup_withdrawal_blocks_new_dispatch(withdrawal)
    if blocked:
        return _kanban_followup_withheld_dispatch_decision(
            ledger,
            reason_code=blocked,
            withdrawal=withdrawal,
            pending=pending,
            pending_path=pending_path,
            receipt_fields={
                "kanban_terminal_delivery_query_degraded_reason": degraded_reason,
            },
            task_id=task_id,
            session_id=session_id,
            cwd=cwd,
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
# 整个 stranded sweep 的**总**挂钟预算（秒）。条数上界（SWEEP_MAX，默认 8）单独并不构成时间上界：
# 开启自动重投后每条 pending 都要多做一次终态查询（默认 timeout 5s），仅这一项最坏就是 8×5=40s，
# 还没算 `task list`、通知与重投子进程 —— 已经越过 Stop 链路约 30s 的总预算。故给一个显式的
# 总预算：每条 marker 开工前检查，超预算即停止扫荡（marker 全部保留，下一次 Stop 接着扫）。
# 0 = 关掉预算（回到纯条数上界的历史行为）。
KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_ENV = "RVF_KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_SECONDS"
DEFAULT_KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_SECONDS = 12.0
KANBAN_FOLLOWUP_AUTO_REDISPATCH_ENV = "RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"
KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_ENV = "RVF_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS"
DEFAULT_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS = 20
# `kanban task message` 的**短等**预算（毫秒）。默认 0 = 完全不等，保持 Stop 链路既有时延；
# 契约保证「立刻可判的失败」（如 no_active_terminal_session）不等也会当场返回终态，所以 0
# 并不牺牲诚实性，只是把「需要收敛时间的那一类」推迟到下一次 Stop 的 message-status 对账。
KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS_ENV = "RVF_KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS"
DEFAULT_KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS = 0
# pending 对账 / 撤回取消时那一次 `kanban task message-*` 子进程的 timeout 上限（秒）。
# 都是本机只读或幂等查询，正常亚秒级返回；上界压在 5s 是因为它跑在 Stop hook 的秒级预算里
# （Codex 链路总预算 30s），不能让一个不可达 / 卡死的 kanban server 把整个 Stop 拖垮。
# 超时不是投递失败——它走 `query_failed` 降级，退回历史的 TTL 对账。
KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_ENV = (
    "RVF_KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_SECONDS"
)
DEFAULT_KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_SECONDS = 5
# 自动重投的次数上界（每次都必须换新 idempotency key，故次数只能显式记账）。
KANBAN_FOLLOWUP_MAX_REDISPATCH_ATTEMPTS_ENV = "RVF_KANBAN_FOLLOWUP_MAX_REDISPATCH_ATTEMPTS"
DEFAULT_KANBAN_FOLLOWUP_MAX_REDISPATCH_ATTEMPTS = 3

# `_query_kanban_terminal_delivery_receipt` 查不到结论时的降级原因（全部**不是**投递失败）。
TERMINAL_DELIVERY_QUERY_DEGRADED_INTERFACE_UNAVAILABLE = "interface_unavailable"
TERMINAL_DELIVERY_QUERY_DEGRADED_VERDICT_NOT_YET_AVAILABLE = "verdict_not_yet_available"
TERMINAL_DELIVERY_QUERY_DEGRADED_RECORD_NOT_FOUND = "record_not_found"
TERMINAL_DELIVERY_QUERY_DEGRADED_MISSING_QUERY_KEYS = "missing_query_keys"
TERMINAL_DELIVERY_QUERY_DEGRADED_QUERY_FAILED = "query_failed"
# 搁浅判定改读 kanban session.state 时，那一次 best-effort `task list` 子进程的 timeout 上限，
# 防在 30s Codex 链路预算内卡在不可达/慢 kanban server。见 _kanban_task_list_payload。
KANBAN_FOLLOWUP_LIVENESS_LIST_TIMEOUT_ENV = (
    "CODEX_RVF_KANBAN_FOLLOWUP_LIVENESS_LIST_TIMEOUT_SECONDS"
)
DEFAULT_KANBAN_FOLLOWUP_LIVENESS_LIST_TIMEOUT_SECONDS = 10
# in-progress 锁 active 时，「认锁前先 re-engage 几次再退回静默 skip」的预算上限。
# 0 = 关掉 re-engage（永远静默放行，回到历史行为）。见 kanban_followup_in_progress_decision。
KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET_ENV = (
    "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET"
)
DEFAULT_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET = 2
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
    子进程带 timeout；重投后据**真实回执**改写/清除 pending，避免与该 task 自己的下次 Stop
    撞成双投。

    **F2a 改写了幂等键策略。** 历史实现用一枚绑定旧 token 的**稳定**键
    ``rvf-redispatch-<task>-<token>`` 来间接去重，这在新契约下是一条死路：

    - 契约第 7 条「同 key + 同 prompt hash → 直接返回当前记录、**不重投**」⇒ 稳定键根本投不出去；
    - 而每次重投都会铸新 token 并把它写进 prompt ⇒ prompt hash 变了 ⇒ 契约判**幂等冲突、退出码 1**
      ⇒ 这条 stranded marker 从此永久重投失败。

    故改为把键绑定到**本次新铸的 token**（与 prompt 内容一一对应，永不冲突），去重责任
    移交给 ``dispatch_single_flight_for_task`` 这道 per-task 派发闸（契约第 5 条要求的正是它）。

    重投前**必须先中和旧 token**（tombstone + ``message-cancel``），且中和、投递、pending 落盘
    三件事全部在 per-task 派发闸内完成 —— 见函数体内两处注释。
    """
    if not is_truthy(os.environ.get(KANBAN_FOLLOWUP_AUTO_REDISPATCH_ENV)):
        return {"redispatched": False, "reason": "disabled"}
    attempt_count = kanban_followup_pending_redispatch_attempt_count(marker)
    max_attempts = _kanban_followup_env_int(
        KANBAN_FOLLOWUP_MAX_REDISPATCH_ATTEMPTS_ENV,
        DEFAULT_KANBAN_FOLLOWUP_MAX_REDISPATCH_ATTEMPTS,
    )
    if attempt_count >= max_attempts:
        return {
            "redispatched": False,
            "reason": "redispatch-budget-exhausted",
            "redispatch_attempt_count": attempt_count,
        }
    # 先问对方：这条投递到底是什么下场？拿得到结论时，只有「确实没送到、且这种失败允许换新
    # key 重投」才继续；拿不到结论（接口不存在 / 硬超时 / 老 marker 无 key）维持历史行为。
    prior_receipt, prior_degraded_reason = _query_kanban_terminal_delivery_receipt(marker)
    if prior_receipt is not None:
        if prior_receipt.is_delivered:
            return {
                "redispatched": False,
                "reason": "already-delivered",
                "terminal_delivery_status": prior_receipt.terminal_delivery_status,
            }
        if prior_receipt.is_awaiting_terminal_status and not (
            awaiting_receipt_exceeded_contract_convergence_budget(prior_receipt)
        ):
            return {
                "redispatched": False,
                "reason": "still-within-contract-convergence-budget",
                "terminal_delivery_status": prior_receipt.terminal_delivery_status,
            }
        if (
            prior_receipt.is_failed
            and prior_receipt.redispatch_disposition
            != REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY
        ):
            # 「文本可能仍残留在输入框里」「人正在打字」「等用户答完」这几类，自动连投只会
            # 把情况变糟。marker 仍在 + stranded 通知仍会把它浮现给用户，由人来决定。
            #
            # ``redispatch_not_needed`` 曾被和 ``allowed_with_fresh_idempotency_key`` **并列**
            # 放进「继续重投」，与该常量的语义（不必重投：结果已由另一条更晚的投递承载，或本就是
            # RVF 自己取消的）**正好相反**：于是 ``superseded_by_later_delivery`` 会被再投一次，
            # 而那条「更晚的合法投递」可能反过来被新投的这条撞成 superseded。允许继续的只有
            # 「换新 key 就能干净重投」这一种。
            return {
                "redispatched": False,
                "reason": (
                    "redispatch-not-needed"
                    if prior_receipt.redispatch_disposition == REDISPATCH_DISPOSITION_NOT_NEEDED
                    else "redispatch-disposition-withheld"
                ),
                "terminal_delivery_status": prior_receipt.terminal_delivery_status,
                "terminal_delivery_failure_reason": (
                    prior_receipt.terminal_delivery_failure_reason
                ),
                "redispatch_disposition": prior_receipt.redispatch_disposition,
            }
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
    # 幂等键绑定**本次新铸的 token**：prompt 正文里字面含这个 token，故 key 与 prompt hash
    # 一一对应，永不触发契约的幂等冲突；而「不要并发投同一个 task」由下面的 per-task 派发闸
    # 负责，不再由 key 兼职（历史上那枚稳定键在新契约下等于「投不出去 + 之后永久冲突」）。
    idem = f"rvf-redispatch-{safe_token(task_id)}-{fresh_prep.token}"
    redispatch_timeout = _kanban_followup_env_float(
        KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_REDISPATCH_TIMEOUT_SECONDS,
    )
    # per-task 派发闸必须一路裹到「pending 已按新 token 落盘」为止。只裹住投递那一句是不够的：
    # 闸一放开、pending 还没写，另一个 Stop 就能立刻通过「无 pending」判据进闸再投一条新 key，
    # 于是先到的那条被判 ``superseded_by_later_delivery`` —— 正是这道闸要防的事。
    with kanban_followup_dispatch_single_flight_for_task(task_id) as dispatch_slot_acquired:
        if not dispatch_slot_acquired:
            return {
                "redispatched": False,
                "reason": "dispatch-single-flight-busy",
                "token": fresh_prep.token,
            }
        # 重投前先**中和旧 token**（tombstone + 物理取消），且只在闸内做。历史实现直接铸新 token
        # 就投，旧那条仍可迟到落地并与新一轮并发 —— 同 task 对账路径明明会走撤回，这条路径却把
        # 整套止血绕过去了。这里刻意**只中和、不清理**：lease / 锁 / pending 属于**同一轮**
        # review，本次重投正是要让它继续，绝不能顺手放掉。
        neutralization = _neutralize_kanban_followup_dispatch_token(
            marker, reason="superseded_by_stranded_redispatch"
        )
        if neutralization.get("neutralization") == KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED:
            # 旧 token 已被 UPS 接手：那轮 review 正在跑，重投等于投出第二条。
            return {
                "redispatched": False,
                "reason": "prior-dispatch-already-consumed",
                "token": token,
                "prior_dispatch_neutralization": neutralization,
            }
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
        except KanbanFollowupDeliveryVerdictNotYetAvailableError as exc:
            # CLI 硬超时（exit 124 + 空 stdout）= 「还没结论」，**不是**投递失败：这条消息可能
            # 已被受理。绝不能当普通失败退出——那会把 pending 留在**旧** token 上，而 prompt 里
            # 已经是新 token，UPS 落地时拿新 token 去清旧 token 的 pending 会被防误清 guard 拒绝
            # ⇒ pending 永久锁死。故照未确认路径把 pending 改写到新 token。
            message_payload = exc.provisional_payload()
            verdict_pending = True
        except subprocess.TimeoutExpired:
            return {
                "redispatched": False,
                "reason": "redispatch-timeout",
                "token": fresh_prep.token,
                "prior_dispatch_neutralization": neutralization,
            }
        except Exception as exc:
            return {
                "redispatched": False,
                "reason": "redispatch-failed",
                "error": f"{type(exc).__name__}: {exc}",
                "prior_dispatch_neutralization": neutralization,
            }
        else:
            verdict_pending = False
        new_message_id = message_payload.get("message_id")
        new_channel = _kanban_followup_delivery_channel(new_message_id)
        new_receipt = classify_cline_kanban_terminal_delivery_receipt(message_payload)
        if new_receipt.is_delivered:
            # 已确认进入 agent → 清掉 pending（用旧 token 过 clear 的防误清 guard）。
            clear_kanban_followup_pending(task_id=task_id, token=token)
        else:
            # 未确认**与已确定失败走同一条路**：都把 pending 改写到**新** token。
            #
            # 曾想给「已确定失败」单开一条「什么都不写」的分支（理由是没必要重置在途窗口），那是
            # 错的，两层原因：① 不写就等于 ``redispatch_attempt_count`` 永不递增 ⇒ 重投次数上界形同
            # 虚设，同一条 stranded marker 会在每个 renotify 窗口被无限重投；② 旧 token 留在 marker 上
            # 而 prompt 里已经是新 token，一旦那条投递后来还是被送出去（例如
            # ``submit_confirmation_budget_exhausted`` 的残留文本被人按了回车），UPS 会拿新 token 去
            # 清一个记着旧 token 的 pending —— 防误清 guard 直接拒绝 ⇒ pending 永久锁死。
            #
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
                    prompt_path=message_payload.get("prompt_path") or marker.get("prompt_path"),
                    kanban_project_path=project_path,
                    kanban_task_title=marker.get("kanban_task_title"),
                    kanban_task_title_source=marker.get("kanban_task_title_source"),
                    origin_transcript_path=marker.get("origin_transcript_path"),
                    idempotency_key=message_payload.get("idempotency_key") or idem,
                    terminal_delivery_status=new_receipt.terminal_delivery_status,
                    terminal_delivery_failure_reason=new_receipt.terminal_delivery_failure_reason,
                    terminal_delivery_status_updated_at=(
                        new_receipt.terminal_delivery_status_updated_at
                    ),
                    # 契约第 7 条让「重投了几次」不再能从 key 反推，故必须显式累加记账。
                    redispatch_attempt_count=attempt_count + 1,
                )
            except Exception:
                pass
    return {
        "redispatched": True,
        "reason": "redispatched",
        "delivery_channel": new_channel,
        "token": fresh_prep.token,
        "idempotency_key": idem,
        "redispatch_attempt_count": attempt_count + 1,
        "terminal_delivery_status": new_receipt.terminal_delivery_status,
        "terminal_delivery_failure_reason": new_receipt.terminal_delivery_failure_reason,
        "terminal_delivery_verdict_not_yet_available": verdict_pending,
        "prior_dispatch_neutralization": neutralization,
        "prior_terminal_delivery_query_degraded_reason": prior_degraded_reason,
    }


def _kanban_task_list_payload(project_path: str) -> dict[str, Any] | None:
    """Best-effort 拉一次 kanban ``task list``（只读、带 timeout），失败返回 None。

    用 codex_stop 既有的「shell cline_kanban_client.py list」惯用法（见 lookup_kanban_task_workspace），
    但带显式 timeout——本调用挂在每次 Stop 的搁浅 sweep 上，不能让不可达/慢 kanban server 卡满 30s 预算。
    """
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    timeout = _kanban_followup_env_float(
        KANBAN_FOLLOWUP_LIVENESS_LIST_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_LIVENESS_LIST_TIMEOUT_SECONDS,
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(client), "list", "--repo", project_path, "--task-cmd", task_cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _kanban_task_session_liveness(
    task_id: str,
    project_path: Any,
    list_cache: dict[str, dict[str, Any] | None],
) -> str:
    """读 kanban ``task list`` 的 session 真相，判某 task 的 follow-up 投递是否搁浅。

    返回 ``"stopped"`` / ``"alive"`` / ``"unknown"``：
    - ``"stopped"``：目标 session 已退出（``exitCode`` 非空）、停在 user review/input gate
      （``awaiting_review``）或无 live turn（``idle``）——这些 session 不会在无人干预下消费排队的
      follow-up，即已搁浅；应按搁浅处理，无需等 15min TTL。
    - ``"alive"``：session 处于活跃 turn（``running``）——可能正在消费本 follow-up，不应升级
      （留给真实投递的 UPS-arm 清 marker），即便 marker 已 TTL-stale 也不报，砍掉「活着却因超时被误升级」。
    - ``"unknown"``：project_path 缺失 / list 不可达 / task 不在 list / session 缺失或 state 不识别
      ——调用方退回原 TTL 行为，绝不比现状更差。

    ``state`` 是这里的判别真相而非 ``liveness`` 字段：``awaiting_review`` 的 ``liveness`` 也是
    ``live``（进程在、但停在 user gate、不会消费），故只能按 ``state`` 区分「会消费 vs 不会消费」。
    list 结果按 project_path 缓存在传入的 ``list_cache``（单次 sweep 内每 project 至多拉一次）。
    """
    if not (isinstance(project_path, str) and project_path.strip()):
        return "unknown"
    if project_path not in list_cache:
        list_cache[project_path] = _kanban_task_list_payload(project_path)
    payload = list_cache.get(project_path)
    if not isinstance(payload, dict):
        return "unknown"
    task = task_payload_from_list(payload, task_id)
    if not isinstance(task, dict):
        return "unknown"
    if task_session_exit_code(task) is not None:
        return "stopped"
    state = task_session_state(task)
    if state in {"awaiting_review", "idle"}:
        return "stopped"
    if state == "running":
        return "alive"
    return "unknown"


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
        sweep_budget_seconds = _kanban_followup_env_float(
            KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_ENV,
            DEFAULT_KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_SECONDS,
        )
        sweep_deadline = (
            time.monotonic() + sweep_budget_seconds if sweep_budget_seconds > 0 else None
        )
        markers = iter_kanban_followup_pending()
        now_ts = datetime.now(timezone.utc).timestamp()
        processed = 0
        # 单次 sweep 内按 project_path 缓存 kanban `task list` payload，每 project 至多拉一次。
        session_list_cache: dict[str, dict[str, Any] | None] = {}
        for marker in markers:
            if processed >= sweep_max:
                break
            # 总预算闸：每条 marker **开工前**检查一次。条数上界管不住时间——单条最坏要跑
            # `task list`(10s) + 终态查询(5s) + 通知(10s) + 重投子进程(20s)，8 条足以吃穿整个
            # Stop 预算。停下不丢事：所有 marker 都保留，下一次任意会话的 Stop 会接着扫。
            if sweep_deadline is not None and time.monotonic() >= sweep_deadline:
                ledger.event(
                    phase="gate",
                    event="kanban_followup_stranded_sweep_budget_exhausted",
                    status="completed",
                    reason_code="kanban_followup_stranded_sweep_budget_exhausted",
                    kanban_followup_stranded_sweep_processed=processed,
                    kanban_followup_stranded_sweep_budget_seconds=sweep_budget_seconds,
                )
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
            # 真相驱动搁浅判定：优先读 kanban session.state（投递落地与否的地面真相），
            # 15min 挂钟 TTL 仅作拿不到 state 时的降级兜底。见 _kanban_task_session_liveness。
            liveness = _kanban_task_session_liveness(
                marker_task_id, marker.get("kanban_project_path"), session_list_cache
            )
            if liveness == "alive":
                # 目标 session 仍在活跃 turn（可能正消费本 follow-up）——即便 marker 已 TTL-stale 也不报，
                # 留给真实投递的 UPS-arm 清 marker；砍掉「活着却因超时被误升级 / 误重投」的假阳。
                continue
            if liveness == "unknown":
                # 拿不到可信 state（task 不在 list / list 不可达）→ 退回原 TTL 行为。
                if kanban_followup_pending_status(marker, now_ts=now_ts) == KANBAN_FOLLOWUP_LOCK_ACTIVE:
                    continue
            # liveness == "stopped"：session 已停在 user gate / idle / 已退出 → 当场即搁浅，不等 TTL。
            # 仍先走下方 late-consumption 精修，覆盖「已落地但 UPS-arm 漏清」的假阳。
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
            # park 泄漏兜底（proposer §②，量级对齐 pending 15min TTL）：stranded = 派发成功但 resume
            # 静默丢投 → 该 task 的 PostToolUse self-park 已泄漏。幂等 unpark 清掉它，避免泄漏的 park
            # 误抑制该 task 下一次真实 Stop 的通知（redispatch 若成功，其 followup 落地不依赖 park）。
            stranded_unparked = _unpark_stranded_task(
                marker.get("kanban_project_path"), marker_task_id
            )
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
                kanban_followup_stranded_unparked=stranded_unparked,
                kanban_followup_stranded_session_liveness=liveness,
            )
            processed += 1
    except Exception:
        # 对齐既有 best-effort：扫荡的任何异常都不得影响本次 Stop 的主流程。
        pass


def awaiting_dispatched_agent_decision(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | None,
) -> StopDecision | None:
    """RVF 内外统一 shield：主 agent 本轮 Stop 的原因是「正在等待某个已派发的后台/外部 agent」时，
    不触发新 RVF。

    与 in-progress 锁（``kanban_followup_in_progress_decision``，管 RVF **内**等已派发 reviewer）
    互补：本闸管 RVF **外 / 实现期**那条——主 agent 还没进 RVF、实现到一半派了个后台 agent（典型 =
    delegate-to-cursor WRITE 模式留脏树）后纯文本停下等它。没有本闸，``evaluate_stop_event`` 会因
    无 in-progress 锁 / 无 pending 落到 dirty gate，对半成品工作树 mint 一个不该有的新 RVF + 自注入。

    signal 是 dispatch 层写的 wait-on marker（见 rvf_awaiting_dispatched_agent_marker），不是 agent
    文本 sentinel（Stop hook 在 Claude 下读不到主 agent 自己最后一轮文本）。只对 ACTIVE marker 跳过，
    STALE 由 reader 惰性清后放行 → masking 上界 = marker TTL。

    放在 in-progress 闸**之上**：合法 parked 的主 agent 应先被认出「还在等」，再轮到 in-progress 的
    reengage/nudge。静默 ``{"continue": true}`` skip——不打断 agent 续等、不 nudge、不 force-continue
    （harness 会在后台 agent 完成时重新唤起主 agent）。
    """
    session_id = session_hook_id_from_event(event)
    if not session_id:
        return None
    active = iter_active_awaiting_dispatched_agent_for_session(session_id)
    if not active:
        return None
    dispatched_ids = [marker.get("dispatched_agent_id") or "<unknown>" for marker in active]
    dispatchers = sorted(
        {marker.get("dispatcher") for marker in active if marker.get("dispatcher")}
    )
    return skip_decision(
        "主 Agent 本轮 Stop 的原因是正在等待已派发的后台/外部 agent"
        f"（dispatched_agent_id={', '.join(dispatched_ids)}；"
        f"dispatcher={', '.join(dispatchers) or '<unknown>'}）；"
        "本次 Stop 不触发 RVF——agent 只是 park 等待异步派发结果、并未完成可审单元。"
        "派发方消费完结果后会 clear 该 awaiting marker（或 TTL 到期自动失效），届时正常 Stop 照常触发 RVF。",
        ledger,
        "awaiting_dispatched_agent",
        cwd=cwd,
        session_id=session_id,
        awaiting_dispatched_agent_ids=dispatched_ids,
        awaiting_dispatched_agent_dispatchers=dispatchers,
        awaiting_dispatched_agent_markers=active,
    )


KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_ENV = (
    "RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_SECONDS"
)
DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_SECONDS = 60
# 等待态的**终止界**。护住一轮 bootstrap 等待是对的，但无界地护住它只是把 32cad 的「反复重开」
# 换成同样糟糕的「永久驻留」：agent 若始终没能完成吸收（skill 不可用、冲突无人解、用户已转去
# 做别的事），锁会一直被复活、Stop 会一直被 block。故给两条界：
#   - re-engage 预算用尽 → 退回静默 skip（不再硬挡 agent 干停，仍留 user-facing 诊断）；
#   - 复活次数用尽 → 不再复活，让既有 stale 语义接手清锁，本轮被明确放弃而不是无限驻留。
KANBAN_FOLLOWUP_BOOTSTRAP_REENGAGE_BUDGET_ENV = (
    "RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REENGAGE_BUDGET"
)
DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REENGAGE_BUDGET = 3
KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET_ENV = "RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET"
DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET = 3
# 一次 prepare 重放的**并发**去重窗口。占位在起 replay **之前**抢、在这次
# `replay_prepare_for_gate` 收尾时释放，所以窗口只需要覆盖「一个还活着的持有者最长可能持有它
# 多久」= 重放预算 + 子进程强杀余量（`rvf_bootstrap_gate.REPLAY_SUBPROCESS_KILL_GRACE_SECONDS`）
# + 一段余量；余量的用途是在持有者**崩在半路**（Stop hook 进程被 harness 预算杀掉、机器休眠）时
# 让下一个 Stop 接手，而不是让占位永久堵死本轮。
#
# 刻意**不再**是 15min 量级：那个默认值来自「重放超时后后台 prepare worker 仍在写同一个 run
# 目录、故整个窗口内不得再起第二次」的旧立论，而重放子进程化之后它已不成立（见下面
# receipt_valid 分支里对 timeout 的处理）。窗口太长的代价是实打实的：一个本可立刻重试的可恢复
# 轮次会被白挡满一整个窗口。
KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_ENV = (
    "RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_SECONDS"
)
KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_MARGIN_SECONDS = 60


def _kanban_followup_bootstrap_replay_in_flight_window_seconds() -> float:
    """在途重放占位的接手窗口（秒）：默认按重放的**硬上限**推导，env 可覆盖。

    推导而不是写死一个常数，是为了让「窗口 ≥ 一次合法重放的最长时长」这条不变式在
    `RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_SECONDS` 被调大时自动成立——否则窗口会短于
    一次仍在跑的重放，另一个 Stop 就能就地接手并起第二次 prepare，正是占位要防的事。
    """
    replay_timeout = _kanban_followup_env_int(
        KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_SECONDS,
    )
    derived_default = int(
        replay_timeout
        + rvf_bootstrap_gate.REPLAY_SUBPROCESS_KILL_GRACE_SECONDS
        + KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_MARGIN_SECONDS
    )
    return float(
        _kanban_followup_env_int(
            KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_ENV, derived_default
        )
    )


BOOTSTRAP_REPLAY_IN_FLIGHT_REASON = "bootstrap_replay_in_flight"
BOOTSTRAP_REPLAY_INCOMPLETE_REASON = "bootstrap_replay_incomplete"
# 这两条等待的共同前提是「receipt 已验证通过、只差 prepare 收尾」，与「还没吸收 base」相反：
# 给 agent 的定向指令必须换成「别再跑同步 skill / 别动 receipt」，否则重跑会产出另一份 receipt，
# 下一轮按 digest 判 tampered（RVF-002 把 digest 落盘之后这条路径才真正生效）。
BOOTSTRAP_REPLAY_PENDING_REASONS = frozenset(
    {BOOTSTRAP_REPLAY_IN_FLIGHT_REASON, BOOTSTRAP_REPLAY_INCOMPLETE_REASON}
)


def _bootstrap_replay_error_text(replay: dict[str, Any] | None) -> str:
    """从一次重放结果里取出人类可读的 error（缺省空串）。"""
    if not isinstance(replay, dict):
        return ""
    error = replay.get("error")
    return str(error) if error else ""


def _bootstrap_replay_in_flight_reconciliation(
    reconciliation: rvf_bootstrap_gate.BootstrapGateReconciliation,
    *,
    age_seconds: float,
    window_seconds: float,
) -> rvf_bootstrap_gate.BootstrapGateReconciliation:
    """把「**另一个** Stop 此刻正在跑本轮重放」渲染成一条可区分的等待结论（不 resume、不清锁）。

    与 ``bootstrap_replay_incomplete`` 分开：那条是「本进程起的这次重放已经收尾但没成功、下一次
    Stop 可以立刻再试」，这条是「占位被别人握着，本轮**刻意不起第二次**」——占位只在一次
    `replay_prepare_for_gate` 调用期间存在，所以它在途 = 真的有另一个进程正在写这个 run。
    """
    return rvf_bootstrap_gate.BootstrapGateReconciliation(
        outcome=rvf_bootstrap_gate.OUTCOME_STILL_WAITING,
        phase=rvf_bootstrap_gate.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
        reason=BOOTSTRAP_REPLAY_IN_FLIGHT_REASON,
        diagnostic=(
            "bootstrap receipt 已通过校验，另一个 Stop 此刻正在跑本轮 prepare 重放"
            f"（占位已在途 {age_seconds:.0f}s / 接手窗口 {window_seconds:.0f}s）；"
            "为避免同一个 run 目录被并发重复写入，本次不再启动第二次重放。"
        ),
        resume_run_id=reconciliation.resume_run_id,
        resume_run_dir=reconciliation.resume_run_dir,
    )


def _kanban_followup_bootstrap_gate_decision(
    ledger: RunLedger,
    *,
    marker: dict[str, Any],
    task_id: str | None,
    session_id: str | None,
    cwd: str | None,
    store_expired: bool,
) -> tuple[bool, StopDecision | None]:
    """in-progress 锁上挂着 bootstrap 等待 gate 时的读侧判定。返回 ``(handled, decision)``。

    ``handled=False`` = 这把锁与 bootstrap 无关，调用方继续走原有 active / stale 逻辑。

    这里同时堵住 32cad 家族的根因：等待期跨过 1h 锁 TTL 时，原 stale 分支会无条件清锁 +
    ``return None``，让常规 gate 把这一轮当成「没人在跑」而重铸 token / 重开 run。改为先按
    gate 重新判定——仍在等 / 冲突未解 / 证据不可信一律 **revive 锁并定向 re-engage**（既不清、
    也不新开）；receipt 有效才按**原 run** 重放 prepare；只有工件确实丢了才清锁回 fresh。
    """
    gate = rvf_bootstrap_gate.gate_of(marker)
    if not rvf_bootstrap_gate.is_waiting_gate(gate):
        return False, None
    assert gate is not None  # narrow for readers
    marker_path = marker.get("_marker_path")
    reconciliation = rvf_bootstrap_gate.reconcile_bootstrap_gate(
        gate, store_expired=store_expired
    )

    def _event(name: str, **fields: Any) -> None:
        # reconciliation 的字段一律带 ``bootstrap_`` 前缀落账：它自带 ``phase``/``reason``，
        # 与 ledger.event 自己的 phase / reason_code 形参同名，直接 spread 会撞成重复关键字。
        ledger.event(
            phase="gate",
            event=name,
            status="completed",
            reason_code=f"kanban_followup_bootstrap_{reconciliation.reason}",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker_path=marker_path,
            bootstrap_gate=dict(gate),
            **{f"bootstrap_{key}": value for key, value in reconciliation.to_dict().items()},
            **fields,
        )

    def _artifacts_lost(event_name: str) -> tuple[bool, StopDecision | None]:
        # 等待期工件已消失 → 这一轮无从恢复（也无在途工作可撞）。清锁回 fresh 是安全的，
        # 并把丢失原因显式落账，避免它表现成一次无声的「锁自己没了」。
        removed = clear_kanban_followup_lock(
            task_id=task_id,
            session_id=session_id,
            repo=kanban_followup_lock_repository_anchor(cwd=cwd, marker=marker),
        )
        _event(event_name, consumed_kanban_followup_in_progress_marker_paths=removed)
        return True, None

    if reconciliation.outcome == rvf_bootstrap_gate.OUTCOME_ARTIFACT_LOST:
        return _artifacts_lost("kanban_followup_bootstrap_gate_artifacts_lost")

    if reconciliation.outcome == rvf_bootstrap_gate.OUTCOME_RECEIPT_VALID:
        # ①「继续持有等待态」前先把校验通过的 receipt 路径/digest 盖回锁上的 gate。
        # `build_bootstrap_gate` 造 gate 时 receipt 还不存在（digest 恒为 None），没有这一步，
        # `bootstrap_receipt_digest_changed` 那条篡改判据在真实路径上永远不可达：后续每一轮
        # 都只做身份校验，一份被替换但仍能通过身份校验的 receipt 不会被判成篡改。
        gate = rvf_bootstrap_gate.gate_with_verified_receipt(gate, reconciliation)
        stamp_kanban_followup_bootstrap_gate(marker, gate=gate)

        # ② 起重放**之前**先原子 claim，收尾时释放（见下面的 elif claimed 分支）。抢不到 =
        # 另一个 Stop 此刻正握着占位跑这一轮重放，本轮绝不能再起第二次（同一 prep / 同一 run_id
        # 会并发重复写同一个 run 目录）。占位必须**先于** replay 且落在 marker payload 之外：
        # 事后才写标记的话，两个并发 Stop 会同时读到「无在途标记」并各自进入 replay；
        # 而写进 marker 的话，另一个 Stop 上面 ① 的整份读-改-写会把刚落的占位抹掉。
        in_flight_window = _kanban_followup_bootstrap_replay_in_flight_window_seconds()
        replay: dict[str, Any] | None = None
        replay_status: str | None = None
        claimed, in_flight_age = claim_kanban_followup_bootstrap_replay_in_flight(
            marker, window_seconds=in_flight_window
        )
        if not claimed:
            reconciliation = _bootstrap_replay_in_flight_reconciliation(
                reconciliation,
                age_seconds=in_flight_age if in_flight_age is not None else 0.0,
                window_seconds=in_flight_window,
            )
        else:
            # 抢到了（含「上一枚占位已过接手窗口 → 就地接手」：那个持有者已经崩在半路，
            # 允许再试一次；prep 若已被另一个 Stop 的重放推进为 completed，本次重放幂等短路直接成功）。
            replayed = rvf_bootstrap_gate.replay_prepare_for_gate(
                gate,
                timeout_seconds=float(
                    _kanban_followup_env_int(
                        KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_ENV,
                        DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_TIMEOUT_SECONDS,
                    )
                ),
            )
            replay = replayed if isinstance(replayed, dict) else None
            replay_status = replay.get("status") if replay is not None else None
        if replay_status == rvf_bootstrap_gate.REPLAY_STATUS_COMPLETED:
            assert replay is not None  # narrow for readers
            # bootstrap 收尾 → 进入 REVIEWING：摘掉 gate、锁回 in_progress（**不清锁**，
            # 否则常规 gate 会把这一轮当没人跑而重开），并 force-continue 让 agent 接着审。
            if store_expired:
                # 本轮是从 STALE 分支进来的：只把 state 改回 in_progress **不够**。
                # `stamp_bootstrap_gate` 是读-改-写，会原样保留已经过期的 ``expires_at``，
                # 于是下一次 Stop 仍判 stale；而此时 gate 已被摘掉 → 本函数 handled=False →
                # 交还既有 stale 分支 clear_kanban_followup_lock + return None → 常规 gate
                # 判「没人在跑」→ 为同一个仍在继续的 run 再开一轮 review。正是这套改动本来
                # 要根治的 32cad 失败模式在这条路径上原样复活。故摘 gate 的同时必须把锁续期
                # 回 active：下一次 Stop 读到的必须是一把 **active 的 in_progress 锁**。
                revive_kanban_followup_lock(marker)
            stamp_kanban_followup_bootstrap_gate(
                marker, gate=None, state=KANBAN_FOLLOWUP_LOCK_STATE_IN_PROGRESS
            )
            _event(
                "kanban_followup_bootstrap_gate_resolved",
                resumed_run_id=replay.get("run_id"),
                resumed_run_dir=replay.get("run_dir"),
            )
            return True, reengage_decision(
                "local base 已吸收、bootstrap receipt 校验通过，本轮 RVF 的 prepare 已就绪"
                f"（run_id={replay.get('run_id') or '<unknown>'}，"
                f"run_dir={replay.get('run_dir') or '<unknown>'}）。"
                "请继续完成**这一轮**的 review 并 handoff，**不要**新开 review。",
                ledger,
                reason_code="kanban_followup_bootstrap_resumed",
                cwd=cwd,
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_in_progress_marker_path=marker_path,
                active_rvf_run_id=replay.get("run_id"),
                active_rvf_run_dir=replay.get("run_dir"),
                **stop_hook_rvf_state_fields(
                    phase="complete",
                    backend="kanban-followup",
                    backend_raw="kanban-followup",
                    completion_gate="kanban_followup_bootstrap_resumed",
                ),
            )
        elif replay_status == rvf_bootstrap_gate.REPLAY_STATUS_ARTIFACT_LOST:
            # prep 文件已不可用（TTL 300s ≪ 一次 bootstrap 等待，且 sweep_stale 会删它）：
            # 这一轮**没有**可恢复的在途工作，继续硬等只会白白熬光 re-engage / revive 预算。
            # 走与 reconcile 级 artifact_lost 同一条「显式诊断 + 清锁安全重来」路径。
            reconciliation = rvf_bootstrap_gate.BootstrapGateReconciliation(
                outcome=rvf_bootstrap_gate.OUTCOME_ARTIFACT_LOST,
                phase=rvf_bootstrap_gate.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
                reason="bootstrap_replay_artifact_lost",
                diagnostic=(
                    "bootstrap receipt 已通过校验，但本轮 prepare 的 prep 工件已不可用"
                    f"（{_bootstrap_replay_error_text(replay) or '<unknown>'}）；"
                    "本轮无从恢复，已清锁以便安全重来。"
                ),
            )
            return _artifacts_lost("kanban_followup_bootstrap_gate_replay_artifact_lost")
        elif claimed:
            # 抢到占位、也确实起了重放，但它既没 completed 也没 artifact_lost（timeout / failed /
            # 又撞回 waiting_local_base_bootstrap，或契约外地没回传可分类 status）：一律
            # 「**释放占位** + 维持等待、绝不清锁」，让下一次 Stop 能立刻重试同一个 run 的 prepare。
            #
            # timeout 也走这里（而不是扣着占位硬扛满一个替换窗口）：重放跑在独立子进程里
            # （`rvf_bootstrap_gate._replay_prepare_in_subprocess`）——内层预算到点由子进程写完结果
            # `os._exit`（那个非守护 prepare worker 线程随进程一起消失），外层硬上限到点按**进程组**
            # SIGKILL，两种收尾都不留在途的 prepare 写者。原先「`prepare_run_from_prep_file` 只
            # shutdown(wait=False)、后台 worker 仍在写同一个 run 目录」的保留立论随子进程化失效；
            # 并发去重本来就由 replay **之前**那次 O_EXCL claim 负责，与超时后是否保留占位无关。
            # 继续扣着占位只会把一轮本可立刻重试的可恢复失败白拖满窗口，还给 agent 一条事实错误的
            # 「仍在后台进行」。超时后真正的残留只剩被 orphan 的 git 孙进程（index.lock 量级、秒级
            # 自消；真撞上也只是下一次重放再报一次可重试失败），远小于任何有意义的窗口。
            release_kanban_followup_bootstrap_replay_in_flight(marker)
            replay_error = _bootstrap_replay_error_text(replay)
            reconciliation = rvf_bootstrap_gate.BootstrapGateReconciliation(
                outcome=rvf_bootstrap_gate.OUTCOME_STILL_WAITING,
                phase=rvf_bootstrap_gate.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
                reason=BOOTSTRAP_REPLAY_INCOMPLETE_REASON,
                diagnostic=(
                    "bootstrap receipt 已通过校验，但本轮 prepare 重放未完成"
                    f"（status={replay_status or '<unknown>'}"
                    f"{'，' + replay_error if replay_error else ''}）；"
                    "重放已收尾、没有在途写者，下一次 Stop 会立刻重试同一个 run 的 prepare。"
                ),
                resume_run_id=reconciliation.resume_run_id,
                resume_run_dir=reconciliation.resume_run_dir,
            )

    # still_waiting / conflict / tampered（含重放未成）：**既不 resume 也不 clear**。
    # 锁过期就地复活，让这一轮继续被保护，然后给一条模型可见的定向指令。
    #
    # 「等的是什么」必须在**所有**下游出口之前定下来：receipt 已验证通过、只差 prepare 收尾
    # （replay_pending）与「base 还没吸收」是方向完全相反的两种等待，任何一条出口把前者当后者
    # 渲染，就是在教人去再跑一次同步 skill —— 那会产出第二份 receipt，下一轮按 digest 判 tampered
    # 把这一轮锁死。
    replay_pending = reconciliation.reason in BOOTSTRAP_REPLAY_PENDING_REASONS
    revive_budget = _kanban_followup_env_int(
        KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET_ENV,
        DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET,
    )
    if store_expired:
        if kanban_followup_lock_revive_count(marker) >= revive_budget:
            # 复活预算耗尽：本轮 bootstrap 迟迟没完成，交还给既有 stale 语义清锁并明确放弃，
            # 而不是让一把永不过期的等待锁长期挡住这个 task。
            _event(
                "kanban_followup_bootstrap_gate_revive_budget_exhausted",
                bootstrap_revive_budget=revive_budget,
            )
            return False, None
        revive_kanban_followup_lock(marker)
    reengage_budget = _kanban_followup_env_int(
        KANBAN_FOLLOWUP_BOOTSTRAP_REENGAGE_BUDGET_ENV,
        DEFAULT_KANBAN_FOLLOWUP_BOOTSTRAP_REENGAGE_BUDGET,
    )
    if kanban_followup_bootstrap_reengage_count(marker) >= reengage_budget > 0:
        # 定向指令已给够却仍没进展：不再硬挡 agent 干停（防死循环），但等待态原样保留——
        # 下一轮 Stop 若发现 receipt 已就绪，仍会按原 run 接续。
        _event(
            "kanban_followup_bootstrap_gate_reengage_budget_exhausted",
            bootstrap_reengage_budget=reengage_budget,
        )
        return True, skip_decision(
            (
                # replay_pending：base 早已吸收、receipt 也已校验通过，只差 prepare 收尾。这条
                # 出口落进 ledger summary/events，是事后排障的记录——写成「需要先跑同步 skill」
                # 会把排障的人推向本函数下方 `bootstrap_replay_pending_reengage_reason` 明确
                # 禁止的动作（再产一份 receipt → 下一轮判 tampered 锁死本轮）。
                "本轮 Cline Kanban RVF follow-up 的 pre-review local-base bootstrap **已完成**"
                f"（run_id={marker.get('run_id') or '<unknown>'}，"
                f"local base `{gate.get('local_base_ref') or '<unknown>'}` 已吸收、"
                "bootstrap receipt 已校验通过，只差本轮 prepare 收尾；"
                "**不要**再跑一次同步 skill 或改动 receipt）；"
                if replay_pending
                else "本轮 Cline Kanban RVF follow-up 仍卡在 pre-review local-base bootstrap"
                f"（run_id={marker.get('run_id') or '<unknown>'}，"
                f"需要先跑 `{gate.get('required_sync_skill') or '<sync skill>'}` "
                f"吸收 local base `{gate.get('local_base_ref') or '<unknown>'}`）；"
            )
            + "定向提示预算已用尽，本次 Stop 不再打断，也不会新开一轮 RVF。",
            ledger,
            "kanban_followup_waiting_local_base_bootstrap",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker_path=marker_path,
            bootstrap_phase=reconciliation.phase,
            active_rvf_run_id=marker.get("run_id"),
            active_rvf_run_dir=marker.get("run_dir"),
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_waiting_local_base_bootstrap",
            ),
        )
    bumped = bump_kanban_followup_bootstrap_reengage_count(marker)
    _event(
        (
            "kanban_followup_bootstrap_gate_replay_pending"
            if replay_pending
            else "kanban_followup_bootstrap_gate_waiting"
        ),
        lock_revived=store_expired,
        bootstrap_reengage_count=bumped,
        bootstrap_reengage_budget=reengage_budget,
    )
    return True, reengage_decision(
        (
            rvf_bootstrap_gate.bootstrap_replay_pending_reengage_reason(
                gate,
                reconciliation,
                resume_hint=(
                    "本轮会在下一次 Stop 自动接续 prepare 并进入 review；"
                    "在那之前 RVF 不会另开一轮。"
                ),
            )
            if replay_pending
            else rvf_bootstrap_gate.bootstrap_waiting_reengage_reason(
                gate,
                reconciliation,
                resume_hint=(
                    "完成后本轮会在下一次 Stop 自动接续 prepare 并进入 review；"
                    "在那之前 RVF 不会另开一轮。"
                ),
            )
        ),
        ledger,
        reason_code=f"kanban_followup_bootstrap_{reconciliation.reason}",
        cwd=cwd,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_in_progress_marker_path=marker_path,
        bootstrap_phase=reconciliation.phase,
        bootstrap_outcome=reconciliation.outcome,
        active_rvf_run_id=marker.get("run_id"),
        active_rvf_run_dir=marker.get("run_dir"),
        **stop_hook_rvf_state_fields(
            phase="complete",
            backend="kanban-followup",
            backend_raw="kanban-followup",
            completion_gate="kanban_followup_waiting_local_base_bootstrap",
        ),
    )


# ── 清 kanban-followup 锁时的 tracker lease 回收 ────────────────────────────────────
# 清锁只写掉「别再催这一轮」这一件事，这一轮 RVF 在 tracker 里占着的 scope lease 不会因此消失。
# kanban-followup 的 lease 默认 2h（DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS），而 awaiting
# 短窗只有 10min：不连带释放的话，清锁之后原 scope 的 unit 仍是 ``assigned``，而常规 gate 的
# 候选查询只收 ``review_state='available'`` → 把它们整批排除。于是「交回常规 gate 按当时工作树
# 重新决策」在剩下的近两小时里根本兑现不了；等 lease 自己过期时会话早已走远，当时的口径
# （committed baseline 等）也可能已经丢了。故凡是「清锁 = 放弃这一轮」的路径都必须走这里。
# （lease TTL 上调后这个缺口只会更宽，连带释放因此更不能省。）


def _tracker_lease_identity_for_kanban_followup_lock(
    marker: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """从锁记的 ``run_dir`` 回捞这一轮的 ``(lease_id, repo, 取值来源 artifact 名)``。

    刻意**不**走 dispatch prep file（``marker['dispatch_token']`` → ``rvf_prep_file``，
    ``rvf_agent_decision_cli.run_decline_dispatched_review`` 走的那条）：prep 的 TTL 只有 5min
    且 ``rvf_prep_file.sweep_stale`` 会把过期的直接删掉，而本函数的两个调用点（awaiting 窗口
    10min 到期 / 用户 rewind，可能发生在数小时之后）几乎必然晚于那个窗口——那条路在这里注定
    拿不到 lease id。run dir 里的 artifact 才是长驻来源，按可信度排：

      ① ``artifacts/tracker-scope.json`` —— allocate 成功那一刻直接落盘的 scope（权威）；
      ② ``artifacts/dispatch-prep-file.json`` —— 派发时落的 prep **快照**（含同一个 lease id
         与 ``target_worktree``），①缺失/损坏时兜底。

    repo 优先用锁自己记的 ``repo``（UPS 从 prep 的 ``origin_repo`` 抄来），缺失才回落到 ②。
    """
    repo_raw = marker.get("repo")
    repo = repo_raw.strip() if isinstance(repo_raw, str) and repo_raw.strip() else None
    run_dir_raw = marker.get("run_dir")
    if not (isinstance(run_dir_raw, str) and run_dir_raw.strip()):
        return None, repo, None
    artifacts_dir = Path(run_dir_raw).expanduser() / "artifacts"

    def _read_json_object(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    tracker_scope = _read_json_object(artifacts_dir / "tracker-scope.json")
    if isinstance(tracker_scope, dict):
        lease_id = tracker_scope.get("lease_id")
        if isinstance(lease_id, str) and lease_id.strip():
            return lease_id.strip(), repo, "tracker-scope.json"

    dispatch_prep = _read_json_object(artifacts_dir / "dispatch-prep-file.json")
    if isinstance(dispatch_prep, dict):
        payload = dispatch_prep.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        rvf_run = payload.get("rvf_run")
        rvf_run = rvf_run if isinstance(rvf_run, dict) else {}
        lease_id = rvf_run.get("tracker_lease_id")
        if repo is None:
            for key in ("target_worktree", "origin_repo"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    repo = candidate.strip()
                    break
        if isinstance(lease_id, str) and lease_id.strip():
            return lease_id.strip(), repo, "dispatch-prep-file.json"

    return None, repo, None


def _release_tracker_lease_for_current_run(
    ledger: RunLedger,
    *,
    repo: str | None,
    reason: str,
) -> dict[str, Any]:
    """释放**本次 Stop 自己刚 allocate 出来的** tracker lease（从 ledger 的 scope meta 取 id）。

    与 ``release_tracker_lease_for_reconciled_kanban_followup_lock`` 的区别只在取 lease id 的
    来源：那一支是从**别人留下的 marker**的 run_dir artifact 回捞，本支就在本轮进程里，直接读
    ``ledger.tracker_scope_meta``。用在「派发这一步走不下去、也不会留下任何 pending 记录」的
    收尾路径上——不放掉，这批 scope unit 会以孤儿 ``assigned`` 挂满整个 lease TTL。

    诊断恒为可落 ledger 的 dict；tracker 侧任何异常一律吞掉转成诊断，绝不阻断 Stop。
    """
    meta = getattr(ledger, "tracker_scope_meta", None)
    lease_id = None
    if isinstance(meta, dict):
        lease_id = meta.get("tracker_lease_id") or meta.get("lease_id")
    if not (isinstance(lease_id, str) and lease_id.strip()):
        return {
            "status": "unresolved",
            "released": False,
            "reason": "tracker_lease_id_not_found_on_ledger",
        }
    if not (isinstance(repo, str) and repo.strip()):
        return {
            "status": "unresolved",
            "released": False,
            "reason": "tracker_repo_not_found_for_lease",
            "lease_id": lease_id.strip(),
        }
    try:
        release_result = lease_release(repo=repo.strip(), lease_id=lease_id.strip(), reason=reason)
    except Exception as exc:
        return {
            "status": "error",
            "released": False,
            "lease_id": lease_id.strip(),
            "repo": repo.strip(),
            "error": f"{type(exc).__name__}: {exc}",
        }
    result = dict(release_result) if isinstance(release_result, dict) else {"result": release_result}
    result.setdefault("lease_id", lease_id.strip())
    result["repo"] = repo.strip()
    return result


def release_tracker_lease_for_reconciled_kanban_followup_lock(
    marker: dict[str, Any],
    *,
    reason: str,
) -> tuple[str | None, dict[str, Any]]:
    """清锁时连带释放该轮 RVF 的 tracker lease，让 scope unit 退回 ``available``。

    返回 ``(lease_id, 诊断字典)``，诊断**恒为可落 ledger 的 dict**：拿不到 lease id、拿不到
    repo、释放抛异常都如实写进去，绝不静默跳过——这条路径出问题的表征是「后续 Stop 再也不
    提议这批改动」，是一种沉默的漏审，没有落盘证据根本无从溯源。

    Stop hook 是 best-effort 路径：tracker 侧任何异常一律吞掉转成诊断，绝不阻断 Stop 主流程
    （形状照抄本文件 ``patch_ownership_incomplete_skip_payload`` 的 lease 释放写法）。
    """
    lease_id, repo, source = _tracker_lease_identity_for_kanban_followup_lock(marker)
    if not lease_id:
        return None, {
            "status": "unresolved",
            "released": False,
            "reason": "tracker_lease_id_not_found_in_run_dir_artifacts",
            "run_dir": marker.get("run_dir"),
            "repo": repo,
        }
    if not repo:
        return lease_id, {
            "status": "unresolved",
            "released": False,
            "reason": "tracker_repo_not_found_for_lease",
            "lease_id": lease_id,
            "lease_id_source": source,
            "run_dir": marker.get("run_dir"),
        }
    try:
        release_result = lease_release(repo=repo, lease_id=lease_id, reason=reason)
    except Exception as exc:
        return lease_id, {
            "status": "error",
            "released": False,
            "lease_id": lease_id,
            "lease_id_source": source,
            "repo": repo,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result = dict(release_result) if isinstance(release_result, dict) else {"result": release_result}
    result["lease_id_source"] = source
    result["repo"] = repo
    return lease_id, result


def _kanban_followup_awaiting_agent_decision_decision(
    ledger: RunLedger,
    *,
    marker: dict[str, Any],
    presence: TriggerPresence,
    status: str,
    task_id: str | None,
    session_id: str | None,
    cwd: str | None,
) -> tuple[bool, StopDecision | None]:
    """处理 ``delivered_awaiting_agent_decision`` 态，返回 ``(handled, decision)``。

    - trigger 确在祖先链上 → **升格**（按有无 bootstrap gate 落到 waiting_bootstrap /
      in_progress），``handled=False`` 让调用方落回常规分支（nudge 预算等一切照旧）。
    - 尚未确认落地且锁仍在 awaiting 短窗内 → ``skip_decision``：**只做互斥去重，不发 nudge**。
      这正是本次要根治的那一格——历史实现在这里会催 agent「收尾这一轮」。
    - awaiting 短窗已过仍未确认 → 清锁 + **释放本轮 lease** 交回常规 gate，按**当时**工作树重新
      决策。绝不复活一条早已被会话走远抛下的旧派发（原地复活正是 09:08 派发被 49 分钟后原样
      唤醒的那条路径）。
    """
    marker_path = marker.get("_marker_path")
    blocking_run_id = marker.get("run_id")
    if presence.presence == TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN:
        promoted_state = promote_kanban_followup_lock_out_of_awaiting(
            marker, reason="trigger_on_transcript_ancestor_chain"
        )
        ledger.event(
            phase="gate",
            event="kanban_followup_lock_promoted_after_agent_received_trigger",
            status="completed" if promoted_state else "warning",
            reason_code=(
                "kanban_followup_trigger_on_ancestor_chain"
                if promoted_state
                else "kanban_followup_lock_promotion_write_failed_kept_awaiting"
            ),
            level="info" if promoted_state else "warn",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker_path=marker_path,
            kanban_followup_lock_state=promoted_state,
            kanban_followup_lock_promotion_failed_kept_awaiting=not promoted_state,
            active_rvf_run_id=blocking_run_id,
            **presence.as_ledger_fields(),
        )
        if promoted_state:
            return False, None
        # 升格写盘失败（``promote_...`` 只在 OSError 时返回 None）→ **磁盘上这把锁仍是 awaiting**。
        # 此处绝不能 ``return False, None`` 把它交给下面的常规 in_progress / bootstrap 分支：那条
        # 路会对一把「磁盘态仍是未确认接手」的锁发 re-engage nudge——正是本轮要根治的那个行为
        # （ledger 记了 warning 但控制流照走，等于没 fail-closed）。故 fail-closed：往下继续按
        # awaiting 处理（窗口内只互斥去重、不 nudge；窗口已过则回收 + 释放 lease），下一次 Stop
        # 会重试升格。刻意**不**改成「无条件 skip」：marker 若永久不可写，无条件 skip 会让这把锁
        # squat 到天荒地老；awaiting 短窗给的是确定的兜底上界，且回收方向是「重新决策」而不是
        # 「强制续跑」，最坏代价是一次重复提议，不是漏审。

    if status == KANBAN_FOLLOWUP_LOCK_ACTIVE:
        return True, skip_decision(
            "上一条 Cline Kanban RVF follow-up trigger 已投递、但尚未确认被 agent 接手"
            f"（run_id={blocking_run_id or '<unknown>'}，"
            f"armed_at={marker.get('armed_at') or '<unknown>'}，"
            f"trigger_presence={presence.presence}）；"
            "本次 Stop 只做互斥去重、不催收尾——agent 尚未看到这条 trigger 时催它「继续收尾"
            "这一轮」会逼它跑一次从未接受的 review。等待窗超时后将按当时工作树重新决策。",
            ledger,
            "kanban_followup_delivered_awaiting_agent_decision",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker=marker,
            kanban_followup_in_progress_marker_path=marker_path,
            kanban_followup_lock_state=KANBAN_FOLLOWUP_LOCK_STATE_AWAITING_DECISION,
            active_rvf_run_id=blocking_run_id,
            active_rvf_run_dir=marker.get("run_dir"),
            active_rvf_armed_at=marker.get("armed_at"),
            **presence.as_ledger_fields(),
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_delivered_awaiting_agent_decision",
            ),
        )

    # 释放 lease 必须与清锁同时发生：只清锁不放 lease，那批 unit 会一直卡在 ``assigned``、被
    # 常规 gate 的候选查询排除，「交回常规 gate 按当时工作树重新决策」就只是一句空话。
    released_lease_id, lease_release_result = release_tracker_lease_for_reconciled_kanban_followup_lock(
        marker, reason="kanban_followup_awaiting_agent_decision_expired"
    )
    removed_lock = clear_kanban_followup_lock(
        task_id=task_id,
        session_id=session_id,
        repo=kanban_followup_lock_repository_anchor(cwd=cwd, marker=marker),
    )
    removed_pending = clear_kanban_followup_pending(task_id=task_id)
    ledger.event(
        phase="gate",
        event="kanban_followup_awaiting_agent_decision_window_expired",
        status="completed",
        reason_code="kanban_followup_awaiting_agent_decision_expired",
        cwd=cwd,
        cline_kanban_task_id=task_id,
        session_id=session_id,
        kanban_followup_in_progress_marker=marker,
        kanban_followup_in_progress_marker_path=marker_path,
        kanban_followup_lock_state=KANBAN_FOLLOWUP_LOCK_STATE_AWAITING_DECISION,
        reconciled_blocking_run_id=blocking_run_id,
        removed_kanban_followup_in_progress_marker_paths=removed_lock,
        removed_kanban_followup_pending_marker_paths=removed_pending,
        tracker_lease_id=released_lease_id,
        lease_release_result=lease_release_result,
        **presence.as_ledger_fields(),
    )
    return True, None


def kanban_followup_trigger_presence_for_lock(
    event: dict[str, Any],
    marker: dict[str, Any],
) -> TriggerPresence:
    """判定这把锁对应的 follow-up trigger 是否仍在**投递会话**的 transcript 祖先链上。

    优先用锁自己记下的 ``delivered_transcript_path``（UPS 投递那一刻的权威路径，跨会话仍有效）；
    老 marker 没有该字段时，仅当本次 Stop 与投递是同一会话才退回用本次 Stop 的 transcript
    ——读错文件得出的「不在链上」会误清一把真在跑的锁。

    needle **从 marker 现推**（``trigger_needle_for_lock_marker``）而不是写死 dispatch token：
    P1 之后这把锁的默认来源是「agent 在发起决策点横幅上显式 start 的就地 run」，锚点是横幅
    正文里的 ``RVF_DECISION_TOKEN=<token>``。写死 dispatch token 会让这类锁**永远**判成
    「无 token → unknown」，rewind 对账整条失效。
    """
    transcript_path = marker.get("delivered_transcript_path")
    if not (isinstance(transcript_path, str) and transcript_path.strip()):
        delivered_session_id = marker.get("delivered_session_id")
        current_session_id = event.get("session_id")
        if (
            isinstance(delivered_session_id, str)
            and delivered_session_id
            and delivered_session_id == current_session_id
        ):
            fallback = first_readable_session_path(event)
            transcript_path = str(fallback) if fallback is not None else None
        else:
            transcript_path = None
    trigger_token, trigger_literal = trigger_needle_for_lock_marker(marker)
    return classify_dispatch_trigger_presence(
        transcript_path=transcript_path,
        dispatch_token=trigger_token,
        trigger_literal=trigger_literal,
    )


def kanban_followup_in_progress_decision(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | None,
) -> StopDecision | None:
    task_id = current_kanban_task_id(event)
    session_id = kanban_followup_lock_session_id(event)
    repository_anchor = kanban_followup_lock_repository_anchor(event, cwd=cwd)
    marker = read_kanban_followup_lock(
        task_id=task_id, session_id=session_id, repo=repository_anchor
    )
    if marker is None:
        # 无 in-progress 锁（从未落地或已 handoff 清掉）→ 转交 pending 对账：判断上一条
        # dispatch 是否在途（去重）或已静默丢投（重投）。
        return _kanban_followup_pending_decision(
            event, ledger, task_id=task_id, session_id=session_id, cwd=cwd
        )

    status = kanban_followup_lock_status(marker)
    marker_path = marker.get("_marker_path")
    lock_state = kanban_followup_lock_state(marker)

    # ── 锁卫生（先于一切 nudge/skip）：这条 trigger 现在还在主链上吗？ ────────────────
    # 「UPS 上过锁」只证明 prompt 被**提交**过，不证明 agent 收到、更不证明它现在还算数。
    # 用户 rewind 会把落地过的那一段整体移出祖先链；此时继续按锁催 agent「收尾这一轮」，
    # 就是在逼它跑一次它从未接受、且已被撤销的 RVF。
    presence = kanban_followup_trigger_presence_for_lock(event, marker)
    if presence.presence == TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND:
        # 确凿证据：token 在这份 transcript 里出现过，却不在当前 leaf 的祖先链上 = 被 rewind。
        # （若是 /compact 抹掉了历史，token 会整个消失 → ABSENT，不会走到这里，故本判据不会
        # 因压缩而误伤。）清锁 + 清 pending + **释放本轮 lease**，交回常规 gate 按**当前**工作树
        # 重新决策——rewind 掉的那一轮 scope 若不放 lease，会一路 assigned 到 1h TTL 到期，
        # 期间常规 gate 看不到这批 unit（候选查询只收 available），等于把「重新决策」推迟一小时。
        released_lease_id, lease_release_result = (
            release_tracker_lease_for_reconciled_kanban_followup_lock(
                marker, reason="kanban_followup_trigger_rewound_off_ancestor_chain"
            )
        )
        removed_lock = clear_kanban_followup_lock(
            task_id=task_id, session_id=session_id, repo=repository_anchor
        )
        removed_pending = clear_kanban_followup_pending(task_id=task_id)
        ledger.event(
            phase="gate",
            event="kanban_followup_lock_reconciled_for_rewind",
            status="completed",
            reason_code="kanban_followup_trigger_rewound_off_ancestor_chain",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker=marker,
            kanban_followup_in_progress_marker_path=marker_path,
            kanban_followup_lock_state=lock_state,
            reconciled_blocking_run_id=marker.get("run_id"),
            removed_kanban_followup_in_progress_marker_paths=removed_lock,
            removed_kanban_followup_pending_marker_paths=removed_pending,
            tracker_lease_id=released_lease_id,
            lease_release_result=lease_release_result,
            **presence.as_ledger_fields(),
        )
        return None

    if lock_state == KANBAN_FOLLOWUP_LOCK_STATE_AWAITING_DECISION:
        handled, awaiting_decision = _kanban_followup_awaiting_agent_decision_decision(
            ledger,
            marker=marker,
            presence=presence,
            status=status,
            task_id=task_id,
            session_id=session_id,
            cwd=cwd,
        )
        if handled:
            return awaiting_decision
        # 升格成功（trigger 确在祖先链上）→ 落回下面的常规 in_progress / waiting_bootstrap 分支。
        status = kanban_followup_lock_status(marker)
        lock_state = kanban_followup_lock_state(marker)

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
            removed_lock = clear_kanban_followup_lock(
                task_id=task_id, session_id=session_id, repo=repository_anchor
            )
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
        # bootstrap 等待：这一轮**还没开始 review**，只是在等 local base 被吸收。必须排在下面的
        # nudge/skip 之前——那两条都以「review 正在跑、只差 handoff」为前提，对一个还卡在
        # bootstrap 门前的 agent 发「继续收尾、输出 handoff」是错误指令。
        handled, bootstrap_decision = _kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=marker,
            task_id=task_id,
            session_id=session_id,
            cwd=cwd,
            store_expired=False,
        )
        if handled:
            return bootstrap_decision
        # B（re-engage nudge）：锁 active 但 agent 停在「未 handoff」处时，历史行为是无条件
        # 返回 {"continue":true} 静默放行 → agent 当回合干停、RVF 不再 force-continue，于是它
        # 被这把锁一路静默挡停直到 handoff/TTL（实测曾冻 47min）。这里改为：在 nudge 预算内
        # 不静默放行，而是 return None 把判定让回常规 gate——若 latest turn 不是注入的 follow-up
        # trigger 且仍有未审改动，常规 gate 会重新派发 review / 重新唤起 agent（把「让 agent 停」
        # 与「不派重复 review」解耦）。预算（marker.reengage_nudge_count vs 默认 2）用尽再退回
        # 静默 skip，防 review↔fix 死循环；配合 1h 锁 TTL，一把卡死锁最坏只冻「预算次 + ≤1h」。
        #
        # 仅在本回合「不会被随后的 followup/analyze trigger-skip 无条件挡停」时才消耗预算 nudge：
        # evaluate_stop_event 在调用本函数之后还排了三道 trigger-skip（RVF_ANALYZE_FOLLOWUP /
        # 显式 $rvf-analyze / KANBAN_FOLLOWUP），若本 Stop 的 latest_user 命中其一，本函数 return
        # None 也换不来 re-engage（trigger-skip 会接管挡停本回合），只会空耗预算、把真正能 re-engage
        # 的 non-trigger turn 的预算削掉。故这种 trigger turn 直接 return None 不 bump，预算留到后面
        # 能真正生效的 non-trigger turn。（committed-round RVF review elevated 修复：原实现在 trigger
        # turn 上先 bump 再被 trigger-skip 挡停，stall-on-trigger-turn 场景下预算白花、re-engage 失效。）
        latest_user_for_nudge = latest_user_message_from_event(event)
        if (
            latest_user_for_nudge
            and (
                RVF_ANALYZE_FOLLOWUP_MARKER in latest_user_for_nudge
                or KANBAN_FOLLOWUP_MARKER in latest_user_for_nudge
            )
        ) or _rvf_analyze_manually_invoked(event, latest_user_for_nudge):
            return None
        nudge_budget = _kanban_followup_env_int(
            KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET_ENV,
            DEFAULT_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET,
        )
        nudge_count = kanban_followup_lock_nudge_count(marker)
        if nudge_budget > 0 and nudge_count < nudge_budget:
            bumped = bump_kanban_followup_lock_nudge_count(marker)
            active_rvf_run_dir = marker.get("run_dir")
            ledger.event(
                phase="gate",
                event="kanban_followup_in_progress_nudged",
                status="completed",
                reason_code="kanban_followup_in_progress_reengage_nudge",
                cwd=cwd,
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_in_progress_marker=marker,
                kanban_followup_in_progress_marker_path=marker_path,
                kanban_followup_in_progress_nudge_count=bumped,
                kanban_followup_in_progress_nudge_budget=nudge_budget,
                active_rvf_run_id=blocking_run_id,
                active_rvf_run_dir=active_rvf_run_dir,
            )
            # re-engage = force-continue 现有 run，**不**经常规 gate 重派新 RVF（修根因：原
            # return None 让回常规 gate，reset --mixed 物化的 dirty 使常规 gate 每次都判「有未审
            # 改动」→ mint 重复 run）。把「唤醒 agent 收尾」与「派发 review」彻底解耦。
            return reengage_decision(
                f"task {task_id or '<unknown>'} 的 RVF run "
                f"{blocking_run_id or '<unknown>'} 仍在进行且未 handoff"
                f"（run_dir={active_rvf_run_dir or '<unknown>'}）。请继续收尾**这一轮**——"
                "跑完 / 合并 reviewer 结果后，第一行输出 "
                "`RVF_HANDOFF_FILE: <handoff.md 绝对路径>` 完成 handoff，**不要新开 review**。"
                "若你正在等后台 reviewer，请先确认其状态再 finalize。",
                ledger,
                cwd=cwd,
                cline_kanban_task_id=task_id,
                session_id=session_id,
                kanban_followup_in_progress_marker=marker,
                kanban_followup_in_progress_marker_path=marker_path,
                kanban_followup_in_progress_nudge_count=bumped,
                kanban_followup_in_progress_nudge_budget=nudge_budget,
                active_rvf_run_id=blocking_run_id,
                active_rvf_run_dir=active_rvf_run_dir,
                **stop_hook_rvf_state_fields(
                    phase="complete",
                    backend="kanban-followup",
                    backend_raw="kanban-followup",
                    completion_gate="kanban_followup_in_progress_reengage",
                ),
            )
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

    # 锁已过 TTL（stale / invalid）。清它之前先问一句：这一轮是不是只是**还在等 bootstrap**？
    # 若是，清锁会让常规 gate 判「没人在跑」而重铸 token / 重开 run——正是历史上那条既漏审又
    # 多派的路径。等待态一律改为就地复活 + 定向 re-engage，只有工件真丢了才落到下面的清理。
    handled, bootstrap_decision = _kanban_followup_bootstrap_gate_decision(
        ledger,
        marker=marker,
        task_id=task_id,
        session_id=session_id,
        cwd=cwd,
        store_expired=True,
    )
    if handled:
        return bootstrap_decision

    # 心跳续期（B）：清锁之前再问一句「这一轮其实还在跑吗？」
    #
    # 原本这里是无条件清锁 —— 而 TTL 被证实短于它要保护的事件（实测 p50 = 84.3min、53% 超过
    # 原先的 1h TTL），于是一轮**正常**的长 review 会在中途丢掉自己的抑制凭证，此后每一次 Stop
    # 都落回 fresh-initiation、对**这一轮自己尚未 handoff 的产物**重新征询。近 4 天落盘的 7 次
    # stale 清锁后面全都紧跟一次重铸+decline。
    #
    # 存活证据 = run 目录的**阶段锚点**近期仍有写入。注意别把它读成「ledger 每个阶段都在写、
    # 所以 run 目录 mtime 是条覆盖整轮的心跳」——那句话实测是错的：只扫 run 根 + 直接子项时，
    # 215 个真实长 run 里 51.6% 至少有一个时刻会被判「已死」、46.2% 的在途时长是盲的，即本条
    # 续期分支基本等于掷硬币。观察面与窗口宽度的实测标定见 kanban_followup_lock 里
    # ``RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS`` 上方那段；此处只需记住它仍有一类**探不到**的
    # 盲窗：主 agent 在仓库工作树里改代码/跑测试的那几十分钟，run 目录一个字节都不动。
    # 拿不到证据（run_dir 缺失 / 不可读 / 久无活动）一律**不**续期：抑制性凭证的失败方向必须是
    # 「多问一次」而不是「静默续命」。
    #
    # 绝对硬顶（F2a：任何 wait 分支都要有与被等对象无关的上界）由**本调用点**负责，不塞进
    # ``revive_marker`` 的默认行为——bootstrap 等待那条路径有它自己的上界形态（复活次数预算），
    # 且其中「收尾时把锁续回 active」那一处必须成功，在共享路径上塞墙钟硬顶会把 32cad 回归放
    # 回来。硬顶自 ``armed_at`` 起算、不随续期前移；越过之后无论 run 写得多勤都不再续——「它还
    # 在写日志」恰恰是活锁最典型的样子。
    #
    # 硬顶要真的是硬顶，需要**两道**判定，缺一道就只是个建议值：
    #   ① 续期之前判「还能不能续」（``..._exceeds_revival_absolute_cap``）；
    #   ② 续期之时判「最多能续到哪」（``truncate_expiry_to_revival_absolute_cap=True``）。
    # 只有 ① 时，一把在 ``cap - 1min`` 处续上的锁会被推到 ``now + TTL`` —— 而下一次 Stop 读到它
    # 是 ACTIVE，ACTIVE 分支不复查硬顶，于是它一路 active 到 ``armed + cap + TTL``（实测 armed
    # 5h59m 的锁被续到 armed + 7.98h，6h 硬顶被整整绕过一个 TTL）。② 之后上界闭合在 armed + cap：
    # 硬顶那一刻锁必然转 stale，回到这里就只剩 ① 那条「不再续、清锁交回常规 gate」的出口。
    #
    # 第一个合取项是 RVF-003：心跳只对**真的在跑 review** 的锁生效，绝不覆盖 bootstrap 侧刚刚
    # 作出的放弃决定。上面那次 gate 判定返回 ``handled=False`` 有两种成因，处置**相反**：
    #   * 这把锁与 bootstrap 无关（没有等待 gate）→ 正是心跳要保护的长 review；
    #   * 等待 gate 还在、但复活预算已耗尽 → bootstrap 已**明确放弃本轮**，把 stale 语义交还给
    #     下面的清锁。此时再按 run 活动续期，等于把「3 次复活」的等待上界偷换成「6h 硬顶」。
    # 判据取 marker 的 ``state`` 而不是 gate 形状，因为真正的理由在证据的语义上：心跳的存活证据
    # 是「run 目录近期有写入」，而等待态的 run 目录是被 **prepare 重放**写的，不代表 review 在
    # 推进——receipt 已验证却重放反复失败时，重放本身就刚写过 run 目录，恰好把这条证据变成永动机。
    if (
        kanban_followup_lock_state(marker)
        != KANBAN_FOLLOWUP_LOCK_STATE_WAITING_BOOTSTRAP
        and in_flight_rvf_run_shows_recent_activity(marker)
        and not kanban_followup_lock_exceeds_revival_absolute_cap(marker)
        and revive_kanban_followup_lock(
            marker, truncate_expiry_to_revival_absolute_cap=True
        )
    ):
        ledger.event(
            phase="gate",
            event="kanban_followup_in_progress_marker_revived_on_run_activity",
            status="completed",
            reason_code="kanban_followup_in_progress_revived_on_run_activity",
            cwd=cwd,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker_path=marker.get("_marker_path"),
            active_rvf_run_id=marker.get("run_id"),
            active_rvf_run_dir=marker.get("run_dir"),
            active_rvf_armed_at=marker.get("armed_at"),
            seconds_since_last_run_activity=seconds_since_last_in_flight_rvf_run_activity(marker),
            seconds_since_marker_armed=seconds_since_kanban_followup_lock_armed(marker),
            kanban_followup_lock_revive_count=kanban_followup_lock_revive_count(marker),
        )
        return skip_decision(
            "已有 RVF run 仍在进行（run 目录近期仍有写入），锁虽已过 TTL 但已就地续期；"
            f"本次 Stop 跳过自动 RVF dispatch。run_id={marker.get('run_id') or '<unknown>'}，"
            f"armed_at={marker.get('armed_at') or '<unknown>'}，"
            f"续期次数={kanban_followup_lock_revive_count(marker)}，"
            f"续到 expires_at={marker.get('expires_at') or '<unknown>'}。"
            "续期有绝对硬顶（自 armed_at 起算，本次续期已按它截断），"
            "到点即按 TTL 判死、清锁交回常规 gate。",
            ledger,
            "kanban_followup_in_progress_revived_on_run_activity",
            cwd=cwd,
            backend="kanban-followup",
            cline_kanban_task_id=task_id,
            session_id=session_id,
            kanban_followup_in_progress_marker=marker,
            active_rvf_run_id=marker.get("run_id"),
            active_rvf_run_dir=marker.get("run_dir"),
            active_rvf_armed_at=marker.get("armed_at"),
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_in_progress_revived_on_run_activity",
            ),
        )

    removed = clear_kanban_followup_lock(
        task_id=task_id, session_id=session_id, repo=repository_anchor
    )
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
    """失败再入：在即将 dispatch / allocate 前消费 rescope marker，并把结果落 ledger。

    真正的读-重开-consume 三步住在 ``review_reopen_marker.consume_review_reopen_marker_for_run_scope``
    ——**两条发起入口共用同一份实现**：本函数（Stop hook 旧派发路径）与 ``rvf-agent start``
    （P1 之后的默认就地路径）。本函数只负责 Stop 侧的 ledger 记账。

    语义不变：``active`` → 按 ``target_run_id`` 把该实现 run 仍存在的 ``reviewed`` units 翻回
    ``available``（run-scoped，**绝不广播**），使紧接着的 allocate 自然得到「该实现 units ∪
    本次 fix delta」全量重审；``stale`` / ``invalid`` 也消费但不重开。best-effort：tracker
    异常不阻断 Stop 主流程。
    """
    task_id = current_kanban_task_id(event)
    session_id = session_hook_id_from_event(event)
    consumption = consume_review_reopen_marker_for_run_scope(
        task_id=task_id, session_id=session_id, repo=repo
    )
    outcome = consumption.get("outcome")
    if outcome == REVIEW_REOPEN_CONSUMPTION_NO_MARKER:
        return None

    marker = consumption.get("marker") or {}
    target_run_id = consumption.get("target_run_id")
    removed = consumption.get("consumed_marker_paths") or []

    if outcome == REVIEW_REOPEN_CONSUMPTION_DISCARDED_NOT_ACTIVE:
        ledger.event(
            phase="gate",
            event="review_reopen_marker_discarded",
            status="completed",
            reason_code=f"review_reopen_{consumption.get('status')}",
            cwd=cwd,
            repo=repo,
            cline_kanban_task_id=task_id,
            session_id=session_id,
            target_run_id=target_run_id,
            review_reopen_marker=marker,
            consumed_review_reopen_marker_paths=removed,
        )
        return None

    reopen_result = consumption.get("reopen_result")
    reopen_error = consumption.get("reopen_error")
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
    host_label = resolve_host_adapter(parent_origin.get("host_kind")).host_display_name()
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
    # S5（R22）：bootstrap 独立确认闸已废除 —— 无条件短路，不再计算 decision、不再写确认
    # marker、不再 raise BootstrapConfirmationRequired。超大 bootstrap 的把关职责移交发起决策点：
    # 候选摘要（S2）携带 bootstrap_size，agent 在 start/decline 时据 R24 护栏（超大改动本应开工前
    # elevate 给用户）自行裁量。UPS 侧的 yes/no resume 闸已同步废除（rvf_user_prompt_submit S5），
    # 若此处仍写 marker 将成为无人应答的悬空流。函数体与 BootstrapConfirmationRequired /
    # resume_dispatch_from_confirmation_marker 保留作观察期考古（R11/R23 fallback 冻结），下方
    # 代码在废除期内不可达。
    return
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


# ``cline_kanban_client.py`` 给三种降级运行态各留了专属退出码（见该文件 main()）：
# 3=接口不存在、4=还没结论（CLI 硬超时 exit 124 + 空 stdout）、5=账本无此记录。
# 其中 **4 绝不是投递失败**：那条消息可能已经被受理，只是 CLI 没等到结论就到点了。
KANBAN_FOLLOWUP_CLIENT_EXIT_VERDICT_NOT_YET_AVAILABLE = 4


class KanbanFollowupDeliveryVerdictNotYetAvailableError(RuntimeError):
    """``kanban task message`` 没能在硬超时内给出结论（子进程退出码 4）。

    **不是**投递失败：这条消息可能已被受理并随后落地。故调用方不得走「派发不可用、不写
    pending」那条路 —— 那会让一条可能在途的消息失去 status 对账与撤回能力，并可迟到执行。
    正确处置是照「未确认投递」写 pending：``provisional_payload()`` 给出**只含已知为真字段**的
    最小回执（不伪造 ``message_id``/``terminal_delivery_status``；回执判读会据此落 awaiting）。
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str,
        idempotency_key: str,
        attempt_id: str | None,
        prompt_path: str | None,
        command_artifact_path: str | None,
        project_path: str,
        task_cmd: str,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.idempotency_key = idempotency_key
        self.attempt_id = attempt_id
        self.prompt_path = prompt_path
        self.command_artifact_path = command_artifact_path
        self.project_path = project_path
        self.task_cmd = task_cmd

    def provisional_payload(self) -> dict[str, Any]:
        """只含**已知为真**字段的临时回执（``message_id`` 恒缺失：我们确实没拿到）。"""
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "prompt_path": self.prompt_path,
            "command_artifact_path": self.command_artifact_path,
            "project_path": self.project_path,
            "task_cmd": self.task_cmd,
            "terminal_delivery_verdict_not_yet_available": True,
        }
        if self.attempt_id:
            payload["attempt_id"] = self.attempt_id
        return payload


# 「投递到底有没有产生外部副作用」这个问题，只有 ``start_cline_kanban_followup_message``
# 自己答得出——它知道子进程有没有真的跑起来。调用方只能看到一个异常，于是历史实现把**所有**
# 异常一律当「确定失败」，而真实 client 的顺序是「先跑完 ``kanban task message`` 子进程（横幅
# 此时可能已经投出去了），再解析 JSON、再校验 message_id」——后两步任一失败都会抛。结果是：
# 横幅明明已经到 agent 面前，Stop 侧却判它确定失败并当场再自注入一条。
#
# 故把「投递未发生」与「投递结论未知」拆成两族异常，由本函数在**知道真相的地方**分类，
# 调用方按 ``external_side_effect_may_have_happened`` 分流。失败方向必须是「晚问」不是「双投」。
class KanbanFollowupDeliveryNotAttemptedError(RuntimeError):
    """投递子进程**根本没能启动**——外部副作用**确定没发生**，可以安全回落。

    只覆盖「送出去这件事根本没开始」：prompt 工件写不出来、客户端脚本路径不存在、
    ``subprocess`` 自身起不来（``OSError``/``FileNotFoundError``）。一旦子进程跑起来了，
    无论它后来怎么失败，都**不**属于这一族。
    """

    external_side_effect_may_have_happened = False


class KanbanFollowupDeliveryOutcomeUnknownError(RuntimeError):
    """子进程跑完了、但**回执不可用**——横幅可能已经投到 agent 面前了，结论未知。

    覆盖：退出码非零、stdout 不是合法 JSON（CLI 版本漂移多打了一行也算）、回执缺
    ``message_id``。这三种在真实世界里都发生在**投递之后**，绝不能当确定失败处置。

    与 :class:`KanbanFollowupDeliveryVerdictNotYetAvailableError` 的区别：那一族是对方
    **明说**「还没结论」并给得出可用的临时回执（idempotency_key 齐全，能事后查 status）；
    这一族连回执都没读成，只能靠调用方保留的投递意图做后续对账。
    """

    external_side_effect_may_have_happened = True


def start_cline_kanban_followup_message(
    *,
    project_path: str,
    task_id: str,
    attempt_id: str | None,
    prompt: str,
    ledger: RunLedger,
    idempotency_key: str | None = None,
    timeout: float | None = None,
    artifact_label: str = "kanban-followup",
) -> dict[str, Any]:
    """经 cline-kanban 终端投递把 ``prompt`` 投成目标 task 的一条真实 user message。

    ``artifact_label`` 只决定本次投递的工件文件名前缀。它存在是因为这条投递管线现在承载**两种
    语义完全不同**的正文：一条 review follow-up（有 run / lease / prep 在背后），和一次**尚未被
    接受**的发起征询横幅。把后者的工件也叫 ``kanban-followup.prompt.md`` 会让事后取证的人把两者
    混为一谈——而这两者的善后动作完全不同。

    **异常契约**（调用方按此分流，见两个异常类的 docstring）：

    * :class:`KanbanFollowupDeliveryNotAttemptedError` —— 投递**确定没发生**（子进程没起来）；
    * :class:`KanbanFollowupDeliveryVerdictNotYetAvailableError` —— 对方明说「还没结论」；
    * :class:`KanbanFollowupDeliveryOutcomeUnknownError` —— 子进程跑完了但回执不可用，
      **横幅可能已经投出去了**；
    * 其余异常（如 ``subprocess.TimeoutExpired``）同样发生在子进程起来之后，调用方须按
      「结论未知」兜底，绝不可当确定失败。
    """
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    prompt_path = ledger.artifact(f"{artifact_label}.prompt.md", prompt)
    if not prompt_path:
        # prompt 工件都没写出来 ⇒ 命令根本还没组装，投递确定没发生。
        raise KanbanFollowupDeliveryNotAttemptedError(
            "failed to write Cline Kanban terminal delivery prompt artifact"
        )
    if not client.is_file():
        # 客户端脚本不在 ⇒ 子进程会以「python 打不开这个文件」失败，外部副作用确定没发生。
        # 显式前置检查而不是靠退出码倒推：``sys.executable <missing>`` 的 exit 2 与「跑起来了
        # 然后失败」在退出码层面无法区分，而这两者的善后动作正好相反。
        raise KanbanFollowupDeliveryNotAttemptedError(
            f"Cline Kanban client script does not exist: {client}"
        )

    # 默认沿用每次 Stop 唯一的 ledger.run_id 作幂等键（主派发路径）；S2 重投显式传入
    # 一枚**绑定本次新铸 dispatch token** 的新键（见 _maybe_redispatch_stranded_kanban_followup）。
    # 契约第 7 条：同 key + 同 prompt hash 只返回当前记录、**不重投**；同 key + 不同 prompt hash
    # 直接报幂等冲突。故「重投」与「换 key」是同一件事，去重责任落在 per-task 派发单飞闸上。
    resolved_idempotency_key = idempotency_key or str(ledger.run_id)
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
        resolved_idempotency_key,
    ]
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])
    # 契约第 4 条：短等而已（默认 0 = 完全不等，保持 Stop 链路的既有时延）。开大它并不能等满
    # 契约的 196.5s 收敛预算——CLI 硬超时会把等待 clamp 掉；真正的终态靠 message-status 轮询。
    wait_ms = _kanban_followup_env_int(
        KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS_ENV,
        DEFAULT_KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS,
    )
    if wait_ms > 0:
        command.extend(["--wait-for-terminal-status-timeout-ms", str(wait_ms)])

    # timeout 默认 None=主路径行为不变（无内层超时，由 Codex 链路 30s 总预算兜底）；
    # S2 重投显式传入有界 timeout，避免在 best-effort 路径上无界阻塞吃光预算。
    #
    # ⚠️ 这一行是「投递未发生」与「投递可能已发生」的**分界线**：它抛 OSError 说明子进程压根
    # 没起来（确定没投）；它一旦返回（或抛 TimeoutExpired），横幅就可能已经在 agent 面前了。
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, **ledger.env()},
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        raise KanbanFollowupDeliveryNotAttemptedError(
            f"failed to start Cline Kanban task message subprocess: {type(exc).__name__}: {exc}"
        ) from exc
    command_path = ledger.artifact(
        f"{artifact_label}-message.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode == KANBAN_FOLLOWUP_CLIENT_EXIT_VERDICT_NOT_YET_AVAILABLE:
        # 「还没结论」必须在 parse_json_command_output 之前拦截：那个包装器把所有非零退出码
        # 一律当普通失败抛，会让一条**可能已被受理**的消息走「派发不可用、不写 pending」。
        raise KanbanFollowupDeliveryVerdictNotYetAvailableError(
            (completed.stderr or "").strip()
            or "Cline Kanban task message did not reach a verdict before the CLI hard timeout",
            task_id=task_id,
            idempotency_key=resolved_idempotency_key,
            attempt_id=attempt_id,
            prompt_path=prompt_path,
            command_artifact_path=command_path,
            project_path=project_path,
            task_cmd=task_cmd,
        )
    # 走到这里，子进程已经跑完了 —— 横幅**可能已经投出去**。此后任何解析/校验失败都只是
    # 「我们读不懂回执」，绝不是「投递失败」。故统一升成「结论未知」那一族。
    try:
        payload = parse_json_command_output(completed, label="Cline Kanban task message")
    except RuntimeError as exc:
        raise KanbanFollowupDeliveryOutcomeUnknownError(str(exc)) from exc
    message_id = str(payload.get("message_id") or payload.get("messageId") or "").strip()
    if not message_id:
        raise KanbanFollowupDeliveryOutcomeUnknownError(
            f"Cline Kanban task message response did not include message_id: {payload!r}"
        )
    payload["message_id"] = message_id
    payload.setdefault("task_id", task_id)
    if attempt_id:
        payload.setdefault("attempt_id", attempt_id)

    ledger.artifact(
        f"{artifact_label}-message-result.json",
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
    # 幂等键是「事后还能不能查到这条投递的真实终态」的唯一钥匙（message-status 按
    # (task_id, idempotency_key) 定位），必须显式回传给调用方写进 pending marker。
    payload["idempotency_key"] = payload.get("idempotency_key") or resolved_idempotency_key
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


def run_review_validate_fix_child_fork(
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
    payload = run_review_validate_fix_child_fork(
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
    return run_review_validate_fix_child_fork(
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
        # 契约第 5 条：同一 task 绝不并发投递（cline-kanban 每 session 只有一个单飞槽，先到的
        # 会被判 superseded_by_later_delivery）。拿不到闸即本轮不投——下一次 Stop 重新决策。
        #
        # 这道闸必须一路裹到 **pending marker 落盘为止**，并在闸内**重新校验**决策是否已过时：
        # 「此刻没有在途 follow-up」这个判据是在本次 Stop 早得多的时候读的，两个 Stop 完全可以
        # 都读到「无 pending」，再一前一后进闸各投一条**新 key** —— 先到的那条随即被判
        # ``superseded_by_later_delivery``，正是这道闸要防的事。只有「闸内复检 + 闸内落盘」
        # 才把「检查 → 投递 → 记账」变成一个原子段。
        message_payload: dict[str, Any] | None = None
        dispatch_error: str | None = None
        delivery_verdict_not_yet_available = False
        pending_marker_path: str | None = None
        pending_task_id: Any = None
        with kanban_followup_dispatch_single_flight_for_task(task_id) as dispatch_slot_acquired:
            if not dispatch_slot_acquired:
                return skip_payload(
                    "另一个进程正在对同一个 Cline Kanban task 派发 RVF follow-up；"
                    "本次 Stop 不并发投递（并发会让先到的那条被判 superseded_by_later_delivery）。",
                    ledger,
                    "kanban_followup_dispatch_single_flight_busy",
                    repo=decision.repo,
                    cwd=cwd,
                    backend=decision.backend,
                    cline_kanban_task_id=task_id,
                    cline_kanban_project_path=project_path,
                    **stop_hook_rvf_state_fields(
                        phase="prepare",
                        backend="kanban-followup",
                        backend_raw=decision.backend,
                    ),
                )
            concurrent_pending = read_kanban_followup_pending(task_id=task_id)
            if (
                concurrent_pending is not None
                and kanban_followup_pending_status(concurrent_pending)
                == KANBAN_FOLLOWUP_LOCK_ACTIVE
            ):
                return skip_payload(
                    "闸外做出的「此刻没有在途 Cline Kanban RVF follow-up」判断已经过时：另一个 "
                    "Stop 抢先投了一条并写下 active pending。本次 Stop 不再并发投递"
                    "（并发会让先到的那条被判 superseded_by_later_delivery）。",
                    ledger,
                    "kanban_followup_dispatch_superseded_by_concurrent_pending",
                    repo=decision.repo,
                    cwd=cwd,
                    backend=decision.backend,
                    cline_kanban_task_id=task_id,
                    cline_kanban_project_path=project_path,
                    kanban_followup_pending_marker=concurrent_pending,
                    kanban_followup_pending_marker_path=concurrent_pending.get("_marker_path"),
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
            except KanbanFollowupDeliveryVerdictNotYetAvailableError as exc:
                # CLI 硬超时（exit 124 + 空 stdout）= 「还没结论」，**不是**投递失败。走
                # `kanban-followup-unavailable` 且不写 pending 会让一条**可能已被受理**的消息
                # 失去 status 对账与撤回能力，并可迟到执行。故按未确认投递继续，用只含已知为真
                # 字段的临时回执写 pending（回执判读据此落 awaiting，绝不伪造送达）。
                message_payload = exc.provisional_payload()
                delivery_verdict_not_yet_available = True
            except Exception as exc:
                message_payload = None
                dispatch_error = f"{type(exc).__name__}: {exc}"

            if message_payload is not None:
                # 旧设计在此把 message_id/turn_id 回写进 in-progress 锁；现在 arm 由目标 session
                # 的 UserPromptSubmit hook 在投递落地时完成，这里不再写锁。
                message_id = message_payload.get("message_id")
                # 投递通道只作审计（新契约下 message_id 恒为 ``terminal:<task>:<key>``，它**不再**是
                # 「是否已确认送达」的判据）。真相唯一来自 terminal_delivery_status，见下。
                delivery_channel = _kanban_followup_delivery_channel(message_id)
                delivery_receipt = classify_cline_kanban_terminal_delivery_receipt(message_payload)
                # F2a：``accepted_pending_submit_confirmation`` 不是成功；遗留 ``status`` 的
                # started/queued 是 mutate **之前**读的快照推断、与实际投递结果无关，同样不算成功。
                # 只有契约的两个 delivered_* 终态才算「已确认进入 agent」。
                delivery_confirmed = delivery_receipt.is_delivered
                if delivery_confirmed:
                    status = "kanban-followup-started"
                elif delivery_receipt.is_failed:
                    status = "kanban-followup-delivery-failed"
                else:
                    status = "kanban-followup-dispatched-unconfirmed"
                reason_code = status.replace("-", "_")
                delivery_receipt_fields = {
                    "kanban_terminal_delivery_status": delivery_receipt.terminal_delivery_status,
                    "kanban_terminal_delivery_failure_reason": (
                        delivery_receipt.terminal_delivery_failure_reason
                    ),
                    "kanban_terminal_delivery_receipt_field_source": (
                        delivery_receipt.receipt_field_source
                    ),
                    "kanban_terminal_delivery_redispatch_disposition": (
                        delivery_receipt.redispatch_disposition
                    ),
                    "kanban_terminal_delivery_summary": (
                        describe_terminal_delivery_receipt_for_operator(delivery_receipt)
                    ),
                    "kanban_terminal_delivery_verdict_not_yet_available": (
                        delivery_verdict_not_yet_available
                    ),
                }
                # 写 pending marker，作为「dispatch 已发、这一轮尚未成为 turn」的对账依据：UPS arm
                # 落地时按 token 清掉它；下一次 Stop 拿 idempotency_key 去查真实终态。
                #
                # **已确认送达的也要写。** 曾以为 delivered 就不必留记录，那是错的：
                # ``delivered_queued_behind_active_agent_turn`` 只是排上了队，而
                # ``delivered_and_submit_confirmed`` 之后 UPS 也可能还没 arm 就被中断 / rewind 掉。
                # 这两种情形下既没有 in-progress 锁、也没有带 TTL 的 pending，
                # 「delivered 但从未成为 turn ⇒ 到期撤回」那道保护就永远不会触发，那轮 review
                # 静默搁浅且看上去一切正常。
                #
                # **已确定失败的仍不写** —— 此刻并没有任何东西在途，写一条 active pending 只会把
                # 接下来 15 分钟的重新决策白白挡住；失败本身已进 ledger 并会走通知。
                if not delivery_receipt.is_failed:
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
                            turn_id=(
                                message_payload.get("turn_id") or message_payload.get("turnId")
                            ),
                            prompt_path=message_payload.get("prompt_path"),
                            # S0：快照 deep-link 与可选 verify-consumed 所需字段，供 S1a/S1b 复用。
                            kanban_project_path=project_path,
                            kanban_task_title=source_origin.get("kanban_task_title"),
                            kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                            origin_transcript_path=source_origin.get("transcript_path"),
                            # F2a：没有这枚 key 就查不到真实终态，对账只能退回 TTL 猜测。
                            idempotency_key=message_payload.get("idempotency_key"),
                            terminal_delivery_status=delivery_receipt.terminal_delivery_status,
                            terminal_delivery_failure_reason=(
                                delivery_receipt.terminal_delivery_failure_reason
                            ),
                            terminal_delivery_status_updated_at=(
                                delivery_receipt.terminal_delivery_status_updated_at
                            ),
                        )
                        pending_marker_path = str(pending_path) if pending_path else None
                    except Exception:  # best-effort：写 pending 失败不阻断 dispatch 上报
                        pending_marker_path = None

        if message_payload is None:
            error = dispatch_error or "unknown dispatch error"
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

        # 通知与 lease 记账在闸外做：它们与「别并发投同一个 task」无关，压在闸内只会白白拉长
        # 临界区（通知子进程默认 timeout 10s）。
        if not delivery_confirmed and not delivery_receipt.is_failed:
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
        elif delivery_receipt.is_failed:
            # 确定没送到：没有 pending marker 会替它浮现，故这里直接发一条 OS 通知。
            # 整段 best-effort，永不抛。
            try:
                failed_task_id = message_payload.get("task_id") or task_id
                notify_kanban_followup_stranded(
                    task_id=failed_task_id,
                    task_title=source_origin.get("kanban_task_title"),
                    task_url=resolve_kanban_task_url(project_path, failed_task_id),
                    reason=(
                        "delivery-failed:"
                        f"{delivery_receipt.terminal_delivery_failure_reason or 'unknown'}"
                    ),
                )
            except Exception:
                pass
            # 这条路径刻意不留 pending，于是**没有任何东西**会在之后替这一轮释放 tracker lease：
            # 不在这里放掉，本轮 allocate 出来的 scope unit 会在整个 lease TTL（默认约 1h）里
            # 保持 ``assigned``，而 hook payload 却承诺「下一次该 task 的 Stop 会按当时的工作树
            # 重新决策」—— 那次 Stop 根本拿不到这批单元，承诺是空头的。best-effort，永不抛。
            failed_lease_release = _release_tracker_lease_for_current_run(
                ledger,
                repo=decision.repo,
                reason=(
                    "kanban_followup_delivery_failed:"
                    f"{delivery_receipt.terminal_delivery_failure_reason or 'unknown'}"
                ),
            )
            ledger.event(
                phase="fork",
                event="kanban_followup_delivery_failed_lease_released",
                status=status,
                reason_code="kanban_followup_delivery_failed_lease_released",
                repo=decision.repo,
                cwd=cwd,
                mode="kanban-followup",
                cline_kanban_task_id=task_id,
                tracker_lease_id=failed_lease_release.get("lease_id"),
                tracker_lease_release=failed_lease_release,
                **delivery_receipt_fields,
            )
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
            cline_kanban_idempotency_key=message_payload.get("idempotency_key"),
            **delivery_receipt_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
        )
        if delivery_confirmed:
            followup_delivery_message = (
                "Cline Kanban follow-up user message was injected "
                f"（{describe_terminal_delivery_receipt_for_operator(delivery_receipt)}）。"
            )
        elif delivery_receipt.is_failed:
            followup_delivery_message = (
                "Cline Kanban follow-up **未送达**："
                f"{describe_terminal_delivery_receipt_for_operator(delivery_receipt)}"
                " 本轮不留在途记录；下一次该 task 的 Stop 会按当时的工作树重新决策。"
            )
        else:
            followup_delivery_message = (
                "Cline Kanban follow-up 已被 Kanban 受理，但**尚未确认**成为真实 turn"
                f"（{describe_terminal_delivery_receipt_for_operator(delivery_receipt)}）；"
                "请打开或恢复该 task，让排队中的 $review-validate-fix 被消费。"
                "下一次该 task 的 Stop 会按 idempotency key 查它的真实终态再决定是否重投。"
            )
        success_payload = ledger.hook_payload(
            status=status,
            reason_code=reason_code,
            message=followup_delivery_message,
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
            cline_kanban_idempotency_key=message_payload.get("idempotency_key"),
            **delivery_receipt_fields,
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
        # 仅本「成功派发 self-rising followup」路径置位：resume 在路上 → main 据此保留 park（不 unpark）。
        # hook_payload 顶层无 status 键，故必须用这个显式内部信号；main 在 emit 前 pop 掉、不泄漏给 Claude。
        # F2a：**投递已确定失败时不置位** —— 没有任何 resume 在路上，继续 park 会把卡片永久压在
        # in_progress 上等一个不会到来的 follow-up（正是「乐观回执」那类缺陷的卡片侧同构形态）。
        if not delivery_receipt.is_failed:
            success_payload[_RVF_KEEP_SELF_PARK_FLAG] = True
        return success_payload
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
    return run_review_validate_fix_child_fork(
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
        report = report_only_no_auto_fork_message(repo)
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


# 疑似 transcript schema 漂移时的 loud-skip error 文案（RVF-001 F3）。复用 `session_manifest_failed`
# skip 通路的 `error` 字段承载，使 skip 原因对 dispatcher / systemMessage 显式可见。
SCHEMA_DRIFT_SUSPECTED_ERROR = (
    "session_transcript_schema_drift_suspected: transcript shows file-edit activity "
    "(patch_apply_end or embedded apply_patch) and the worktree is dirty, yet ownership "
    "attribution is empty — suspected Codex rollout schema drift; skipped RVF fork/review "
    "instead of silently degrading to no_codebase_changes. Review manually."
)


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
    committed_base_ref: str | None = None
    try:
        event = context.get("event")
        if isinstance(event, dict):
            repo_resolved = Path(repo).expanduser().resolve()
            committed_baseline = _resolve_committed_round_baseline(
                repo_resolved,
                current_kanban_task_id(event),
                session_hook_id_from_event(event),
            )
            # base 分支 ref：排除 rebase/cherry 压平到第一父链的 base 工作。与 detection 端
            # `maybe_route_committed_round_scope` 走同一个 `_resolve_committed_round_base_ref`
            # （入参仅 repo），detection 与 allocation 同源——见 ac042be 的下界同源教训。
            committed_base_ref = _resolve_committed_round_base_ref(repo_resolved)
    except Exception:
        committed_baseline = None
        committed_base_ref = None
    context["committed_baseline"] = committed_baseline
    context["committed_base_ref"] = committed_base_ref
    try:
        manifest = build_manifest(
            Path(repo).expanduser().resolve(),
            transcript,
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
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
    if isinstance(manifest, dict) and manifest.get("schema_drift_suspected"):
        # Fail-loud（防静默盲）：transcript 有具体编辑证据（patch_apply_end / 内嵌
        # apply_patch）且工作树 dirty，归属却为空——解析器与实际 transcript schema 失配的
        # 签名。emit 一条显式 loud 事件，并返回与 build_manifest 失败同形的 `error`，让两个
        # 调用方（session_scope_gate_payload / precheck_session_scope_for_dirty_route）走
        # **同一个** `session_manifest_failed` loud-skip 通路，产出显式可见的 skip 原因，而非
        # 继续走 allocator → 空 session_units → 静默降级为 no_unassigned_review_scope /
        # no_codebase_changes（RVF-001 F3：守卫先前只 emit warning 后仍 observed=True 继续，
        # 名不副实的 fail-loud）。
        ledger.event(
            phase="gate",
            event="session_transcript_schema_drift_suspected",
            status="warning",
            reason_code="session_transcript_schema_drift_suspected",
            repo=repo,
            cwd=context.get("cwd"),
        )
        return {
            "observed": False,
            "manifest": manifest,
            "error": SCHEMA_DRIFT_SUSPECTED_ERROR,
        }
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


def auto_review_lease_ttl_seconds(context: dict[str, Any]) -> int | None:
    event = context.get("event")
    backend = normalize_backend_from_env(event if isinstance(event, dict) else None)
    if backend == "kanban-followup" and isinstance(event, dict) and current_kanban_task_id(event):
        return _positive_int_from_env(
            KANBAN_FOLLOWUP_LEASE_TTL_ENV,
            DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS,
        )
    return None


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
                committed_base_ref=context.get("committed_base_ref"),
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
            committed_base_ref=context.get("committed_base_ref"),
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

    if isinstance(manifest, dict) and manifest.get("schema_drift_suspected"):
        # tracker-disabled / legacy gate 路径也必须消费 schema_drift 信号（RVF-001 F3）：
        # 否则 `RVF_TRACKER_DISABLE=1` 会话在疑似漂移时仍走到下面 owned_dirty 空判 →
        # no_session_owned_dirty 静默跳过。与非 legacy 路径同形，走 loud-skip。
        ledger.event(
            phase="gate",
            event="session_transcript_schema_drift_suspected",
            status="warning",
            reason_code="session_transcript_schema_drift_suspected",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"transcript": str(transcript)},
        )
        return skip_payload(
            "session manifest failed; skipped RVF fork/review.",
            ledger,
            "session_manifest_failed",
            repo=repo,
            error=SCHEMA_DRIFT_SUSPECTED_ERROR,
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


def report_only_no_auto_fork_message(repo: str) -> str:
    # report-only 模式（CODEX_RVF_MODE=continuation/block）是「刻意不自动 fork」，
    # 非 fork 失败——host-中性消息，不提任何 host 专属 fork 机制（去-codex：原先复用
    # fork_failure_report 会向此中性路泄漏 "Codex GUI fork" / "Codex Desktop control socket"）。
    return (
        "review-validate-fix 处于 report-only 模式（CODEX_RVF_MODE=continuation/block）："
        "本轮不自动创建 review fork。Stop continuation prompt 已禁用，因为它不会创建真正的"
        "新用户 prompt，只会作为 hook system context 出现在当前轨迹中。"
        f" target_repo={repo}。需要用户手动触发 $review-validate-fix。"
    )


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


def reengage_decision(
    reason: str,
    ledger: RunLedger,
    reason_code: str = "kanban_followup_in_progress_reengage",
    *,
    repo: str | None = None,
    cwd: str | None = None,
    backend: str = "off",
    **summary_fields: Any,
) -> StopDecision:
    """force-continue 决策：阻止本次 Stop、把 reason 回灌 agent 令其继续收尾**现有** RVF run，
    既不经 launch gate、也不 dispatch 新 review / 不写 pending marker / 不新建 run_dir。

    用于 in-progress 锁 active + agent 停在「未 handoff」处的 re-engage——破「锁 active 时静默
    放行使 agent 干停、被一路挡停到 handoff/TTL」的同时，**不再 mint 重复 RVF run**（修
    9f9fb78 的 nudge 用「return None → 常规 gate 重派新 run」唤醒、无法区分 agent 真卡住 vs
    合法等待异步 reviewer 的设计缺陷）。

    这是受众 ``agent_must_act_now`` 的第二类消息（(b) 未完成 RVF 的续跑提示）：它**必须**
    驱动 agent 再跑一回合，与 ``skip_payload`` 的 ``{"continue": True, ...}`` 相反（后者放任
    agent 干停）。具体线材（Claude 的非错误 ``additionalContext`` vs Codex 的
    ``decision:block``）由 host adapter 在 emit 时决定，本函数只声明受众。

    ``developer_visible_notice`` 不是可选装饰：历史上 90/92 条阻塞注入不带 systemMessage，
    开发者终端里它们与「hook 真崩了」完全无法区分。
    """
    payload_fields = dict(summary_fields)
    if repo is not None:
        payload_fields.setdefault("repo", repo)
    if cwd is not None:
        payload_fields.setdefault("cwd", cwd)
    if backend != "off":
        payload_fields.setdefault("backend", backend)
    ledger.summary(
        status="reengaged",
        reason_code=reason_code,
        message=reason,
        **payload_fields,
    )
    return StopDecision(
        action="emit",
        reason_code=reason_code,
        repo=repo,
        cwd=cwd,
        backend=backend,
        message=reason,
        summary_fields=payload_fields,
        payload=None,
        status="reengaged",
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=AgentDirectiveStopHookOutput(
            agent_directive_body=reason,
            developer_visible_notice=(
                "review-validate-fix: 已向 agent 注入进行中 RVF 的续跑提示"
                f"（reason={reason_code}）——这是刻意注入，不是 hook 失败。"
            ),
        ),
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
    report = report_only_no_auto_fork_message(repo)
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
    # base 分支 ref：把 rebase/cherry 压平到第一父链的 base 工作排除出可审窗口。
    # 与 allocation 端 `refresh_global_diff_tracker` 调**同一个**
    # `_resolve_committed_round_base_ref`（入参仅 repo_path，天然同源），否则 detection
    # 选中的窗口与 allocation 重建的窗口不一致 → no_session_owned_dirty 误判（ac042be 教训）。
    base_ref = _resolve_committed_round_base_ref(repo_path)
    # Per-commit opt-out audit: surface which round commits the `RVF-Skip-Review`
    # trailer excluded BEFORE the empty-set early return, so even a round whose
    # commits are all opted-out (committed_paths == []) leaves an audit trace
    # instead of looking indistinguishable from "no committed work".
    try:
        skip_review_shas = sorted(_list_round_skip_review_commit_shas(repo_path, baseline, base_ref))
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
            base_ref,
        )
    except Exception:
        committed_paths = []
    if not committed_paths:
        return None
    # OBSERVATION-WINDOW(R9): review_state「已审」真相源过滤 —— 把已被本 session 审过的
    # committed 路径（其 committed 单元全 reviewed 且本 session 拥有）从候选里剔除，使 detection
    # 与 allocation（其单元选择本就 `review_state IN ('available','assigned')`）同源，不再对
    # 已审工作过度 route。SHA 窗口种子（baseline）保留作 bootstrap，保住 orphan 高水位检测；
    # R9 终局删 marker 前须先把 reviewed_head 持久化进 tracker（方案 B），marker 才能卸下窗口
    # 种子角色（见 select_committed_paths_reviewed_by_session 函数头）。best-effort：过滤失败退化
    # 为今日 SHA-only 行为，绝不误剔未审工作。
    try:
        reviewed_committed_paths = select_committed_paths_reviewed_by_session(
            repo=repo_path,
            session_id=session_hook_id_from_event(event),
            candidate_paths=committed_paths,
        )
    except Exception:
        reviewed_committed_paths = set()
    if reviewed_committed_paths:
        committed_paths = [p for p in committed_paths if p not in reviewed_committed_paths]
        ledger.event(
            phase="gate",
            event="committed_round_review_state_filtered",
            status="committed",
            reason_code="committed_round_already_reviewed",
            repo=repo,
            cwd=cwd,
            committed_baseline=baseline,
            review_state_filtered_path_count=len(reviewed_committed_paths),
            remaining_committed_path_count=len(committed_paths),
        )
    if not committed_paths:
        # 全部 committed 路径都已被本 session 审过 → 与 allocation 空 scope 同结果，静默停（不 route）。
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


# ---------------------------------------------------------------------------
# S4：Stop hook 决策点信使（RVF overhaul 核心切片 / 计划 §三·§四 S4·R23·R24·R25）
#
# RVF overhaul 把「要不要审、审什么」的发起决策权从 Stop hook 交给主 agent：Stop hook 不再自动
# fork（拆掉 route_reviewable_scope→run_review_validate_fix_child_fork 的**初始 auto-fork** 链，
# 两函数本体保留作 fallback/观察期，仅其在 evaluate_stop_event 的初始发起调用被替换），而是
#   ① 生成一份**轻量**候选摘要（S2 stop_decision_candidate_summary）+ 指纹；
#   ② 铸一枚决策 token（S1 rvf_decision_token_store，去重/declined 冷却/铸造熔断三闸内建于 mint）；
#   ③ 以受众 agent_must_act_now 的出口把 token + 摘要 + CLI 命令样例注入给主 agent
#      （线材由 host adapter 决定：Claude 走非错误的 hookSpecificOutput.additionalContext，
#      Codex 走 decision:block）；主 agent 简析后经 rvf-agent CLI 显式 start / decline。
# systemMessage 仅通知用户（agent 不可见）。
#
# ## 边界：只拆**初始发起**，不动延续/特殊后端（忠实于「→run_review_validate_fix_child_fork」措辞）
# 延续/特殊后端不是「是否发起 RVF」的决策点，保留原 route_reviewable_scope→launch 语义：
#   * ``kanban-followup``：flow-1-self-rising 子 run 的自我延续（message dispatch，非 child fork）；
#   * ``report-only`` / ``off``：特殊/禁用模式。
# ``normalize_backend_from_env`` 的 ``in_kanban_task`` 天然区分自升子 run（kanban-followup，保留）
# 与主会话初始发起（kanban，注入）。
#
# ## 投递回执三信号归并（评审修正，非乐观预 arm；87e9338 pure-arm 教训）
# 注入投递不可乐观确认。下一次 Stop 事件先 reconcile 上一枚 token：
#   (a) token 已被 CLI 触碰（状态离开 injected → acknowledged/dispatched/declined）= confirmed；
#   (b) TTL 过期但在 transcript 探到注入痕迹（needle ``RVF_DECISION_TOKEN=<token>``，复用
#       _kanban_followup_marker_consumed 的扫描模式）= 投递已确认、只是未决 → 标 expired，静默；
#   (c) TTL 过期且无任一信号 = lost → mark_lost + systemMessage 告警用户，**绝不自动重试**。
#
# ## park / unpark ⟵ token 状态（决策点在途 = 保留 park；显式状态转换表，先表后码）
# | token 状态（本次 Stop 结束时） | 在途? | park 处置              |
# |------------------------------|-------|----------------------|
# | minted / injected            | 是    | 保留 park（决策点等 agent）|
# | acknowledged                 | 是    | 保留 park（start 在途）    |
# | dispatched                   | 否    | unpark（已派发，交常规流程）|
# | declined / declined_cooldown | 否    | unpark（已谢绝）          |
# | lost / expired               | 否    | unpark（决策点已死）      |
# | 无候选 / 熔断 / 无 token       | 否    | unpark（无 resume）      |
# 实现：注入/dedup-pending 决策用「resume 在途」reason_code（见 _DECISION_POINT_RESUME_PENDING_REASONS，
# 经 _skip_decision_should_unpark 保留 park）；其余静默停用普通 reason_code → 照常 unpark。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 发起生命周期分类（P1：取代历史的「延续/特殊后端集合」判据）
#
# 历史实现按 ``backend in {"kanban-followup", "report-only", "off"}`` 决定「这次 Stop 要不要
# 走发起决策点」。那个判据把两件互不相干的事糊在一个字段上：
#
#   * **transport**（这一轮如果要审，消息/进程怎么送达：kanban 新 task / kanban 终端投递 /
#     GUI / dry-run…）；
#   * **lifecycle**（这一次 Stop 到底是「一次全新的发起」，还是「一轮已被接受的 run 的延续」，
#     还是「发起被模式整个关掉了」）。
#
# 糊在一起的直接后果就是本轮要根治的缺陷：``normalize_backend_from_env`` 在
# ``fork_mode=auto`` 时**只按「在不在 kanban task 内」**把 backend 定成 ``kanban-followup``，
# 于是 task 内的自动 RVF 被当成「延续」，结构上到不了决策点——注入给刚停下来的那个 agent 的
# 是一条落地即执行的祈使句，它从来没有机会说「不」。2026-08-08 的
# ``rvf-20260808T090747Z`` 事故（agent 从未接受过那一轮）与其后多次重复派发都由此而来。
#
# 拆开之后：``backend`` 只描述 transport，**不再**参与「新发起 / 延续」分类（INV-4）。
# ---------------------------------------------------------------------------

# 全新发起：本次 Stop 应该注入**发起决策点横幅**，由主 agent 显式 start / decline。
REVIEW_INITIATION_LIFECYCLE_FRESH_INITIATION = "fresh_initiation"
# 已接受且在途：agent 早前已经 start 过一轮，凭证仍 active。不得重新征询（INV-5）。
REVIEW_INITIATION_LIFECYCLE_ACCEPTED_RUN_IN_FLIGHT = "accepted_run_in_flight"
# 模式把发起整个关掉了（``CODEX_RVF_MODE=off`` / ``report-only``）：维持既有早退语义。
REVIEW_INITIATION_LIFECYCLE_DISABLED_BY_MODE = "initiation_disabled_by_mode"
# 操作者**显式**点名了 kanban 终端投递这条延续 transport（``CODEX_RVF_FORK_MODE=kanban-followup``）：
# 这是一条保留的逃生口/兼容入口，仍走原 route_reviewable_scope→投递语义。
REVIEW_INITIATION_LIFECYCLE_EXPLICIT_CONTINUATION_TRANSPORT_OVERRIDE = (
    "explicit_continuation_transport_override"
)

# 发起被模式关掉的两个 backend 值（``route_reviewable_scope`` 内部对它们各有一条早退分支）。
_RVF_INITIATION_DISABLED_BACKENDS = frozenset({"report-only", "off"})


def classify_review_initiation_lifecycle(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    backend: str,
    backend_selection_mode: str,
    cwd: str | None,
) -> str:
    """本次 Stop 在「发起」这件事上处于哪个生命周期阶段。见上方模块注释。

    判定顺序即优先级：

    1. ``off`` / ``report-only`` → 发起被模式关掉，照旧走 ``route_reviewable_scope`` 早退。
    2. **显式**点名 ``CODEX_RVF_FORK_MODE=kanban-followup`` → 保留的延续 transport 逃生口。
       注意判据是 ``backend_selection_mode == "explicit"`` 而不是 backend 值本身：``auto``
       在 task 内也会推出 ``kanban-followup``，而那**正是**要改成走决策点的那条路径。
    3. 读到一枚仍 active 的在途凭证 → ``accepted_run_in_flight``。
    4. 其余 → ``fresh_initiation``。

    第 3 条是**兜底**而非主机制：真正的延续在 ``evaluate_stop_event`` 更早处就被
    ``awaiting_dispatched_agent_decision`` / ``kanban_followup_in_progress_decision`` 接走了
    （两者覆盖 in_progress / waiting_bootstrap / awaiting-decision / pending-in-flight）。走到
    这里还能读到活凭证属于不该发生的状态，故写一条 ``warn`` 级 ledger 让它可观测。
    """
    if backend in _RVF_INITIATION_DISABLED_BACKENDS:
        return REVIEW_INITIATION_LIFECYCLE_DISABLED_BY_MODE
    if backend == "kanban-followup" and backend_selection_mode == "explicit":
        return REVIEW_INITIATION_LIFECYCLE_EXPLICIT_CONTINUATION_TRANSPORT_OVERRIDE

    marker = read_kanban_followup_lock(
        task_id=current_kanban_task_id(event),
        session_id=kanban_followup_lock_session_id(event),
        repo=kanban_followup_lock_repository_anchor(event, cwd=cwd),
    )
    # 只有 **active** 凭证才算在途：stale 凭证按 TTL 自然失效，绝不能让一把忘了清的锁把发起
    # 决策点永久静默掉（87e9338「锁 squat」那一族缺陷的对偶）。
    if marker is not None and kanban_followup_lock_status(marker) == KANBAN_FOLLOWUP_LOCK_ACTIVE:
        ledger.event(
            phase="gate",
            event="review_initiation_accepted_run_still_in_flight_at_dirty_gate",
            status="warning",
            level="warn",
            reason_code="review_initiation_accepted_run_in_flight",
            cwd=cwd,
            backend=backend,
            active_rvf_run_id=marker.get("run_id"),
            kanban_followup_in_progress_marker_path=marker.get("_marker_path"),
            kanban_followup_lock_state=kanban_followup_lock_state(marker),
        )
        return REVIEW_INITIATION_LIFECYCLE_ACCEPTED_RUN_IN_FLIGHT
    return REVIEW_INITIATION_LIFECYCLE_FRESH_INITIATION

# 决策点注入的 reason_code（模型可见 block）。
REASON_DECISION_POINT_INJECTED = "decision_point_injected"
REASON_DECISION_POINT_DEDUPLICATED_PENDING = "decision_point_deduplicated_pending"
REASON_DECISION_POINT_DECLINED_COOLDOWN = "decision_point_declined_cooldown"
REASON_DECISION_POINT_CIRCUIT_BREAKER = "decision_point_circuit_breaker_tripped"
REASON_DECISION_POINT_NO_CANDIDATES = "decision_point_no_candidates"
# 本次 Stop 刚撤掉一枚搁浅横幅 ⇒ 当次就地结束，不在同一次 Stop 里重铸（C-①）。
REASON_DECISION_POINT_STRANDED_DELIVERY_WITHDRAWN_THIS_STOP = (
    "decision_point_stranded_delivery_withdrawn_this_stop"
)
REASON_DECISION_POINT_MINT_ANOMALY = "decision_point_mint_anomaly"
# 带外投递（横幅经 cline-kanban 终端投递成为一条真实 user message）新增的三种结局。
REASON_DECISION_POINT_DELIVERED_OUT_OF_BAND = "decision_point_delivered_out_of_band"
REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_IN_FLIGHT = (
    "decision_point_out_of_band_delivery_in_flight"
)
REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_SINGLE_FLIGHT_BUSY = (
    "decision_point_out_of_band_delivery_single_flight_busy"
)
# 子进程跑完了但回执不可用：横幅**可能**已经在 agent 面前了。这不是失败、也不是成功，
# 是第三种结局，必须有自己的 reason_code —— 复用 delivered 会谎报送达，复用 failed 会触发回落。
REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN = (
    "decision_point_out_of_band_delivery_outcome_unknown"
)
# 跨 turn 再入预算已耗尽 ⇒ 本轮自注入横幅**不发**，已铸的 token 当场撤回。它不是投递失败，
# 也不是 agent 的决策：是 RVF 自己在「宣称 agent 已收到」之前就决定不问这一次。
REASON_DECISION_POINT_REENTRY_BUDGET_EXHAUSTED = "decision_point_reentry_budget_exhausted"

# 撤回理由（写进 token payload 的 ``withdrawn_reason``，供 $rvf-analyze 与人工对账区分撤回成因）。
DECISION_POINT_WITHDRAWN_REASON_REENTRY_BUDGET_EXHAUSTED = "stop-hook-reentry-budget-exhausted"

# 注入尝试 ledger（canary）里表示「本轮铸了 token、把横幅交出去了，但拿不到可解析回执」的取值。
# 刻意不复用 ``INJECTION_OUTCOME_DELIVERED_OUT_OF_BAND``：那个取值断言「已交给终端投递并拿到
# 回执」，用它记这一支就是把「不知道」写成「知道」。字面量定义在此而非 ledger 模块，是因为本轮
# 修复的改动面被限定在 Stop hook / UPS 两个文件；后续应上收进
# ``agentic_initiation_injection_attempt_ledger`` 与其余 INJECTION_OUTCOME_* 同处。
INJECTION_ATTEMPT_OUTCOME_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN = (
    "out_of_band_delivery_outcome_unknown"
)

# 注入尝试 ledger 里表示「候选非空、token 也铸了，但因再入预算耗尽而根本没发出去」的取值。
# 必须单列且 ``injected=False``：记成 ``injected``（哪怕 True/False）都会把 canary 的分子
# 与「确实发过一次横幅」混为一谈，而这一支的全部意思正是**没发**。
INJECTION_ATTEMPT_OUTCOME_REENTRY_BUDGET_EXHAUSTED = "reentry_budget_exhausted"

# park 保留集：这些 skip/emit 决策代表「决策点仍在途」（token minted/injected/acknowledged），
# resume 尚未落定，必须保留 self-park（对偶于 kanban-followup 的 resume-pending 语义）。
_DECISION_POINT_RESUME_PENDING_REASONS = frozenset(
    {
        REASON_DECISION_POINT_INJECTED,
        REASON_DECISION_POINT_DEDUPLICATED_PENDING,
        # 带外投递已交出去、还没被 agent 接到：决策点同样「仍在途」，必须保留 park。
        REASON_DECISION_POINT_DELIVERED_OUT_OF_BAND,
        REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_IN_FLIGHT,
        # 结论未知同样按「仍在途」处置：横幅可能已经落在 agent 面前，unpark 等于宣布它没送到。
        REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN,
    }
)

# 决策点横幅投递通道的显式覆盖（逃生口 + 测试钩子）。取值见
# ``rvf_decision_token_store.DECISION_POINT_DELIVERY_TRANSPORTS``。
DECISION_POINT_DELIVERY_TRANSPORT_ENV = "RVF_DECISION_POINT_DELIVERY_TRANSPORT"
# 只有这两个 backend 意味着「本会话就活在一个 cline-kanban task 的终端里」，横幅才投得进去。
_DECISION_POINT_CLINE_KANBAN_TRANSPORT_BACKENDS = frozenset({"kanban", "kanban-followup"})


def resolve_decision_point_delivery_transport(
    event: dict[str, Any],
    *,
    backend: str | None = None,
    ledger: RunLedger | None = None,
    cwd: str | None = None,
) -> str:
    """决策点横幅走哪条 transport。判的是 **transport 事实**，不是发起 lifecycle。

    按序：

    1. ``RVF_DECISION_POINT_DELIVERY_TRANSPORT`` 显式指定（逃生口 + 测试钩子）→ 直接采用；
       取值非法时记一条 warn 级 ledger 并回落自注入（**绝不**因为一个拼错的 env 就把发起路径整个
       改道）。
    2. 不在 cline-kanban task 内 → 自注入（task 外根本没有可投递的 chat session）。
    3. backend 不属 kanban 家族（``gui`` / ``manual`` / ``dry-run`` …）→ 自注入。
    4. 否则 → 经 cline-kanban 终端投递投成一条真实 user message。

    ``backend_from_values`` 在 ``auto`` 下本就按 ``in_kanban_task`` 分岔——上一轮 P1 把它从
    **lifecycle** 判据上摘了下来，这里正好让它做它本该做的事：描述 transport。
    """
    configured = (os.environ.get(DECISION_POINT_DELIVERY_TRANSPORT_ENV) or "").strip()
    if configured:
        if configured in decision_token_store.DECISION_POINT_DELIVERY_TRANSPORTS:
            return configured
        if ledger is not None:
            ledger.event(
                phase="gate",
                event="decision_point_delivery_transport_override_invalid",
                status="warning",
                level="warn",
                reason_code="decision_point_delivery_transport_override_invalid",
                cwd=cwd,
                configured_transport=configured,
                known_transports=sorted(
                    decision_token_store.DECISION_POINT_DELIVERY_TRANSPORTS
                ),
            )
        return decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
    if not current_kanban_task_id(event):
        return decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
    resolved_backend = backend if backend is not None else normalize_backend_from_env(event)
    if resolved_backend not in _DECISION_POINT_CLINE_KANBAN_TRANSPORT_BACKENDS:
        return decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
    return decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION


def _rvf_agent_cli_path() -> Path:
    """rvf-agent 决策 CLI（S3）的落盘路径——与本 Stop hook 同目录的兄弟脚本，注入提示里渲染成
    可复制粘贴的命令样例。"""
    return Path(__file__).resolve().with_name("rvf_agent_decision_cli.py")


# 候选摘要里承载「这一轮源自哪份会话 transcript」的键。名字与 ``prepare_review_run`` 的 prep
# payload 键逐字相同（本仓一直用这个名字指同一件事），``rvf_agent_decision_cli`` 侧以同名常量
# ``CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY`` 读回；两侧刻意不互相 import（Stop hook 与
# CLI 各自都是重量级入口，互引会把两条冷启动路径绑死）。
CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY = "origin_transcript_path"


def stamp_origin_transcript_path_on_candidate_summary(
    summary: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    """把本次事件的 transcript 路径盖进候选摘要，随 token 一起交给 ``rvf-agent start``。

    **为什么非它不可**：``start`` 的 ``--transcript-path`` 是 agent 手打的可选参数，而 agent 通常
    并不知道自己的 transcript 落在哪；铸 token 的这一刻（Stop / UPS 事件在手）是全链路上唯一确定
    知道该路径的地方。不盖这一份，默认发起路径上 transcript 恒为 None：prepare 生成不了
    session-manifest ⇒ scope contract 从 ``session-owned`` 退化成 ``manual-all-uncommitted``
    （吞进别的会话的背景 dirty），且 tracker scope 连分配的前提都不具备（scope 要 splice 进
    manifest）⇒ 无 lease ⇒ 审完标不了 reviewed ⇒ 同一批改动被后续每次 Stop 反复提议。

    best-effort：解析不出可读 transcript 就写 None（下游据此走显式降级），绝不抛。
    """
    try:
        transcript = first_readable_session_path(event)
    except Exception:  # noqa: BLE001 - 摘要加料永不阻断决策点
        transcript = None
    summary[CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY] = (
        str(transcript) if transcript is not None else None
    )
    return summary


def _render_decision_point_injection_prompt(
    *,
    summary: dict[str, Any],
    token: str,
    fork_context: dict[str, str] | None,
    delivery_transport: str | None = None,
) -> str:
    """渲染决策点横幅正文（两条 transport 共用**同一份**正文，只差两行来源/时效说明）。

    必含（计划 §四 S4 bullet 3）：候选摘要渲染 + token 字符串 + start/scope/dispatch/decline CLI 命令
    样例 + 「简析后立刻 start 或 decline，不要搁置」指引 + R24 递归护栏文案；fork-context（已在 RVF
    fork 内）降级为一行标注（R24 硬闸退役）。needle ``RVF_DECISION_TOKEN=<token>`` 有两个消费者：
    下一次 Stop 的投递回执 (b) 信号在 transcript 中探测，以及带外投递时目标会话的
    UserPromptSubmit hook——**UPS 观测到它 = 横幅确实成了一条真实 user message**（INV-A 的达成信号）。

    ``delivery_transport`` = ``cline_kanban_task_chat_session`` 时补两行：① 来源声明（这条不是开发者
    打的）；② 时效说明（token 有 TTL；工作树若已变，``start`` 会按候选指纹诚实报出失效）。

    ⚠️ **改这段文案必须重跑 UPS 全检测器回归**：正文里两处 ``$review-validate-fix`` 只因被反引号
    包着才没命中 ``RVF_MANUAL_TRIGGER_RE``（它要求前面是行首或空白）。一次无心的换行就会让横幅把
    自己判成手动触发、再铸一枚 token 再注入一遍。"""
    cli = str(_rvf_agent_cli_path())
    repo = str(summary.get("repo") or "")
    count = summary.get("candidate_path_count") or 0
    has_committed = bool(summary.get("has_committed_but_unreviewed"))
    bootstrap = summary.get("bootstrap_size") or {}
    strictness = summary.get("strictness_reference") or {}
    suggested_tier = strictness.get("suggested_strictness_tier") or "standard"
    oversized = bool(strictness.get("oversized_change_elevate_recommended"))
    totals = strictness.get("totals") or {}
    candidates = [
        c
        for c in (summary.get("candidates") or [])
        if isinstance(c, dict) and c.get("included_in_candidate_set")
    ]

    lines: list[str] = []
    lines.append("=== REVIEW-VALIDATE-FIX 发起决策点（agent 决策，非自动发起）===")
    lines.append(
        "本轮工作停下时检测到**本 worktree 自上次 review 以来**存在可审改动。是否发起 RVF 由你"
        "（主 agent）决定：简要评估后，用下面的 rvf-agent CLI **显式** start（发起）或 decline（谢绝）。"
    )
    lines.append(f"RVF_DECISION_TOKEN={token}")
    if (
        delivery_transport
        == decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
    ):
        lines.append("")
        lines.append(
            "  来源：这条消息**不是开发者打的**，是 RVF 的 Stop hook 经 cline-kanban 终端投递投进来的"
            "发起征询。它以一条真实 user message 的形态到达，因此在会话历史里是一个可回退的边界"
            "（双 ESC 的 rewind 列表里能看到它）——用户随时可以回到它之前。"
        )
        lines.append(
            "  时效：这枚 token 有 TTL（20 分钟）。若你读到它时工作树已经又变过，直接照常 start 即可"
            "——CLI 会按候选指纹诚实报出失效，不会拿一份过时的候选集去审。"
        )
    lines.append("")
    lines.append("— 候选摘要（程序化构造，你不需要自己枚举全集）—")
    lines.append(f"  目标 worktree: {repo}")
    lines.append(
        f"  候选文件数: {count}"
        + (f"（含已提交但未审改动）" if has_committed else "（均为未提交改动）")
    )
    lines.append(
        "  规模: 非空新增 {added} 行 / 删除 {deleted} 行 / 工作树 {bytes} 字节".format(
            added=bootstrap.get("total_nonblank_added_lines", 0),
            deleted=bootstrap.get("total_nonblank_deleted_lines", 0),
            bytes=bootstrap.get("total_working_tree_bytes", 0),
        )
    )
    lines.append(
        "  严格度参考档（仅参考，最终你选）: {tier}；加权有效行={weighted}；"
        "非测试非生成改动行={core}{elevate}".format(
            tier=suggested_tier,
            weighted=totals.get("weighted_effective_lines", 0),
            core=totals.get("nonblank_change_lines_excluding_tests_and_generated", 0),
            elevate="；⚠ 改动量超大，建议开工前 elevate 给用户" if oversized else "",
        )
    )
    if candidates:
        lines.append("  候选路径（change_state / ownership）:")
        for c in candidates[:40]:
            lines.append(
                f"    - {c.get('path')}  [{c.get('change_state')} / {c.get('ownership')}]"
            )
        if len(candidates) > 40:
            lines.append(f"    …… 其余 {len(candidates) - 40} 个路径略")
    if fork_context is not None:
        lines.append("")
        lines.append(
            "  标注：当前会话本身疑似已在一个 RVF fork 上下文内（fork-context 命中）。"
            "递归护栏见下方——若这是在审你自己 RVF 的结果，通常应 decline。"
        )
    lines.append("")
    lines.append("— 决策命令（token 已在上方 RVF_DECISION_TOKEN=；把它显式回传）—")
    # ``--transcript-path`` 只在解析得出时才渲染。它决定本轮 scope 口径（session-owned + tracker
    # lease vs 退化的 manual-all-uncommitted），故必须出现在样例里；同一路径也已随候选摘要进了
    # token payload，agent 忘了带这个参数时 CLI 会自行回退读回来——两条路互为兜底。
    origin_transcript_path = summary.get(CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY)
    transcript_flag = (
        f" --transcript-path {shlex.quote(str(origin_transcript_path))}"
        if isinstance(origin_transcript_path, str) and origin_transcript_path
        else ""
    )
    lines.append(
        f"  发起（简析后开审）: python3 {cli} start --token {token}{transcript_flag} "
        f"--reason \"<一句话理由>\""
    )
    lines.append(
        f"  自动 scope 微调（可选，仅 start 后 acknowledged 态）: "
        f"python3 {cli} scope exclude --token {token} <path>... --reason \"<理由>\""
    )
    lines.append(
        f"  定稿派发（选严格度档 light|standard|strict）: "
        f"python3 {cli} dispatch --token {token} --strictness {suggested_tier}"
    )
    lines.append(f"  谢绝（不发起）: python3 {cli} decline --token {token} --reason \"<一句话理由>\"")
    lines.append("")
    lines.append("— 接受之后怎么进入执行（**不会再有第二条注入消息**）—")
    lines.append(
        "  `start` 的回执里带一整块 `execution_handoff`（人类可读行 + `RVF_AGENT_RESULT_JSON:` "
        "两种形态都有）：target_repo / prep_file / review_env / review_packet / scope_contract / "
        "scope_of_work / origin_metadata / handoff_path 全在里面。**执行所需的一切都从这份回执读**，"
        "不要等一条 `$review-validate-fix` 开头的消息送上门——那条自注入已经退役。"
    )
    lines.append(
        "  流程：`start` →（可选 `scope` 微调）→ `dispatch` → 由**你自己**在当前会话调用 "
        "`$review-validate-fix` skill，按 SKILL.md 就地走 review → merge → validate/fix → handoff。"
        "不要新建 Kanban task，也不要 fork 新会话。"
    )
    lines.append("")
    lines.append(
        "指引：**简析后立刻 start 或 decline，不要搁置**——决策 token 有 TTL，超时无任一信号会被标"
        "为 lost 并告警。start 后紧接着 dispatch 才真正派发 reviewer；start 只是重量级准备，失败可"
        "`start --retry`。"
    )
    lines.append("")
    lines.append(
        "递归护栏（R24）：不应对自己 RVF 的 result 再次 RVF，除非该 result 有效改动量超大（不含"
        "测试 >1000 行）—— 而超大改动本应在**开工前 elevate 给用户**，而非直接进行。若本次候选就是"
        "你刚跑完的一轮 RVF 的产物，请 decline 并在 reason 里说明。"
    )
    return "\n".join(lines)


def _decision_token_transcript_trace_present(event: dict[str, Any], token: str) -> bool:
    """投递回执信号 (b)：在父会话 transcript 里探 needle ``RVF_DECISION_TOKEN=<token>``（复用
    _kanban_followup_marker_consumed 的扫描模式）。文件缺失/过大/异常 → False（保守判「无痕迹」，
    宁可把「投递成功但未决」误判为 lost 告警，也不漏报静默丢投）。"""
    transcript = first_readable_session_path(event)
    if transcript is None:
        return False
    try:
        if transcript.stat().st_size > KANBAN_FOLLOWUP_CONSUMED_SCAN_MAX_BYTES:
            return False
        needle = f"RVF_DECISION_TOKEN={token}"
        return needle in transcript.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def reconcile_pending_decision_token_receipts(
    event: dict[str, Any],
    *,
    worktree: str,
    session_id: str | None,
    ledger: RunLedger,
) -> list[str]:
    """在铸新 token 前 reconcile 本 (worktree, session) 名下的上一枚决策 token 的投递回执。

    对每枚**仍处 minted/injected（未被 CLI 触碰）且 TTL 已过期**的 token：
      * transcript 探到注入痕迹 (b) → mark_expired（投递已确认、只是 agent 未决），静默不告警；
      * 否则 (c) → mark_lost + 收集一条 systemMessage 告警文案，**绝不自动重试**。
    acknowledged（start 在途）/ 终态 token 一律跳过。整段 best-effort，永不抛、不阻断主流程。
    返回给用户的 lost 告警文案列表（并入 systemMessage）。"""
    warnings: list[str] = []
    try:
        # 枚举实现收敛在 S1 的 ``iter_decision_tokens_for_session``：投递回执对账 / 带外在途查询 /
        # rewind 对账三处共用一份，避免各自 glob 再在字段名上分叉。
        lookups = decision_token_store.iter_decision_tokens_for_session(
            worktree=worktree, session_id=session_id
        )
    except Exception:  # pragma: no cover - 枚举永不阻断
        return warnings
    for lookup in lookups:
        # 仅 TTL 已过期且仍在 minted/injected（未被 CLI 触碰、未复活）的 token 才需要 reconcile。
        if lookup.status != "expired":
            continue
        if lookup.state not in (decision_token_store.STATE_MINTED, decision_token_store.STATE_INJECTED):
            continue
        token = lookup.token
        try:
            if _decision_token_transcript_trace_present(event, token):
                decision_token_store.mark_expired(
                    token, reason="delivery-confirmed-but-no-decision-within-ttl"
                )
                ledger.event(
                    phase="gate",
                    event="decision_token_receipt_expired",
                    status="completed",
                    reason_code="decision_token_delivery_confirmed_no_decision",
                    decision_token=token,
                    worktree=worktree,
                    session_id=session_id,
                )
                continue
            decision_token_store.mark_lost(token, reason="no-delivery-signal-within-ttl")
            ledger.event(
                phase="gate",
                event="decision_token_receipt_lost",
                status="warning",
                level="warn",
                reason_code="decision_token_delivery_lost",
                decision_token=token,
                worktree=worktree,
                session_id=session_id,
            )
            warnings.append(
                f"决策 token {token} 在 TTL 内既未被 rvf-agent CLI 触碰、也未在 transcript 探到"
                "投递痕迹，已标记为 lost（发起层疑似静默丢投）。**不会自动重试**；如仍需审查，"
                "请在下一轮真实改动后重新触发，或手动 $review-validate-fix。"
            )
        except decision_token_store.DecisionTokenError:
            continue
    return warnings


def reconcile_rewound_decision_point_deliveries(
    event: dict[str, Any],
    *,
    worktree: str,
    session_id: str | None,
    ledger: RunLedger,
) -> list[str]:
    """INV-C：用户把某次发起征询 **rewind 掉**之后，那枚 token 必须当轮作废。

    这条不是可选项，它是「能 rewind」这个功能的**另一半**：没有它，用户刚刚刻意回退掉的征询
    会因为去重索引仍活着而把接下来整个 TTL 窗口堵死——而带外 transport 的 1200s TTL 还把这个
    死区拉得更长。

    **只对带外投递（``cline_kanban_task_chat_session``）的 token 成立，故必须先按 transport 过滤。**
    判据 ``off_ancestor_chain_rewound`` 的隐含前提是「这条 needle 在 transcript 里出现 = 那份横幅
    当初落成了条目」，而这个前提只有带外横幅满足——它是以一条**真实 user message** 进 transcript 的。
    自注入横幅的正文走 hook stdout，**根本不进** ``message.content``：本仓 12.9MB 真实 transcript 上，
    一枚确实被自注入过的 token 探出来的结果是 ``absent_from_transcript``。所以自注入 token 上判出的
    ``off_ancestor_chain_rewound`` 只可能来自**非横幅**的其它出现（agent 在自己的回复里引用了该
    token、被聚合进 ``promptSource=system`` 的条目等），与「用户有没有 rewind」毫无关系——照判就是
    假阳性撤回 + 向开发者输出「用户 rewind 掉了这次征询」这个错误归因。兄弟函数
    ``find_decision_token_awaiting_out_of_band_delivery`` 一直有这道过滤，本函数缺它是不对称。

    ⚠️ **已知缺口（刻意不在本轮补）**：自注入 token 因此**没有** rewind 对账。补它需要一个对自注入
    成立的、不同的证据来源（stdout 正文并不落进 transcript，得另找锚点），超出本轮范围。现状下
    自注入 token 被 rewind 后只能等 TTL 对账（``mark_lost``）兜底。

    判据只认**确凿证据**：``classify_dispatch_trigger_presence`` 回
    ``off_ancestor_chain_rewound``（token 在 transcript 里出现过、但不在当前 leaf 的祖先链上）。
    另外两种结局一律不动手：

    * ``absent_from_transcript`` —— 可能只是「刚投出去还没落地」，withdraw 它等于把一条在途的
      征询掐掉；这一支归 TTL 对账（``mark_lost``）管。
    * ``unknown_transcript_unreadable`` —— 没有证据就不下结论。

    ``withdrawn`` 进入即移出去重索引、**不计 declined 冷却、不计铸造熔断**，于是下一次 Stop
    能且只能重新征询一次。整段 best-effort，永不抛、不阻断主流程。
    """
    notices: list[str] = []
    transcript = first_readable_session_path(event)
    if transcript is None:
        return notices
    try:
        lookups = decision_token_store.iter_decision_tokens_for_session(
            worktree=worktree, session_id=session_id
        )
    except Exception:  # pragma: no cover - 枚举永不阻断
        return notices
    for lookup in lookups:
        if lookup.state not in decision_token_store.ACTIVE_NONTERMINAL_STATES:
            continue
        # transport 过滤（见 docstring）：``None``（本轮之前铸的）与 ``stop_hook_self_injection``
        # 都不带外投递，它们的正文从未进过 transcript，off-ancestor-chain 判定对它们无效。
        lookup_payload = lookup.payload if isinstance(lookup.payload, dict) else {}
        if (
            lookup_payload.get("decision_point_delivery_transport")
            != decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
        ):
            continue
        presence = classify_dispatch_trigger_presence(
            transcript_path=transcript,
            dispatch_token=lookup.token,
            trigger_literal=decision_token_literal(lookup.token),
        )
        if presence.presence != TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND:
            continue
        payload = lookup.payload or {}
        try:
            decision_token_store.mark_withdrawn(
                lookup.token,
                reason="user_rewound_decision_point_banner",
                withdrawal_cause=(
                    decision_token_store.DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER
                ),
            )
        except decision_token_store.DecisionTokenError:
            continue
        task_id = None
        detail = payload.get("out_of_band_delivery_detail")
        if isinstance(detail, dict):
            task_id = detail.get("kanban_task_id")
        if isinstance(task_id, str) and task_id:
            try:
                clear_decision_point_delivery_intent(
                    task_id=task_id, decision_token=lookup.token
                )
            except Exception:  # noqa: BLE001 - 清理 best-effort
                pass
        ledger.event(
            phase="gate",
            event="decision_point_withdrawn_after_user_rewind",
            status="completed",
            reason_code="decision_point_withdrawn_after_user_rewind",
            repo=worktree,
            session_id=session_id,
            decision_token=lookup.token,
            decision_point_delivery_transport=payload.get(
                "decision_point_delivery_transport"
            ),
            decision_token_state_before_withdrawal=lookup.state,
            **presence.as_ledger_fields(),
        )
        notices.append(
            f"决策 token {lookup.token} 承载的那次发起征询已不在会话祖先链上（用户 rewind），"
            "已作废该 token 并从去重索引移除；下一次 Stop 会重新征询一次（不计冷却、不计熔断）。"
        )
    return notices


@dataclass(frozen=True)
class StrandedDecisionPointDeliveryReconciliationOutcome:
    """一次「搁浅横幅」对账的结果：给开发者的旁注，以及**本次真的撤回了哪些 token**。

    之所以不再只返回 notices：调用方需要能区分「本轮什么都没撤」与「本轮刚撤掉一枚」——
    后者必须让**当次 Stop 就地结束**，否则控制流会立刻落回铸造路径，在同一次 Stop 里重铸一枚
    新 token（实测间隔 6~7 秒）。notices 是给人看的文本，拿文本非空当控制流判据是脆的。

    撤回清单**按投递通道归属分成两份**，因为对账的枚举面（跨 task 全局）比早退的判据面
    （本轮这条投递通道）大得多：

    * ``withdrawn_decision_tokens_on_this_stop_delivery_channel`` —— 意图的 worktree/cwd/
      kanban task 与本轮 Stop 对得上。**只有这一份**够格触发早退：早退的立论是「刚撤掉一枚 =
      这条投递通道此刻不通」，而这句话只对**同一条通道**成立。
    * ``withdrawn_decision_tokens_garbage_collected_from_other_delivery_channels`` —— 别的
      task/worktree 遗留的搁浅意图。清理它们本来就是这个函数该做的垃圾回收（少清 = 一份迟到
      落地的横幅照样能跑出一轮无 lease 的 review），但它们**与本轮该不该征询无关**：拿它们
      早退等于让 task A 的一枚残留意图把 repo B 的脏改动整轮压掉（漏审方向）。
    """

    developer_visible_notices: list[str]
    withdrawn_decision_tokens_on_this_stop_delivery_channel: list[str]
    withdrawn_decision_tokens_garbage_collected_from_other_delivery_channels: list[str]

    @property
    def withdrew_any_stranded_delivery_on_this_stop_delivery_channel(self) -> bool:
        return bool(self.withdrawn_decision_tokens_on_this_stop_delivery_channel)


def _normalized_path_for_delivery_channel_identity(value: Any) -> str | None:
    """把一个路径串归一到可比较形态（``~`` 展开 + symlink 解析）。不可解析时退回原串。

    投递意图里的 ``worktree`` 写的是候选摘要算出来的 repo 路径（已 resolve），而 Stop 侧手里的
    ``repo``/``cwd`` 未必经过同一道解析（macOS 上 ``/tmp`` 与 ``/private/tmp`` 就是典型差异）。
    不归一就会把「同一个 worktree」判成两个，早退判据随之失效。
    """
    if not (isinstance(value, str) and value.strip()):
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        return value.strip()


def stranded_decision_point_delivery_intent_belongs_to_this_stop_delivery_channel(
    marker: dict[str, Any],
    *,
    stop_worktree: str | None,
    stop_cwd: str | None,
    stop_kanban_task_id: str | None,
) -> bool:
    """这条搁浅投递意图，是不是**本轮 Stop 自己那条投递通道**上的？

    ``iter_decision_point_delivery_intents`` 是**跨 task 全局枚举**（它的 docstring 逐字写着
    「枚举全部投递意图（跨 task）」），而投递通道是按 kanban task 的 chat session 分的、
    候选集是按 worktree 分的。两者都不是全局的，所以「刚撤掉一枚」这个早退判据必须先收敛到
    本轮这条通道上。

    判据是**正向证据**（拿不到证据即判「不属于本轮」）：

    * 路径证据：意图记的 ``worktree``/``cwd`` 与本轮的 repo/cwd 有任一项归一后相等 ⇒ 同一个
      worktree ⇒ 本轮要重算的候选集与那枚搁浅 token 当初承载的是同一批改动。
    * 通道证据：意图记的 ``kanban_task_id`` 与本轮 Stop 所处的 kanban task 相同 ⇒ 同一条
      chat session ⇒ 「这条通道此刻不通」直接成立。

    方向选择：拿不到证据时判「不属于」（=**不**早退）。误判成属于是漏审（本轮候选被整轮压掉、
    连候选摘要都不算）；误判成不属于最多是同一次 Stop 里多铸一枚——那一支还有铸造熔断兜着。
    """
    marker_paths = {
        _normalized_path_for_delivery_channel_identity(marker.get("worktree")),
        _normalized_path_for_delivery_channel_identity(marker.get("cwd")),
    } - {None}
    stop_paths = {
        _normalized_path_for_delivery_channel_identity(stop_worktree),
        _normalized_path_for_delivery_channel_identity(stop_cwd),
    } - {None}
    if marker_paths & stop_paths:
        return True
    marker_task_id = marker.get("kanban_task_id")
    return bool(
        isinstance(marker_task_id, str)
        and marker_task_id.strip()
        and isinstance(stop_kanban_task_id, str)
        and stop_kanban_task_id.strip()
        and marker_task_id.strip() == stop_kanban_task_id.strip()
    )


def classify_stranded_decision_point_delivery_withdrawal_cause(
    *,
    transcript_path: Path | None,
    decision_token: str,
) -> tuple[str, TriggerPresence]:
    """一枚搁浅 token 该按哪个**成因**撤回（决定它烧不烧熔断额度）。返回 ``(成因, 证据)``。

    ``state`` 仍停在 ``minted`` **不足以**断言「从未成为 turn」：
    ``rvf_user_prompt_submit._observe_delivered_decision_point_banner`` 在 ``mark_injected``
    抛 ``DecisionTokenExpiredError``（横幅确实落地了、但排在一个长回合之后越过了 1200s TTL）时
    **提前返回且刻意不清投递意图**，而 ``read_decision_token`` 对已过 TTL 的 token 照样返回
    ``state=minted``。于是「agent 看到过横幅」与「横幅从未落地」在对账侧形状完全一致。

    唯一能把这两者分开的现成证据是 transcript：带外横幅是以一条**真实 user message** 进
    transcript 的（这正是 ``reconcile_rewound_decision_point_deliveries`` 的立论），所以
    ``RVF_DECISION_TOKEN=<token>`` 这枚 needle 出现与否就是「它有没有成为一条 turn」的直答。

    三分：

    * 在祖先链上 ⇒ 横幅**确实**成为了 turn，只是记账没跟上 ⇒ 通道健康，冲销额度（不然连续 3 次
      记账失败就把熔断打跳、整个 session 静默封禁一个窗口 = 漏审方向）；
    * 不在祖先链上但出现过 ⇒ 横幅成为过 turn 且被用户 rewind 掉了 ⇒ 复用 ``rewound_by_user``
      （与 ``reconcile_rewound_decision_point_deliveries`` 同一事实、同一处置，不另造成因）；
    * 整份 transcript 里找不到 / transcript 不可读（含调用方判定「这条意图不属于本轮这条通道」
      因而显式传 ``None``）⇒ 维持 ``never_became_a_turn``（保留额度）。
      不可读这一支刻意**不**走「安全方向」：保留额度是 C-② 引入的**唯一**退避手段，把无证据的
      那一支也改成冲销等于把退避整个删掉，实测过的空转环（一个 session 连铸三枚、全程
      ``minted → withdrawn``）会立刻回来。残留缺口记在下面。

    ⚠️ **已知残留**：拿不到 transcript 的 host 上，「横幅落地过但记账失败」仍会被按空转计费。
    彻底修法是让 UPS 在那条失败分支上把事实写进投递意图 marker（``rvf_user_prompt_submit.py``
    不在本轮改动面内），使对账侧无需 transcript 也能区分。
    """
    presence = classify_dispatch_trigger_presence(
        transcript_path=transcript_path,
        dispatch_token=decision_token,
        trigger_literal=decision_token_literal(decision_token),
    )
    if presence.presence == TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN:
        return (
            decision_token_store
            .DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED,
            presence,
        )
    if presence.presence == TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND:
        return (
            decision_token_store.DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER,
            presence,
        )
    return (
        decision_token_store
        .DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN,
        presence,
    )


def reconcile_stranded_decision_point_delivery_intents(
    event: dict[str, Any],
    *,
    ledger: RunLedger,
    cwd: str | None,
    worktree: str | None,
) -> StrandedDecisionPointDeliveryReconciliationOutcome:
    """D6：一份**投出去却始终没成为 turn** 的横幅意图到期后，必须真的把它中和掉。

    F2a 的安全撤回抢的是 **dispatch token** 的 tombstone，而消费侧只在命中
    ``RVF_DISPATCH=token=`` 时才查 tombstone——横幅根本不带那个 needle，所以**撤回对横幅是空的**：
    一轮被判「已撤回」的横幅，20 分钟后落地照样能跑出一轮无 lease、无 scope 契约的 review。

    修法不是再造一套 tombstone，而是**直接把 decision token 推进 ``withdrawn`` 终态**——token
    store 本身就是权威，``rvf-agent start`` 读到终态自然硬失败（既有 ``DecisionTokenStateError``
    路径）。落地顺序刻意是「先物理取消、再语义中和」：取消能赶上就根本不会落地；赶不上也有终态兜底。

    只处理 **stale**（TTL 已过）的意图。意图 TTL（900s）严格小于带外 token TTL（1200s），
    所以走到这里时 token 一定还读得出来——这正是两个 TTL 那样取值的原因。

    **枚举面 ≠ 早退面。** ``iter_decision_point_delivery_intents`` 是跨 task 全局枚举，垃圾回收
    本来就该跨 task 做；但返回的撤回清单按 ``worktree``/``cwd``/kanban task 归属拆成两份，
    只有本轮这条投递通道上的那份够格触发调用方的当次早退——判据与理由见
    ``stranded_decision_point_delivery_intent_belongs_to_this_stop_delivery_channel``。

    撤回**成因**按 transcript 证据三分（见
    ``classify_stranded_decision_point_delivery_withdrawal_cause``）：``state`` 停在 ``minted``
    并不蕴含「从未成为 turn」，UPS 的 ``mark_injected`` 失败分支会留下形状完全相同的一枚。
    """
    notices: list[str] = []
    withdrawn_tokens_on_this_stop_delivery_channel: list[str] = []
    withdrawn_tokens_from_other_delivery_channels: list[str] = []

    def _outcome() -> StrandedDecisionPointDeliveryReconciliationOutcome:
        return StrandedDecisionPointDeliveryReconciliationOutcome(
            developer_visible_notices=notices,
            withdrawn_decision_tokens_on_this_stop_delivery_channel=(
                withdrawn_tokens_on_this_stop_delivery_channel
            ),
            withdrawn_decision_tokens_garbage_collected_from_other_delivery_channels=(
                withdrawn_tokens_from_other_delivery_channels
            ),
        )

    stop_kanban_task_id = current_kanban_task_id(event)
    # 成因判定要用的 transcript：本轮 Stop 事件自带的那份（拿不到即 ``None``，成因分类器会据此
    # 落到「无结论」那一支）。只解析一次，避免每条意图各扫一遍长 jsonl。
    stop_transcript_path = first_readable_session_path(event)
    try:
        markers = iter_decision_point_delivery_intents()
    except Exception:  # pragma: no cover - 枚举永不阻断
        return _outcome()
    for marker in markers:
        if decision_point_delivery_intent_status(marker) == KANBAN_FOLLOWUP_LOCK_ACTIVE:
            continue
        token = marker.get("decision_token")
        task_id = marker.get("kanban_task_id")
        if not (isinstance(token, str) and token and isinstance(task_id, str) and task_id):
            continue
        lookup = decision_token_store.read_decision_token(token)
        if lookup.state not in decision_token_store.ACTIVE_NONTERMINAL_STATES:
            # 已被决策 / 已作废：意图只是没被清干净，清掉即可，绝不再动 token。
            try:
                clear_decision_point_delivery_intent(task_id=task_id, decision_token=token)
            except Exception:  # noqa: BLE001
                pass
            continue
        if lookup.state != decision_token_store.STATE_MINTED:
            # 已 injected/acknowledged = agent 确实拿到了这份正文，横幅**没有**搁浅。
            try:
                clear_decision_point_delivery_intent(task_id=task_id, decision_token=token)
            except Exception:  # noqa: BLE001
                pass
            continue

        belongs_to_this_stop_delivery_channel = (
            stranded_decision_point_delivery_intent_belongs_to_this_stop_delivery_channel(
                marker,
                stop_worktree=worktree,
                stop_cwd=cwd,
                stop_kanban_task_id=stop_kanban_task_id,
            )
        )
        # 成因判定只对**本轮这条通道**的意图使用本轮的 transcript：别的 worktree/task 那枚
        # token 的横幅从来就不该出现在这份 transcript 里，拿它扫出来的 ``absent_from_transcript``
        # 是「扫错了文件」而不是「那份横幅没落地」。故对跨通道的那些显式传 ``None``（=无结论），
        # 让它们落到与改动前逐字相同的 ``never_became_a_turn`` 上。
        withdrawal_cause, banner_presence = (
            classify_stranded_decision_point_delivery_withdrawal_cause(
                transcript_path=(
                    stop_transcript_path if belongs_to_this_stop_delivery_channel else None
                ),
                decision_token=token,
            )
        )
        banner_became_a_turn = (
            withdrawal_cause
            != decision_token_store
            .DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN
        )
        cancel_diagnostics = _cancel_stranded_decision_point_delivery(marker)
        try:
            decision_token_store.mark_withdrawn(
                token,
                reason=f"decision_point_delivery_stranded:{withdrawal_cause}",
                withdrawal_cause=withdrawal_cause,
            )
            withdrawn = True
            if belongs_to_this_stop_delivery_channel:
                withdrawn_tokens_on_this_stop_delivery_channel.append(token)
            else:
                withdrawn_tokens_from_other_delivery_channels.append(token)
        except decision_token_store.DecisionTokenError as exc:
            withdrawn = False
            cancel_diagnostics["mark_withdrawn_error"] = f"{type(exc).__name__}: {exc}"
        try:
            clear_decision_point_delivery_intent(task_id=task_id, decision_token=token)
        except Exception:  # noqa: BLE001
            pass
        ledger.event(
            phase="gate",
            event="decision_point_out_of_band_delivery_stranded_withdrawn",
            status="warning",
            level="warn",
            reason_code="decision_point_out_of_band_delivery_stranded_withdrawn",
            cwd=cwd,
            decision_token=token,
            cline_kanban_task_id=task_id,
            decision_point_delivery_intent_path=marker.get("_marker_path"),
            decision_point_delivery_intent_state=marker.get("state"),
            decision_token_withdrawn=withdrawn,
            decision_point_delivery_intent_worktree=marker.get("worktree"),
            stranded_delivery_intent_belongs_to_this_stop_delivery_channel=(
                belongs_to_this_stop_delivery_channel
            ),
            decision_point_stranded_withdrawal_cause=withdrawal_cause,
            **banner_presence.as_ledger_fields(),
            **cancel_diagnostics,
        )
        # 文案按三件事分叉：撤回成没成、横幅到底有没有成为过 turn、这条意图是不是本轮这条通道的。
        # 撤回失败时 token 仍活着、去重索引还在，下面的 mint 会命中 dedup 静默停（不会空转），
        # 所以既不早退、也不能宣称「已撤回」。
        if not withdrawn:
            notices.append(
                f"发起横幅（token={token}）投出去后始终没成为一条真实 user message，"
                "但把它推进 withdrawn 终态**失败**了（详见 ledger 的 mark_withdrawn_error）。"
                "该 token 仍处活跃态、去重索引仍在，故本次 Stop 不会重铸；"
                "它会由 TTL 对账（mark_lost）兜底。"
            )
            continue
        if not belongs_to_this_stop_delivery_channel:
            notices.append(
                f"顺带清理了**另一条投递通道**遗留的搁浅横幅（token={token}，"
                f"task={task_id}，worktree={marker.get('worktree')}）：已撤回该 token 并清掉意图。"
                "它与本 worktree 本轮该不该征询无关，**不影响本次 Stop 的发起决策**。"
            )
            continue
        if banner_became_a_turn:
            notices.append(
                f"发起横幅（token={token}）**确实**作为一条真实 user message 落地过"
                f"（transcript 证据：{banner_presence.presence}），但决策 token 没能推进到 injected"
                "（多半是横幅排在长回合之后越过了 TTL）。已撤回该 token；"
                f"撤回成因记为 {withdrawal_cause}，**冲销**这次铸造占用的熔断额度——通道是通的，"
                "不该按空转退避。**本次 Stop 不再重铸**，重新征询最早发生在下一次 Stop。"
            )
        else:
            notices.append(
                f"发起横幅（token={token}）投出去后在有效期内始终没成为一条真实 user message"
                f"（transcript 证据：{banner_presence.presence}），"
                "已撤回该 token（即便消息迟到落地，rvf-agent start 也会硬失败）。"
                "**本次 Stop 不再重铸**——重新征询最早发生在下一次 Stop，且这次空转会如实计入"
                "铸造熔断（通道持续坏着时自动退避；手动 /review-validate-fix 不受影响）。"
            )
    return _outcome()


def _cancel_stranded_decision_point_delivery(marker: dict[str, Any]) -> dict[str, Any]:
    """best-effort 物理取消一条搁浅的横幅投递（缺 project_path / idempotency_key 即跳过）。"""
    diagnostics: dict[str, Any] = {}
    project_path = marker.get("kanban_project_path")
    task_id = marker.get("kanban_task_id")
    idempotency_key = marker.get("idempotency_key")
    if not (
        isinstance(project_path, str)
        and project_path.strip()
        and isinstance(task_id, str)
        and task_id.strip()
        and isinstance(idempotency_key, str)
        and idempotency_key.strip()
    ):
        diagnostics["decision_point_delivery_physical_cancel"] = {
            "attempted": False,
            "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_MISSING_QUERY_KEYS,
        }
        return diagnostics
    cancel_timeout = _kanban_followup_env_float(
        KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_ENV,
        DEFAULT_KANBAN_FOLLOWUP_MESSAGE_STATUS_TIMEOUT_SECONDS,
    )
    try:
        cancel_payload = cancel_task_message_delivery(
            task_cmd=os.environ.get(
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD
            ),
            repo=Path(project_path).expanduser(),
            task_id=task_id.strip(),
            idempotency_key=idempotency_key.strip(),
            timeout=cancel_timeout if cancel_timeout > 0 else None,
        )
    except KanbanTerminalDeliveryInterfaceUnavailableError:
        diagnostics["decision_point_delivery_physical_cancel"] = {
            "attempted": True,
            "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_INTERFACE_UNAVAILABLE,
        }
    except KanbanTerminalDeliveryRecordNotFoundError:
        diagnostics["decision_point_delivery_physical_cancel"] = {
            "attempted": True,
            "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_RECORD_NOT_FOUND,
        }
    except Exception as exc:  # noqa: BLE001 - 取消失败绝不阻断撤回
        diagnostics["decision_point_delivery_physical_cancel"] = {
            "attempted": True,
            "outcome": TERMINAL_DELIVERY_QUERY_DEGRADED_QUERY_FAILED,
            "error": f"{type(exc).__name__}: {exc}",
        }
    else:
        diagnostics["decision_point_delivery_physical_cancel"] = {
            "attempted": True,
            "outcome": "completed",
            # 两个问题、两个具名函数：``cancel_result`` 只回答「消息是否已确认进入 agent」。
            "message_already_reached_agent": (
                cancel_receipt_indicates_message_already_reached_agent(cancel_payload)
            ),
            "terminal_delivery_status": cancel_payload.get("terminal_delivery_status"),
            "terminal_delivery_failure_reason": cancel_payload.get(
                "terminal_delivery_failure_reason"
            ),
        }
    return diagnostics


def decision_point_injection_decision(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
    cwd: str | None,
    *,
    gate_status: str,
    fork_context: dict[str, str] | None,
    backend: str | None = None,
) -> StopDecision:
    """初始发起决策点：轻量候选摘要 → 选 transport → mint（三闸内建）→ 交付或静默停。

    * 候选摘要经 S2 build_candidate_summary（纯 git+tracker 缓存，含 committed-but-unreviewed，先过
      workspace 守卫）；无候选 → 静默停（不写 injection ledger，避免污染 canary 分母）。
    * 候选非空 → 先 reconcile 上一枚 token 的投递回执（lost 告警并入 systemMessage）与 rewind
      对账（INV-C）→ 按 ``resolve_decision_point_delivery_transport`` 选 transport：
        - **带外投递**（在 cline-kanban task 内）：整段交给
          ``_decision_point_out_of_band_delivery_decision``——横幅成为一条真实 user message，
          于是在 Claude Code 的原生 rewind 列表里是一个可回退的边界（本轮存在的理由）；
        - **同步自注入**（task 外 / 非 kanban backend / 逃生口）：mint 后：
            - minted：mark_injected + 以受众 agent_must_act_now 返回（模型可见；线材由 host adapter 决定）；
            - deduplicated：同键活跃 token 已在等待 → 不重注入（R25 幂等），静默停并提醒用户；
            - declined_cooldown / circuit_breaker_tripped：静默停（后者附熔断告警 systemMessage）。
    * **无论哪种候选非空结局都写 injection-attempt ledger**（负路径不变量：候选非空 ⇒ 有记录）。

    接入点标注（S9；范围铁律：以下均**不在本 repo 实现**，录入 Linear 跟踪）：

    * INTEGRATION-POINT(R6 self-injection queue)：本函数是未来 self-injection queue 的
      唯一接管点——queue 系统落地后，「铸 token → 直接 block 注入」改为「铸 token →
      入 queue → 由 queue 按主 agent 负载/优先级择机投递」；三闸、指纹冷却、注入
      ledger 的语义与分母口径必须原样保留。
    * INTEGRATION-POINT(R12 fork≽checkpoint)：决策注入现阶段一律发给**主 agent 本人**
      （R12）。未来分身/task-force 系统若要把决策点改投给 fork 分身，验收条件 =
      fork 机制在「上下文保真 + 决策可追溯 + decline 理由持久化」三项上**不劣于**
      checkpoint/rewind 机制（fork≽checkpoint），否则不得切换注入对象。
    """
    # 每次 Stop 都先做一次「搁浅横幅」对账（D6）。**必须早于候选摘要**：一份投出去却从未成为
    # turn 的横幅，其危害与本轮有没有新候选无关——不撤回它，它 20 分钟后落地照样能跑出一轮
    # 无 lease、无 scope 契约的 review。
    stranded_reconciliation = reconcile_stranded_decision_point_delivery_intents(
        event, ledger=ledger, cwd=cwd, worktree=repo
    )
    stranded_notices = stranded_reconciliation.developer_visible_notices

    # C-①：本次 Stop 刚把**本轮这条投递通道上**的一枚搁浅横幅推进 ``withdrawn`` ⇒ **当次就地结束**。
    #
    # 撤回与重铸原本挨在同一条控制流上：对账只返回 notices、不返回决策，于是撤回后控制流直接
    # 落回下面的候选摘要 → mint 三闸，在**同一次 Stop** 里铸出一枚新 token（实测间隔 6~7 秒，
    # 一个 session 内连出三枚，全程 ``minted → withdrawn``、从未 ``injected``）。
    #
    # 「刚撤掉一枚」本身就是「这条投递通道此刻不通」的直接证据，此刻重铸的那枚多半照样空转。
    # 隔到下一次 Stop 再征询，成本只是延后一个 Stop 周期；候选不会因此丢失——review-highwater
    # 与候选摘要都是按「自上次 review 以来」重算的，下一次 Stop 会把同一批改动原样再算出来。
    #
    # ⚠️ 判据必须收敛到**本轮这条通道**：对账的枚举面是跨 task 全局的，拿「撤回了任意一枚」早退
    # 会让 task A 遗留的一枚残留意图把 repo B 的脏改动整轮压掉（连候选摘要都不算）——那是漏审
    # 方向，而上面那句立论只对同一条通道成立。跨通道的那些只做垃圾回收，不参与本轮决策。
    #
    # 受众是 developer_only（``skip_decision`` ⇒ ``status="skipped"``）：这是一次降级诊断，
    # **不得**驱动 agent 再跑一回合。
    if stranded_reconciliation.withdrew_any_stranded_delivery_on_this_stop_delivery_channel:
        return skip_decision(
            " ".join(
                [
                    "本次 Stop 撤回了搁浅的发起横幅，按约当次就地结束，不在同一次 Stop 内重铸。",
                    *stranded_notices,
                ]
            ),
            ledger,
            REASON_DECISION_POINT_STRANDED_DELIVERY_WITHDRAWN_THIS_STOP,
            repo=repo,
            cwd=cwd,
            withdrawn_stranded_decision_tokens_on_this_stop_delivery_channel=(
                stranded_reconciliation.withdrawn_decision_tokens_on_this_stop_delivery_channel
            ),
            withdrawn_stranded_decision_tokens_garbage_collected_from_other_delivery_channels=(
                stranded_reconciliation
                .withdrawn_decision_tokens_garbage_collected_from_other_delivery_channels
            ),
        )

    try:
        summary = stop_decision_candidate_summary.build_candidate_summary(
            event=event,
            cwd=cwd,
            repo=repo,
            ledger=ledger,
        )
    except Exception as exc:  # pragma: no cover - 摘要构造永不阻断 Stop hook
        ledger.event(
            phase="gate",
            event="decision_point_summary_failed",
            status="warning",
            level="warn",
            reason_code="decision_point_summary_error",
            repo=repo,
            cwd=cwd,
            error={"kind": type(exc).__name__, "message": str(exc)},
        )
        return skip_decision(
            " ".join(
                ["候选摘要构造失败，本次 Stop 静默停（best-effort，不阻断）。", *stranded_notices]
            ),
            ledger,
            REASON_DECISION_POINT_NO_CANDIDATES,
            repo=repo,
            cwd=cwd,
        )

    # 本次事件的 transcript 路径随摘要一起进 token payload：``rvf-agent start`` 靠它把 scope 口径
    # 对齐到 session-owned + 真 lease（见 stamp_origin_transcript_path_on_candidate_summary）。
    stamp_origin_transcript_path_on_candidate_summary(summary, event)

    session_id = summary.get("session_id") or session_hook_id_from_event(event)
    candidate_count = int(summary.get("candidate_path_count") or 0)
    fingerprint = str(summary.get("candidate_fingerprint") or "")
    has_committed = bool(summary.get("has_committed_but_unreviewed"))
    worktree = str(summary.get("repo") or repo)

    # workspace 守卫命中（cwd/git root 与本 kanban task 的执行 worktree 不一致）：这**不是**
    # 「没有候选」，而是「候选在另一个仓、不该由这一轮 RVF 审」。原样返回守卫自己的 skip
    # payload，保住 ``kanban_task_workspace_mismatch`` 这个可诊断的 reason_code —— 降级成通用
    # 的 no_candidates 会让用户看到一句与事实相反的「本 worktree 没有可审改动」。
    if summary.get("workspace_guard_blocked"):
        guard_payload = summary.get("workspace_guard_payload")
        if isinstance(guard_payload, dict) and guard_payload:
            return payload_decision(
                guard_payload,
                reason_code="kanban_task_workspace_mismatch",
                repo=repo,
                cwd=cwd,
            )

    # 无候选 → 静默停，不写 injection ledger（保持 canary 分母干净：只有「候选非空」才是发起层
    # 该表态的分母）。
    if candidate_count <= 0:
        ledger.event(
            phase="gate",
            event="decision_point_no_candidates",
            status="skipped",
            reason_code=REASON_DECISION_POINT_NO_CANDIDATES,
            repo=repo,
            cwd=cwd,
            session_id=session_id,
            workspace_guard_blocked=bool(summary.get("workspace_guard_blocked")),
        )
        return skip_decision(
            " ".join(
                [
                    f"本 worktree 自上次 review 以来无可审候选改动，Stop 静默停。repo={worktree}",
                    *stranded_notices,
                ]
            ),
            ledger,
            REASON_DECISION_POINT_NO_CANDIDATES,
            repo=repo,
            cwd=cwd,
        )

    # 候选非空：先 reconcile 上一枚 token 的投递回执（lost 告警并入 systemMessage），
    # 再对账「用户是否已经把某次征询 rewind 掉了」（INV-C）。
    lost_warnings = list(stranded_notices)
    # rewind 对账**排在 TTL 回执对账之前**：两者都作用于同一批 token，而「用户把它 rewind 掉了」
    # 是比「TTL 到了没人理」更具体、更确凿的事实。反过来排的话，一枚刚好卡在 TTL 边缘又被 rewind
    # 掉的 token 会先被判成 expired（``_decision_token_transcript_trace_present`` 是朴素子串扫描，
    # rewind 之后 needle 仍在文件里），于是 ledger 上留下的是一个不那么准确的标签。
    lost_warnings.extend(
        reconcile_rewound_decision_point_deliveries(
            event, worktree=worktree, session_id=session_id, ledger=ledger
        )
    )
    lost_warnings.extend(
        reconcile_pending_decision_token_receipts(
            event, worktree=worktree, session_id=session_id, ledger=ledger
        )
    )

    # 横幅走哪条 transport（**transport 事实**，不是发起 lifecycle）。带外投递整条路径
    # （单飞闸 → 在途复检 → mint → 意图落盘 → 投递 → 回执处置）收在下面那个函数里，因为它必须
    # 整段裹在 per-task 单飞闸内才是原子的。
    delivery_transport = resolve_decision_point_delivery_transport(
        event, backend=backend, ledger=ledger, cwd=cwd
    )
    if (
        delivery_transport
        == decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
    ):
        return _decision_point_out_of_band_delivery_decision(
            event,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            summary=summary,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            gate_status=gate_status,
            fork_context=fork_context,
            lost_warnings=lost_warnings,
        )

    # mint：去重/declined 冷却/铸造熔断三闸内建（默认落盘根 = $RVF_HOME/~/.rvf，与 rvf-agent CLI
    # 共享，故生产环境 hook 与 CLI 天然同根；测试靠 RVF_HOME 隔离）。
    try:
        mint = decision_token_store.mint_decision_token(
            worktree=worktree,
            session_id=session_id or "",
            candidate_fingerprint=fingerprint,
            candidate_summary=summary,
            delivery_transport=delivery_transport,
        )
    except Exception as exc:  # pragma: no cover - 铸造异常兜底
        return _decision_point_mint_anomaly_decision(
            exc,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            lost_warnings=lost_warnings,
        )

    if mint.outcome == decision_token_store.MINT_OUTCOME_MINTED and mint.token:
        return _decision_point_self_injection_decision(
            token=mint.token,
            summary=summary,
            fork_context=fork_context,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            gate_status=gate_status,
            lost_warnings=lost_warnings,
            event=event,
        )

    return _decision_point_non_minted_outcome_decision(
        mint,
        ledger=ledger,
        repo=repo,
        cwd=cwd,
        worktree=worktree,
        session_id=session_id,
        fingerprint=fingerprint,
        candidate_count=candidate_count,
        has_committed=has_committed,
        lost_warnings=lost_warnings,
    )


def _decision_point_reentry_budget_exhausted_decision(
    budget_decision: Any,
    *,
    token: str,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    lost_warnings: list[str],
) -> StopDecision:
    """再入预算耗尽 ⇒ 本轮不自注入，并把已铸的 token **当场撤回**后静默停。

    撤回不是可选的礼节，是这条路径的正确性本身。横幅没发出去，而 token 若留在活跃非终态上，
    三件事会同时发生且**全都没有信号**：

    * 去重索引仍被这枚 token 占着 ⇒ 后续同指纹 Stop 一律 ``deduplicated`` 走静默路径，
      整个 TTL 窗口内用户等不到横幅；
    * 铸造熔断额度被一次从未发生的征询占掉；
    * ``$rvf-analyze`` 与人工对账看到的是「有一枚征询在等 agent 决策」，而根本没人被问过。

    ``mark_withdrawn`` 恰好一次做完这三件对账（移出去重索引 + 冲销熔断额度 + 落终态），
    且按 INV-C 明确「不计 declined 冷却、不计熔断」——正是本支需要的语义：这次没问成，
    下一次（预算复位后）应当能且只能重新问一次。

    注入尝试 ledger 记 ``injected=False``：candidate 非空的 Stop 都要留一条记录（canary 分母
    不能有洞），但绝不能把「没发」记成发过。

    **已知代价（刻意接受）**：预算持续耗尽期间，每个 Stop 都会走一遍「铸一枚 → 当场撤回」。
    换来的是每一次都有一条开发者可见的诊断，且预算一复位（用户提交新 prompt / digest 变化 /
    TTL 到期）下一个 Stop 就能立刻正常征询。反面（把 token 留在活跃态省掉这次 churn）买到的
    是整个 TTL 窗口的静默——那正是本函数存在的理由。churn 不会失控：撤回同时冲销熔断额度，
    而预算本身始终拒绝驱动 agent，所以不存在续跑循环。
    """
    withdraw_failure: str | None = None
    try:
        decision_token_store.mark_withdrawn(
            token,
            reason=DECISION_POINT_WITHDRAWN_REASON_REENTRY_BUDGET_EXHAUSTED,
            # base 在 0a9a915 给 mark_withdrawn 加了 withdrawal_cause **枚举**（它决定是否冲销
            # 熔断额度），默认值是 rewound_by_user。不显式传就会把「预算耗尽」记成「用户 rewind」：
            # 冲销行为碰巧一致，但成因分类是错的，对账与统计会把两件事混为一谈。
            withdrawal_cause=(
                decision_token_store.DECISION_POINT_WITHDRAWAL_CAUSE_STOP_HOOK_REENTRY_BUDGET_EXHAUSTED
            ),
        )
    except decision_token_store.DecisionTokenError as exc:
        # 撤回失败不得让 Stop 失声，但**必须**响亮：撤不掉就意味着上面那三条后果仍然成立，
        # 开发者需要看到它，否则又变成一次静默搁浅。
        withdraw_failure = f"{type(exc).__name__}: {exc}"
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=INJECTION_ATTEMPT_OUTCOME_REENTRY_BUDGET_EXHAUSTED,
        decision_token=token,
        injected=False,
        note=withdraw_failure,
    )
    ledger.event(
        phase="gate",
        event="decision_point_reentry_budget_exhausted",
        status="warning",
        level="warn",
        reason_code=REASON_DECISION_POINT_REENTRY_BUDGET_EXHAUSTED,
        repo=repo,
        cwd=cwd,
        session_id=session_id,
        decision_token=token,
        candidate_path_count=candidate_count,
        candidate_fingerprint=fingerprint,
        stop_hook_reentry_budget_consecutive_count=(
            budget_decision.consecutive_agent_directive_count
        ),
        stop_hook_reentry_budget_maximum=budget_decision.budget_maximum,
        decision_point_withdrawn_reason=(
            DECISION_POINT_WITHDRAWN_REASON_REENTRY_BUDGET_EXHAUSTED
            if withdraw_failure is None
            else None
        ),
        error=withdraw_failure,
    )
    message = (
        budget_decision.exhaustion_diagnostic
        or "review-validate-fix: Stop hook 再入预算已耗尽，本次不注入发起决策点。"
    )
    message += (
        f" 本轮已铸的决策 token {token} 已撤回"
        "（移出去重索引 + 冲销熔断额度），预算复位后的下一次 Stop 会重新征询一次。"
    )
    if withdraw_failure is not None:
        message += (
            f" ⚠ 撤回失败（{withdraw_failure}）：该 token 仍占着去重索引，"
            "在它 TTL 到期前同指纹的 Stop 会被静默去重。"
        )
    return _decision_point_silent_stop(
        ledger,
        reason_code=REASON_DECISION_POINT_REENTRY_BUDGET_EXHAUSTED,
        message=message,
        repo=repo,
        cwd=cwd,
        lost_warnings=lost_warnings,
    )


def _decision_point_self_injection_decision(
    *,
    token: str,
    summary: dict[str, Any],
    fork_context: dict[str, str] | None,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    gate_status: str,
    lost_warnings: list[str],
    event: dict[str, Any] | None,
    fell_back_from_out_of_band_delivery: dict[str, Any] | None = None,
) -> StopDecision:
    """同步自注入出口：渲染横幅 → 推进 token → 记账 → 以 ``agent_must_act_now`` 返回。

    两个调用方：默认的自注入路径，以及带外投递**确定失败**后的当场回落（INV-B：只在确定失败时
    回落；结论未知一律静默停）。回落时 ``fell_back_from_out_of_band_delivery`` 带着失败诊断，
    进 ledger 与开发者旁注——降级必须响亮。

    **跨 turn 再入预算必须在这里判，不能只留给出口层 ``emit_stop_hook_payload``。** 出口层那道
    闸是所有驱动型出口的通用兜底，但对本函数来说它来得太晚：下面的 ``mark_injected`` /
    INJECTED 记账 / 去重索引占位三件副作用**全都预设了「正文确实会被投出去」**（``mark_injected``
    的 docstring 逐字写着这个前提）。出口层一旦丢掉 ``agent_directive``，前提变假而副作用已经
    落盘 ⇒ token 停在活跃非终态、去重索引被占、canary 分子被一次从未送达的注入撑高，且全程
    零信号。判定必须早于第一件副作用；两层查的是同一次消费（见
    ``_STOP_HOOK_REENTRY_BUDGET_DECISIONS_ALREADY_CONSUMED_IN_THIS_PROCESS``），不会重复计数。
    """
    reentry_budget_decision = _consume_stop_hook_reentry_budget_for_event(
        event, REASON_DECISION_POINT_INJECTED, hand_off_to_the_exit_layer=True
    )
    if reentry_budget_decision is not None and not reentry_budget_decision.agent_directive_permitted:
        return _decision_point_reentry_budget_exhausted_decision(
            reentry_budget_decision,
            token=token,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            lost_warnings=lost_warnings,
        )
    reason_text = _render_decision_point_injection_prompt(
        summary=summary,
        token=token,
        fork_context=fork_context,
        delivery_transport=(
            decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
        ),
    )
    # mark_injected 的语义是「agent 确实拿到了这份正文」。自注入这条路上它成立：正文由 main()
    # 在本函数返回后**同步**写 stdout 投递，故此处推进 minted→injected 不是 87e9338 所诫的
    # 「乐观预 arm」。带外投递那条路推进权在 UPS，见 _decision_point_out_of_band_delivery_decision。
    try:
        decision_token_store.mark_injected(token)
    except decision_token_store.DecisionTokenError:
        pass
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_INJECTED,
        decision_token=token,
        injected=True,
        note=(
            "fell_back_from_out_of_band_delivery"
            if fell_back_from_out_of_band_delivery
            else None
        ),
    )
    ledger.event(
        phase="gate",
        event="decision_point_injected",
        status="started",
        reason_code=REASON_DECISION_POINT_INJECTED,
        repo=repo,
        cwd=cwd,
        session_id=session_id,
        decision_token=token,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        candidate_fingerprint=fingerprint,
        gate_status=gate_status,
        decision_point_delivery_transport=(
            decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
        ),
        decision_point_out_of_band_delivery_fallback=fell_back_from_out_of_band_delivery,
    )
    ledger.summary(
        status="decision-point-injected",
        reason_code=REASON_DECISION_POINT_INJECTED,
        repo=repo,
        cwd=cwd,
        session_id=session_id,
        decision_token=token,
        candidate_path_count=candidate_count,
    )
    user_notice = (
        f"review-validate-fix: 已注入 RVF 发起决策点（token={token}，候选 {candidate_count} 文件），"
        "等待主 agent 显式 start / decline（非自动审查已开始）。"
    )
    if fell_back_from_out_of_band_delivery:
        user_notice += (
            " ⚠ cline-kanban 终端投递**确定失败**"
            f"（{fell_back_from_out_of_band_delivery.get('summary') or 'unknown'}），"
            "本轮已回落为 Stop hook 自注入——这一条不会成为可 rewind 的 user message。"
        )
    if lost_warnings:
        user_notice = user_notice + " " + " ".join(lost_warnings)
    return StopDecision(
        action="emit",
        reason_code=REASON_DECISION_POINT_INJECTED,
        repo=repo,
        cwd=cwd,
        message=reason_text,
        payload=None,
        status="decision-point-injected",
        # 受众 (a)：是否发起 RVF 的决策点——两类够格驱动 agent 的消息之一。
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=AgentDirectiveStopHookOutput(
            agent_directive_body=reason_text,
            developer_visible_notice=user_notice,
        ),
    )


def _decision_point_mint_anomaly_decision(
    exc: BaseException,
    *,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    lost_warnings: list[str],
) -> StopDecision:
    """铸造抛异常的兜底出口（两条 transport 共用）。"""
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_MINT_ANOMALY,
        injected=False,
        note=f"{type(exc).__name__}: {exc}",
    )
    ledger.event(
        phase="gate",
        event="decision_point_mint_anomaly",
        status="warning",
        level="warn",
        reason_code=REASON_DECISION_POINT_MINT_ANOMALY,
        repo=repo,
        cwd=cwd,
        error={"kind": type(exc).__name__, "message": str(exc)},
    )
    return _decision_point_silent_stop(
        ledger,
        reason_code=REASON_DECISION_POINT_MINT_ANOMALY,
        message="决策 token 铸造异常，本次 Stop 静默停。",
        repo=repo,
        cwd=cwd,
        lost_warnings=lost_warnings,
    )


def _decision_point_non_minted_outcome_decision(
    mint: Any,
    *,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    lost_warnings: list[str],
) -> StopDecision:
    """三闸拒铸（deduplicated / declined_cooldown / circuit_breaker）的共用出口。

    两条 transport 走同一份处置：这三种结局都发生在**投递之前**，与横幅怎么送出去无关。
    """
    outcome = mint.outcome

    if outcome == decision_token_store.MINT_OUTCOME_DEDUPLICATED and mint.token:
        # 同键活跃 token 已在等待（R25 注入幂等）→ 不重注入，避免 decline 前的 nag。静默停但保留 park
        # （决策点仍在途）。
        injection_attempt_ledger.record_injection_attempt(
            worktree=worktree,
            session_id=session_id,
            candidate_fingerprint=fingerprint,
            candidate_path_count=candidate_count,
            has_committed_but_unreviewed=has_committed,
            mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_DEDUPLICATED_PENDING,
            decision_token=mint.token,
            injected=False,
        )
        ledger.event(
            phase="gate",
            event="decision_point_deduplicated_pending",
            status="skipped",
            reason_code=REASON_DECISION_POINT_DEDUPLICATED_PENDING,
            repo=repo,
            cwd=cwd,
            session_id=session_id,
            decision_token=mint.token,
        )
        return _decision_point_silent_stop(
            ledger,
            reason_code=REASON_DECISION_POINT_DEDUPLICATED_PENDING,
            message=(
                f"review-validate-fix: 同一候选指纹的决策 token {mint.token} 已在等待主 agent 处理，"
                "本次 Stop 不重复注入（幂等）。"
            ),
            repo=repo,
            cwd=cwd,
            lost_warnings=lost_warnings,
        )

    if outcome == decision_token_store.MINT_OUTCOME_DECLINED_COOLDOWN:
        injection_attempt_ledger.record_injection_attempt(
            worktree=worktree,
            session_id=session_id,
            candidate_fingerprint=fingerprint,
            candidate_path_count=candidate_count,
            has_committed_but_unreviewed=has_committed,
            mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_DECLINED_COOLDOWN,
            injected=False,
        )
        ledger.event(
            phase="gate",
            event="decision_point_declined_cooldown",
            status="skipped",
            reason_code=REASON_DECISION_POINT_DECLINED_COOLDOWN,
            repo=repo,
            cwd=cwd,
            session_id=session_id,
            candidate_fingerprint=fingerprint,
        )
        return _decision_point_silent_stop(
            ledger,
            reason_code=REASON_DECISION_POINT_DECLINED_COOLDOWN,
            message=(
                "review-validate-fix: 候选指纹处于 declined 冷却中（agent 已谢绝且改动未变），"
                "本次 Stop 静默停，不再注入。"
            ),
            repo=repo,
            cwd=cwd,
            lost_warnings=lost_warnings,
        )

    # circuit_breaker_tripped（或其它未识别 outcome）：静默停 + 熔断告警（R24 裁决 b）。
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_CIRCUIT_BREAKER_TRIPPED,
        injected=False,
        note=mint.message,
    )
    ledger.event(
        phase="gate",
        event="decision_point_circuit_breaker_tripped",
        status="warning",
        level="warn",
        reason_code=REASON_DECISION_POINT_CIRCUIT_BREAKER,
        repo=repo,
        cwd=cwd,
        session_id=session_id,
        circuit_breaker_message=mint.message,
    )
    breaker_notice = mint.message or (
        "review-validate-fix: 决策 token 铸造频率熔断，疑似递归发起，本次 Stop 静默停。"
    )
    return _decision_point_silent_stop(
        ledger,
        reason_code=REASON_DECISION_POINT_CIRCUIT_BREAKER,
        message=breaker_notice,
        repo=repo,
        cwd=cwd,
        lost_warnings=lost_warnings,
    )


# ---------------------------------------------------------------------------
# 带外投递：把决策点横幅投成一条**真实 user message**
#
# 本轮存在的理由就在这里。横幅只要成为一条真实 user prompt，它在 Claude Code 的会话历史里就是
# 一条 user 条目，于是双 ESC 的原生 rewind 列表里出现「RVF 发起决策点」这一项，用户可以回到它
# **之前**——Stop hook 的 stdout 自注入永远做不到这件事（它不产生 user 条目）。
#
# 三条必须设计进去的跨仓约束（``cline-kanban`` 终端投递契约，已核验）：
#   1. 回执是**四态**不是三态：``accepted_pending_submit_confirmation`` 是唯一非终态；
#   2. ``DEFAULT_KANBAN_FOLLOWUP_MESSAGE_SHORT_WAIT_MS = 0`` ⇒ 同步调用默认**完全不等**，
#      于是那个非终态是**最常见**的返回，设计必须以它为默认路径；
#   3. 契约的收敛预算 196.5s > Stop hook 的 120s 预算，是**负余量**——结构上不可能在 hook
#      生命周期内等到终态。故「等确认」这条路根本不存在。
#
# INV-B（失败方向）：**只在确定失败时同 Stop 回落自注入**；结论未知一律不回落、本轮静默停，
# 由投递意图在后续 Stop 对账。宁可晚问，不可两条 transport 各问一次（那会违反 INV-1）。
# ---------------------------------------------------------------------------

#: 带外投递的幂等键前缀。绑 decision token 而非 run_id：契约第 7 条下「同 key + 同 prompt hash
#: 只返回当前记录、不重投」，正是我们要的——进程若在 send 之后崩溃，下一次 Stop 从投递意图对账，
#: 绝不因为重跑而投出第二条横幅。真要重新征询时走的是**新 token**，自然就是新 key。
DECISION_POINT_OUT_OF_BAND_IDEMPOTENCY_KEY_PREFIX = "rvf-decision-point"


def decision_point_out_of_band_idempotency_key(token: str) -> str:
    return f"{DECISION_POINT_OUT_OF_BAND_IDEMPOTENCY_KEY_PREFIX}-{token}"


def _decision_point_out_of_band_delivery_decision(
    event: dict[str, Any],
    *,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    summary: dict[str, Any],
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    gate_status: str,
    fork_context: dict[str, str] | None,
    lost_warnings: list[str],
) -> StopDecision:
    """D2 的全部：**闸在前、在途复检在中、mint 在后、意图先于副作用落盘、按四态处置**。

    次序是本函数的全部内容：

    1. 抢 per-task 单飞闸——抢不到即静默停，**不 mint、不计熔断**（闸挡住不是「重复铸币」，
       不该烧熔断额度）。用的是与 follow-up 派发**同一把**闸：约束来自目标 task 只有一个
       终端投递槽，与「谁在投」无关。
    2. 闸内复检本 session 有无「已投出去、agent 还没接到」的横幅——有则静默停，同样不 mint、
       不计熔断。迟到窗口内的重复铸币在此消解。
    3. mint（三闸内建）。
    4. **写投递意图**——外部副作用之前先持久化，这样进程若在 send 之后、回执之前崩溃，
       下一次 Stop 能从意图对账，不必靠「本进程有没有返回」猜。
    5. 投递，按回执四态处置（见下表）。

    | 回执 | token | 意图 | stdout |
    | --- | --- | --- | --- |
    | ``delivered_and_submit_confirmed`` | 停在 ``minted`` | 标 sent | 静默停 + 开发者旁注 |
    | ``delivered_queued_behind_active_agent_turn`` | 停在 ``minted`` | 标 sent | 同上 |
    | ``accepted_pending_submit_confirmation``（**默认**） | 停在 ``minted`` | 标 sent | 同上 |
    | ``delivery_failed`` / 子进程没起来 | 走自注入推进 | 清除 | **当场回落自注入** |
    | 回执不可解析 / 缺 message_id / 非零退出 / 其它异常 | 停在 ``minted`` | **保留** | 静默停 + 开发者旁注 |

    三个 delivered/accepted 分支**都不推进 token**：推进权交给 UPS（观测到
    ``RVF_DECISION_TOKEN=<token>`` 落进 prompt 的那一刻 = INV-A 达成）。

    **最后一行是 INV-B 的另一半，也是本函数最容易搞错的地方**：「拿不到可解析回执」≠「投递失败」。
    真实 client 先跑完 ``kanban task message`` 子进程（横幅此时可能已经投出去了），再解析 JSON、
    再校验 ``message_id``——后两步任一失败都会抛异常。把这类异常当确定失败去回落，就会出现
    「一条带外 user message + 一条 stdout 正文」同时存在，同时违反 INV-1 与 INV-B。分流的依据是
    ``start_cline_kanban_followup_message`` 抛的异常族（见
    :class:`KanbanFollowupDeliveryNotAttemptedError` /
    :class:`KanbanFollowupDeliveryOutcomeUnknownError`），**不是** ``message_payload is None``。
    """
    task_id = current_kanban_task_id(event)
    if not task_id:
        # transport 解析已经保证过它非空；真走到这里说明 event 在两次读之间变了形状。
        # 不静默改道，如实回落自注入并留痕。
        ledger.event(
            phase="gate",
            event="decision_point_out_of_band_transport_lost_task_id",
            status="warning",
            level="warn",
            reason_code="decision_point_out_of_band_transport_lost_task_id",
            repo=repo,
            cwd=cwd,
        )
        return _decision_point_fall_back_to_self_injection(
            summary=summary,
            fork_context=fork_context,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            gate_status=gate_status,
            lost_warnings=lost_warnings,
            fallback_diagnostics={"summary": "kanban task id 在 transport 解析后消失"},
            event=event,
        )

    project_path = current_kanban_project_path(event, worktree)
    attempt_id = current_kanban_attempt_id(event)

    with kanban_followup_dispatch_single_flight_for_task(task_id) as slot_acquired:
        if not slot_acquired:
            # 闸挡住 ≠ 重复铸币。不 mint、不写注入尝试 ledger（分母只记「本轮真的表过态」），
            # 静默停并保留 park：决策点仍在途。
            ledger.event(
                phase="gate",
                event="decision_point_out_of_band_delivery_single_flight_busy",
                status="skipped",
                reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_SINGLE_FLIGHT_BUSY,
                repo=repo,
                cwd=cwd,
                session_id=session_id,
                cline_kanban_task_id=task_id,
            )
            return _decision_point_silent_stop(
                ledger,
                reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_SINGLE_FLIGHT_BUSY,
                message=(
                    "review-validate-fix: 另一个进程正在对同一个 cline-kanban task 投递，"
                    "本次 Stop 不并发投递发起横幅（并发会让先到的那条被判 superseded）。"
                ),
                repo=repo,
                cwd=cwd,
                lost_warnings=lost_warnings,
            )

        in_flight = _decision_point_out_of_band_delivery_in_flight(
            task_id=task_id, worktree=worktree, session_id=session_id
        )
        if in_flight is not None:
            ledger.event(
                phase="gate",
                event="decision_point_out_of_band_delivery_in_flight",
                status="skipped",
                reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_IN_FLIGHT,
                repo=repo,
                cwd=cwd,
                session_id=session_id,
                cline_kanban_task_id=task_id,
                decision_token=in_flight.get("decision_token"),
                decision_point_delivery_intent_path=in_flight.get("_marker_path"),
            )
            return _decision_point_silent_stop(
                ledger,
                reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_IN_FLIGHT,
                message=(
                    "review-validate-fix: 已有一份发起横幅投出去、尚未被 agent 接到"
                    f"（token={in_flight.get('decision_token')}），本次 Stop 不重复投递。"
                ),
                repo=repo,
                cwd=cwd,
                lost_warnings=lost_warnings,
            )

        try:
            mint = decision_token_store.mint_decision_token(
                worktree=worktree,
                session_id=session_id or "",
                candidate_fingerprint=fingerprint,
                candidate_summary=summary,
                delivery_transport=(
                    decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
                ),
            )
        except Exception as exc:  # pragma: no cover - 铸造异常兜底
            return _decision_point_mint_anomaly_decision(
                exc,
                ledger=ledger,
                repo=repo,
                cwd=cwd,
                worktree=worktree,
                session_id=session_id,
                fingerprint=fingerprint,
                candidate_count=candidate_count,
                has_committed=has_committed,
                lost_warnings=lost_warnings,
            )

        if mint.outcome != decision_token_store.MINT_OUTCOME_MINTED or not mint.token:
            return _decision_point_non_minted_outcome_decision(
                mint,
                ledger=ledger,
                repo=repo,
                cwd=cwd,
                worktree=worktree,
                session_id=session_id,
                fingerprint=fingerprint,
                candidate_count=candidate_count,
                has_committed=has_committed,
                lost_warnings=lost_warnings,
            )

        token = mint.token
        banner = _render_decision_point_injection_prompt(
            summary=summary,
            token=token,
            fork_context=fork_context,
            delivery_transport=(
                decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
            ),
        )
        idempotency_key = decision_point_out_of_band_idempotency_key(token)

        # ④ 先落意图再产生外部副作用。写不成也继续投——少一条对账线索，好过因为一次
        # 文件系统抖动就把发起路径整个堵死；这一步失败会如实进 ledger。
        intent_path: Any = None
        try:
            intent_path = write_decision_point_delivery_intent(
                task_id=task_id,
                session_id=session_id,
                worktree=worktree,
                cwd=cwd,
                decision_token=token,
                candidate_fingerprint=fingerprint,
                kanban_project_path=project_path,
                kanban_attempt_id=attempt_id,
                origin_transcript_path=summary.get(
                    CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY
                ),
                state=DECISION_POINT_DELIVERY_INTENT_STATE_RECORDED_BEFORE_SEND,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - 意图落盘失败不阻断投递
            ledger.event(
                phase="gate",
                event="decision_point_delivery_intent_write_failed",
                status="warning",
                level="warn",
                reason_code="decision_point_delivery_intent_write_failed",
                repo=repo,
                cwd=cwd,
                decision_token=token,
                error={"kind": type(exc).__name__, "message": str(exc)},
            )

        delivery_error: str | None = None
        delivery_outcome_unknown = False
        verdict_not_yet_available = False
        message_payload: dict[str, Any] | None = None
        try:
            message_payload = start_cline_kanban_followup_message(
                project_path=project_path,
                task_id=task_id,
                attempt_id=attempt_id,
                prompt=banner,
                ledger=ledger,
                idempotency_key=idempotency_key,
                artifact_label="decision-point-banner",
            )
        except KanbanFollowupDeliveryVerdictNotYetAvailableError as exc:
            # 「还没结论」**不是**失败：这条消息可能已被受理并随后落地。按未确认投递继续。
            message_payload = exc.provisional_payload()
            verdict_not_yet_available = True
        except KanbanFollowupDeliveryNotAttemptedError as exc:
            # **唯一**的「确定失败」异常族：子进程根本没起来 ⇒ 外部副作用确定没发生 ⇒ 可以回落。
            delivery_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - 其余一律按「结论未知」兜底
            # 子进程已经跑过了：坏 JSON、缺 message_id、非零退出、TimeoutExpired……横幅**可能**
            # 已经投到 agent 面前。此时回落自注入会同时违反 INV-1（一次 Stop 至多一条发起正文）
            # 与 INV-B（结论未知不回落）。失败方向必须是「晚问」而不是「双投」。
            delivery_error = f"{type(exc).__name__}: {exc}"
            delivery_outcome_unknown = True

        if delivery_outcome_unknown:
            return _decision_point_out_of_band_delivery_outcome_unknown_decision(
                ledger=ledger,
                repo=repo,
                cwd=cwd,
                session_id=session_id,
                worktree=worktree,
                fingerprint=fingerprint,
                candidate_count=candidate_count,
                has_committed=has_committed,
                token=token,
                task_id=task_id,
                project_path=project_path,
                delivery_error=delivery_error or "unknown delivery outcome",
                intent_path=intent_path,
                lost_warnings=lost_warnings,
            )

        receipt = (
            classify_cline_kanban_terminal_delivery_receipt(message_payload)
            if message_payload is not None
            else None
        )
        # 只剩两种可能：``message_payload is None`` 此刻**只**由确定失败那一族产生（结论未知已在
        # 上面提前返回），或对方明确回了 ``delivery_failed``。
        definitely_failed = message_payload is None or (
            receipt is not None and receipt.is_failed
        )

        if definitely_failed:
            # INV-B 的「确定失败」那一半：当场回落自注入。先把意图清掉——此刻并没有任何东西在途，
            # 留一份 active 意图只会把接下来 15 分钟的重新决策白白挡住。
            failure_summary = delivery_error or (
                describe_terminal_delivery_receipt_for_operator(receipt)
                if receipt is not None
                else "unknown delivery failure"
            )
            try:
                clear_decision_point_delivery_intent(task_id=task_id, decision_token=token)
            except Exception:  # noqa: BLE001 - 清理 best-effort
                pass
            try:
                decision_token_store.record_out_of_band_delivery_status(
                    token,
                    status=(
                        receipt.terminal_delivery_status
                        if receipt is not None
                        else None
                    ),
                    detail={
                        "delivery_error": delivery_error,
                        "terminal_delivery_failure_reason": (
                            receipt.terminal_delivery_failure_reason if receipt else None
                        ),
                        "fell_back_to_stop_hook_self_injection": True,
                    },
                )
            except decision_token_store.DecisionTokenError:
                pass
            ledger.event(
                phase="gate",
                event="decision_point_out_of_band_delivery_failed",
                status="warning",
                level="warn",
                reason_code="decision_point_out_of_band_delivery_failed",
                repo=repo,
                cwd=cwd,
                session_id=session_id,
                decision_token=token,
                cline_kanban_task_id=task_id,
                cline_kanban_project_path=project_path,
                kanban_terminal_delivery_status=(
                    receipt.terminal_delivery_status if receipt else None
                ),
                kanban_terminal_delivery_failure_reason=(
                    receipt.terminal_delivery_failure_reason if receipt else None
                ),
                kanban_terminal_delivery_redispatch_disposition=(
                    receipt.redispatch_disposition if receipt else None
                ),
                error=delivery_error,
            )
            return _decision_point_self_injection_decision(
                token=token,
                summary=summary,
                fork_context=fork_context,
                ledger=ledger,
                repo=repo,
                cwd=cwd,
                worktree=worktree,
                session_id=session_id,
                fingerprint=fingerprint,
                candidate_count=candidate_count,
                has_committed=has_committed,
                gate_status=gate_status,
                lost_warnings=lost_warnings,
                event=event,
                fell_back_from_out_of_band_delivery={
                    "summary": failure_summary,
                    "terminal_delivery_status": (
                        receipt.terminal_delivery_status if receipt else None
                    ),
                    "terminal_delivery_failure_reason": (
                        receipt.terminal_delivery_failure_reason if receipt else None
                    ),
                },
            )

        # 三个 delivered / accepted 分支：**token 停在 minted**，推进权交给 UPS。
        assert receipt is not None
        try:
            update_decision_point_delivery_intent(
                task_id=task_id,
                decision_token=token,
                updates={
                    "state": DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
                    "out_of_band_delivery_status": receipt.terminal_delivery_status,
                    "terminal_delivery_verdict_not_yet_available": verdict_not_yet_available,
                    "message_id": receipt.message_id,
                    "idempotency_key": (
                        message_payload.get("idempotency_key") or idempotency_key
                    ),
                    "prompt_path": message_payload.get("prompt_path"),
                },
            )
        except Exception:  # noqa: BLE001 - 意图更新 best-effort
            pass
        try:
            decision_token_store.record_out_of_band_delivery_status(
                token,
                status=receipt.terminal_delivery_status,
                detail={
                    "kanban_task_id": task_id,
                    "idempotency_key": idempotency_key,
                    "terminal_delivery_verdict_not_yet_available": verdict_not_yet_available,
                    "receipt_field_source": receipt.receipt_field_source,
                },
            )
        except decision_token_store.DecisionTokenError:
            pass

        # canary 记账：``injected=False`` 是**如实**的——这一刻 agent 什么也没拿到。token 由 UPS
        # 观测到 needle 时才推进 injected，两条记录经 ``decision_token`` 关联即可对账，
        # 刻意不在 UPS 侧再补一条 ledger（那会把「候选非空的 Stop 次数」这个分母搞脏）。
        injection_attempt_ledger.record_injection_attempt(
            worktree=worktree,
            session_id=session_id,
            candidate_fingerprint=fingerprint,
            candidate_path_count=candidate_count,
            has_committed_but_unreviewed=has_committed,
            mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_DELIVERED_OUT_OF_BAND,
            decision_token=token,
            injected=False,
            note=receipt.terminal_delivery_status,
        )
        # D8 的 INV-1 gate 从**这条事件**读「本轮带外投递了几条」——带外正文根本不进 stdout
        # payload，只看 payload 的断言对新路径恒为真（假绿）。
        ledger.event(
            phase="gate",
            event="decision_point_delivered_out_of_band",
            status="started",
            reason_code=REASON_DECISION_POINT_DELIVERED_OUT_OF_BAND,
            repo=repo,
            cwd=cwd,
            session_id=session_id,
            decision_token=token,
            candidate_path_count=candidate_count,
            has_committed_but_unreviewed=has_committed,
            candidate_fingerprint=fingerprint,
            gate_status=gate_status,
            decision_point_delivery_transport=(
                decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
            ),
            decision_point_delivery_intent_path=(
                str(intent_path) if intent_path else None
            ),
            cline_kanban_task_id=task_id,
            cline_kanban_project_path=project_path,
            kanban_terminal_delivery_status=receipt.terminal_delivery_status,
            kanban_terminal_delivery_receipt_field_source=receipt.receipt_field_source,
            kanban_terminal_delivery_verdict_not_yet_available=verdict_not_yet_available,
            kanban_terminal_delivery_summary=(
                describe_terminal_delivery_receipt_for_operator(receipt)
            ),
            decision_point_out_of_band_delivery_body_sha256=hashlib.sha256(
                banner.encode("utf-8")
            ).hexdigest(),
        )
        notice = (
            f"发起决策点横幅已交给 cline-kanban 终端投递（token={token}，候选 {candidate_count} "
            f"文件，回执 {receipt.terminal_delivery_status or 'unknown'}）。"
            "它会以一条**真实 user message** 到达并开启新回合——因此可被 rewind。"
            "本次 Stop 不向当前会话注入任何东西。"
        )
        if lost_warnings:
            notice = notice + " " + " ".join(lost_warnings)
        # D3：出口走 ``developer_only`` + ``systemMessage``，**不加第三个 audience**。
        # 契约在带外路径上不是权威——权威是投递意图 + token 状态 + UPS 回执。硬塞进契约只会
        # 制造「契约说驱动了、实际可能没送达」的谎言。
        #
        # 用 ``hook_payload``（而非手搓 dict）是为了与其余出口同形：它顺带写 summary、把 run
        # 指针带进开发者可见的那几行——不然这条「确实做了事」的 Stop 在终端里反而最难追。
        payload = ledger.hook_payload(
            status="decision-point-delivered-out-of-band",
            reason_code=REASON_DECISION_POINT_DELIVERED_OUT_OF_BAND,
            message=notice,
            detail=notice,
            repo=repo,
            cwd=cwd,
            session_id=session_id,
            decision_token=token,
            candidate_path_count=candidate_count,
        )
        return StopDecision(
            action="skip",
            reason_code=REASON_DECISION_POINT_DELIVERED_OUT_OF_BAND,
            repo=repo,
            cwd=cwd,
            message=notice,
            payload=payload,
            status="decision-point-delivered-out-of-band",
            audience=STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
        )


def _decision_point_out_of_band_delivery_outcome_unknown_decision(
    *,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    session_id: str | None,
    worktree: str,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    token: str,
    task_id: str,
    project_path: str,
    delivery_error: str,
    intent_path: Any,
    lost_warnings: list[str],
) -> StopDecision:
    """投递结论未知：**不回落、不清意图、不推进 token**，本轮静默停，交给后续 Stop 对账。

    这一支存在的全部理由是「不知道」与「失败了」不是同一件事：子进程已经跑完，横幅**可能**
    已经作为一条真实 user message 到了 agent 面前，我们只是读不懂回执。此刻做任何一件
    「宣布它没送到」的事都会造成可观测的谎报：

    * 当场自注入 ⇒ agent 眼前出现两份征询（违反 INV-1）、且 INV-B 的原文正是为了防这件事；
    * 清掉投递意图 ⇒ 下一次 Stop 的在途复检失明，会再投一条，先到的那条被判 superseded；
    * 推进 token ⇒ 谎报 agent 已经拿到了正文（``injected`` 的语义就是「确实拿到了」）。

    保留意图之后，后续对账**自动**接手，两条路都已存在、无需新机制：

    * 横幅真的落地了 ⇒ 目标会话的 UPS 观测到 needle，推进 ``injected`` 并清意图；
    * 始终没成为 turn ⇒ 意图 TTL（900s）到期后
      ``reconcile_stranded_decision_point_delivery_intents`` 物理取消 + 把 token 推进
      ``withdrawn``，下一次 Stop 重新征询一次。

    注入尝试 ledger 记的是 ``out_of_band_delivery_outcome_unknown`` 且 ``injected=False``——
    「候选非空的 Stop 都要留一条记录」这个 canary 分母不能有洞，但也绝不能把「不知道」记成
    ``delivered_out_of_band``。
    """
    # 意图**保留**，只补诊断字段（state 刻意仍是 recorded_before_send：我们并不知道它送出去了）。
    try:
        update_decision_point_delivery_intent(
            task_id=task_id,
            decision_token=token,
            updates={
                "out_of_band_delivery_outcome_unknown": True,
                "out_of_band_delivery_outcome_unknown_error": delivery_error,
            },
        )
    except Exception:  # noqa: BLE001 - 意图更新 best-effort
        pass
    try:
        decision_token_store.record_out_of_band_delivery_status(
            token,
            status=None,
            detail={
                "kanban_task_id": task_id,
                "out_of_band_delivery_outcome_unknown": True,
                "out_of_band_delivery_outcome_unknown_error": delivery_error,
                "fell_back_to_stop_hook_self_injection": False,
            },
        )
    except decision_token_store.DecisionTokenError:
        pass
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=INJECTION_ATTEMPT_OUTCOME_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN,
        decision_token=token,
        injected=False,
        note=delivery_error,
    )
    ledger.event(
        phase="gate",
        event="decision_point_out_of_band_delivery_outcome_unknown",
        status="warning",
        level="warn",
        reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN,
        repo=repo,
        cwd=cwd,
        session_id=session_id,
        decision_token=token,
        cline_kanban_task_id=task_id,
        cline_kanban_project_path=project_path,
        decision_point_delivery_intent_path=(str(intent_path) if intent_path else None),
        decision_point_delivery_transport=(
            decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
        ),
        error=delivery_error,
    )
    return _decision_point_silent_stop(
        ledger,
        reason_code=REASON_DECISION_POINT_OUT_OF_BAND_DELIVERY_OUTCOME_UNKNOWN,
        message=(
            f"review-validate-fix: 发起横幅已交给 cline-kanban 终端投递（token={token}），"
            f"但拿不到可解析回执（{delivery_error}）。**投递结论未知**——横幅可能已经作为一条真实 "
            "user message 到达。本次 Stop 不注入任何东西、也不清除投递意图；由后续 Stop 的意图"
            "对账处置（落地则 UPS 推进，始终没成为 turn 则到期撤回并重新征询）。"
        ),
        repo=repo,
        cwd=cwd,
        lost_warnings=lost_warnings,
    )


def _decision_point_fall_back_to_self_injection(
    *,
    summary: dict[str, Any],
    fork_context: dict[str, str] | None,
    ledger: RunLedger,
    repo: str,
    cwd: str | None,
    worktree: str,
    session_id: str | None,
    fingerprint: str,
    candidate_count: int,
    has_committed: bool,
    gate_status: str,
    lost_warnings: list[str],
    fallback_diagnostics: dict[str, Any],
    event: dict[str, Any] | None,
) -> StopDecision:
    """带外 transport 在**铸币之前**就不可用时的回落：照常 mint（自注入 TTL）再自注入。"""
    try:
        mint = decision_token_store.mint_decision_token(
            worktree=worktree,
            session_id=session_id or "",
            candidate_fingerprint=fingerprint,
            candidate_summary=summary,
            delivery_transport=(
                decision_token_store.DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION
            ),
        )
    except Exception as exc:  # pragma: no cover - 铸造异常兜底
        return _decision_point_mint_anomaly_decision(
            exc,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            lost_warnings=lost_warnings,
        )
    if mint.outcome != decision_token_store.MINT_OUTCOME_MINTED or not mint.token:
        return _decision_point_non_minted_outcome_decision(
            mint,
            ledger=ledger,
            repo=repo,
            cwd=cwd,
            worktree=worktree,
            session_id=session_id,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            has_committed=has_committed,
            lost_warnings=lost_warnings,
        )
    return _decision_point_self_injection_decision(
        token=mint.token,
        summary=summary,
        fork_context=fork_context,
        ledger=ledger,
        repo=repo,
        cwd=cwd,
        worktree=worktree,
        session_id=session_id,
        fingerprint=fingerprint,
        candidate_count=candidate_count,
        has_committed=has_committed,
        gate_status=gate_status,
        lost_warnings=lost_warnings,
        event=event,
        fell_back_from_out_of_band_delivery=fallback_diagnostics,
    )


def _decision_point_out_of_band_delivery_in_flight(
    *,
    task_id: str,
    worktree: str,
    session_id: str | None,
) -> dict[str, Any] | None:
    """本 task/session 是否已有一份「投出去了、agent 还没接到」的横幅。

    两个信号取并集，缺一不可：

    * **投递意图**（per-task）——它在 send **之前**就落盘了，故它覆盖崩溃窗口：进程若在 send
      之后、回执之前被杀，意图仍在。
    * **token store 在途查询**（per-session）——它覆盖「意图写盘失败但消息确实送出去了」。

    只有 stale（TTL 过期）的意图才放行重问：那是「晚问」，不是「漏问」。
    """
    marker = read_decision_point_delivery_intent(task_id=task_id)
    if (
        isinstance(marker, dict)
        and decision_point_delivery_intent_status(marker) == KANBAN_FOLLOWUP_LOCK_ACTIVE
    ):
        token = marker.get("decision_token")
        if isinstance(token, str) and token:
            lookup = decision_token_store.read_decision_token(token)
            # 意图还活着、但 token 已经被 agent 接走 / 决策掉 / 撤回掉 ⇒ 意图是陈旧的，
            # 不该再挡住重新征询（它的清理由 UPS 或对账负责，best-effort 可能漏）。
            if lookup.status == "valid" and lookup.state == decision_token_store.STATE_MINTED:
                return marker
    lookup = decision_token_store.find_decision_token_awaiting_out_of_band_delivery(
        worktree=worktree, session_id=session_id
    )
    if lookup is not None:
        return {"decision_token": lookup.token, "_marker_path": str(lookup.path)}
    return None


def _decision_point_silent_stop(
    ledger: RunLedger,
    *,
    reason_code: str,
    message: str,
    repo: str | None,
    cwd: str | None,
    lost_warnings: list[str],
) -> StopDecision:
    """决策点静默停（continue:true）的收口：把 message + lost 告警合成 systemMessage。

    park 处置由 reason_code 经 _skip_decision_should_unpark 决定（dedup-pending 保留 park，
    其余 unpark）。"""
    full_message = message
    if lost_warnings:
        full_message = full_message + " " + " ".join(lost_warnings)
    return skip_decision(
        full_message,
        ledger,
        reason_code,
        repo=repo,
        cwd=cwd,
    )


def evaluate_stop_event(event: dict[str, Any], ledger: RunLedger) -> StopDecision:
    latest_user = latest_user_message_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None

    if event.get("stop_hook_active") is True:
        # host 名由注入面提供：写死 "Codex" 会在 Claude 会话里印出错误的 host（本消息是给
        # 开发者看的诊断，报错主体必须是当前这个 host）。
        host_label = resolve_current_session_host_adapter(event).host_display_name()
        return skip_decision(
            "检测到 stop_hook_active=true，为避免递归已跳过。",
            ledger,
            "stop_hook_active",
            cwd=cwd,
            detail=f"{host_label} 已在执行 Stop hook，RVF 跳过以避免递归",
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

    awaiting_decision = awaiting_dispatched_agent_decision(event, ledger, cwd=cwd)
    if awaiting_decision is not None:
        return awaiting_decision

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

    # R24 裁决 b（电路熔断替代硬闸）：``already_rvf_fork`` 硬闸**退役**——递归防护改由 S1 mint 侧的
    # 铸造频率熔断（同 session/小时 >K 拒铸）+ agent 自觉承担。fork-context 不再硬跳过，降级为候选
    # 摘要注入提示里的一行标注（若命中，提示 agent「若在审自己 RVF 的结果通常应 decline」），仍计算
    # 供 decision_point_injection_decision 使用。
    fork_context = rvf_fork_context(latest_user) or rvf_fork_context_from_event(event)

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
        # P1 手术边界：按**发起生命周期**分流，不再按 backend 集合（见
        # classify_review_initiation_lifecycle 上方的模块注释）。
        #   * fresh_initiation（默认，含 kanban task 内的自动 RVF）：轻量候选摘要 → mint →
        #     注入**发起决策点横幅**给主 agent，由它显式 start / decline（无论 DIRTY / CLEAN，
        #     候选摘要都统一枚举 dirty ∪ committed-but-unreviewed）。
        #   * accepted_run_in_flight：静默停（见下方分支的理由），不派发也不重新征询。
        #   * 模式关闭 / 显式延续 transport：保留原 route_reviewable_scope→launch 语义，
        #     两函数本体亦保留。
        backend_for_initiation = normalize_backend_from_env(event)
        initiation_lifecycle = classify_review_initiation_lifecycle(
            event,
            ledger,
            backend=backend_for_initiation,
            backend_selection_mode=fork_mode_selection_from_env(),
            cwd=cwd,
        )
        if initiation_lifecycle == REVIEW_INITIATION_LIFECYCLE_ACCEPTED_RUN_IN_FLIGHT:
            # 计划原文说这一支「保留旧派发路径」。**刻意不照做**：这一支的 backend 通常是
            # ``kanban`` / ``gui``，而 route_reviewable_scope 在那里的语义是「新建一个 RVF
            # task / fork」——在一轮已被接受、仍在途的 run 之上再开一轮，正是本次 overhaul 要
            # 根治的重复派发。静默停是唯一安全的处置；可观测性由分类器刚写的那条 warn 级
            # ledger 承担（``review_initiation_accepted_run_still_in_flight_at_dirty_gate``），
            # 不是无声吞掉。该状态按构造不可达（更早的 in-progress / awaiting 判定会先接走）。
            return skip_decision(
                "已有一轮被 agent 接受且仍在途的 RVF run；本次 Stop 不重新征询、也不另起一轮。",
                ledger,
                "review_initiation_accepted_run_in_flight",
                repo=cwd_result.repo or cwd,
                cwd=cwd,
                backend=backend_for_initiation,
            )
        routes_to_legacy_dispatch = (
            initiation_lifecycle != REVIEW_INITIATION_LIFECYCLE_FRESH_INITIATION
        )
        if cwd_result.status == "DIRTY" and cwd_result.repo:
            if routes_to_legacy_dispatch:
                return route_reviewable_scope(
                    event,
                    cwd_result.repo,
                    changed_paths_from_gate_output(cwd_result.output),
                    ledger,
                    cwd,
                    source="dirty_working_tree",
                    gate_status=cwd_result.status,
                )
            return decision_point_injection_decision(
                event,
                cwd_result.repo,
                ledger,
                cwd,
                gate_status=cwd_result.status,
                fork_context=fork_context,
                backend=backend_for_initiation,
            )
        if cwd_result.status == "CLEAN":
            if routes_to_legacy_dispatch:
                # 非 fresh 发起：工作区 clean 时仍查「本轮已提交但未审」改动（commit 9127824 的
                # 下游 committed 检测），有则照常按旧语义路由，没有才 clean skip。
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
            # fresh-initiation + CLEAN：候选摘要含 committed-but-unreviewed，故 clean 工作区也可能
            # 有已提交待审改动 → 走注入决策点；真正无候选时摘要为空 → 静默停。
            return decision_point_injection_decision(
                event,
                cwd_result.repo or cwd,
                ledger,
                cwd,
                gate_status=cwd_result.status,
                fork_context=fork_context,
                backend=backend_for_initiation,
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


def _record_guardian_stop_event_heartbeat() -> None:
    """S8·R23 wall-clock 心跳:每个到达本 hook 的 Stop 事件计一次(best-effort)。

    守护体系据此获得「Stop 事件数非零」的分母——发起层若静默死亡,analyze 不会
    着火,但 Stop 心跳仍在,由 :func:`_apply_guardian_liveness_alarm` 出告警。
    绝不阻断 Stop hook 主流程。
    """
    try:
        import agentic_initiation_guardian_dispatch as guardian_dispatch  # noqa: PLC0415

        guardian_dispatch.record_stop_event_heartbeat()
    except Exception:  # noqa: BLE001 - 心跳记录永不阻断
        pass


def _apply_guardian_liveness_alarm(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """把「发起层疑似静默死亡」告警(若有)并入即将 emit 的 payload(best-effort)。

    - 无告警:原样返回(含 ``None`` = 保持静默停)。
    - 有告警且 payload 为 ``None``:升格为 ``{"continue": True, "systemMessage": …}``——
      告警的意义正是打破病态静默,值得为此放弃静默停。
    - 有告警且 payload 存在:追加到既有 ``systemMessage`` 之后。
    """
    try:
        import agentic_initiation_guardian_dispatch as guardian_dispatch  # noqa: PLC0415

        alarm = guardian_dispatch.evaluate_initiation_layer_liveness()
    except Exception:  # noqa: BLE001 - 判定失败按无告警处理
        return payload
    if not alarm:
        return payload
    alarm_text = "=== RVF GUARDIAN ===\n" + str(alarm.get("message") or alarm.get("alarm"))
    if payload is None:
        return {"continue": True, "systemMessage": alarm_text}
    if isinstance(payload, dict):
        existing = payload.get("systemMessage")
        payload["systemMessage"] = (f"{existing}\n{alarm_text}" if existing else alarm_text)
    return payload


CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL = "/.codex/sessions/"
"""Codex rollout **默认**根 ``~/.codex/sessions/`` 的路径字面量。

单独命名是为了钉住它**不是**判定 Codex 的充分条件：``CODEX_HOME`` 一改，真实 rollout
路径里根本不含这个字面量（见 ``codex_rollout_session_path_markers``）。
"""


def codex_rollout_session_path_markers() -> tuple[str, ...]:
    """能判定「这是一条 Codex rollout 文件路径」的路径证据集合（正向证据，保守）。

    除默认根 ``~/.codex/sessions/`` 外，还认 ``$CODEX_HOME/sessions/``——Codex CLI 自己
    就用 ``CODEX_HOME`` override 安装根（本仓 ``adapters.codex.subagent.codex_sessions_root``
    正是这么解析的），自定义过它的用户，其 rollout 路径里根本不含默认字面量。

    ``CODEX_HOME`` 在这里**只**用来回答「sessions 目录在哪」，绝不当成「当前会话是 Codex」
    的身份信号——后者是 ``core/host_adapter/parent_harness_identity`` 明令禁止的误用（它是
    常驻安装路径 override，本机装过 Codex 的用户在任何 host 下都设着它）。
    """
    markers = [CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL]
    codex_home = os.environ.get("CODEX_HOME")
    if isinstance(codex_home, str) and codex_home.strip():
        try:
            root = os.path.normpath(str(Path(codex_home).expanduser() / "sessions"))
        except (OSError, ValueError, RuntimeError):  # 解析失败按「无此证据」处理
            root = ""
        if root:
            markers.append(root.rstrip(os.sep) + os.sep)
    return tuple(markers)


def event_session_path_is_codex_rollout(event: dict[str, Any]) -> bool:
    """event 的任一会话路径键落在 Codex rollout 根下 ⇒ 确诊 Codex（正向证据）。

    覆盖**全部** ``SESSION_PATH_KEYS``（含 ``log_path``），而不是其中一个子集：漏掉的键
    正是「真 Codex 会话溜过守卫」的入口。

    与 ``_claude_hook_entry.is_foreign_invocation`` **同判据**；那个模块刻意 stdlib-only、
    不 import 本仓 ``core``（hook 是最该 fail-open 的安全面），故那边自带一份等价副本——
    两处改动必须同步。
    """
    markers = codex_rollout_session_path_markers()
    for path in event_session_paths(event):
        raw = str(path)
        try:
            normalized = os.path.normpath(str(Path(raw).expanduser()))
        except (OSError, ValueError, RuntimeError):  # 规范化失败退回原串
            normalized = raw
        for marker in markers:
            if marker == CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL:
                if marker in normalized or marker in raw:
                    return True
            elif normalized.startswith(marker):
                return True
    return False


HOST_BY_PLUGIN_ENTRY_SOURCE_PROVIDER_STAMP: dict[str, str] = {
    "claude-code": HOST_CLAUDE,
    "kimi-code": HOST_KIMI_CODE,
}
"""``event["source"]["provider"]`` → host 身份。键是各 host 的 plugin 入口 ``setdefault``
上去的那个字面量（``hooks/_claude_hook_entry.py`` / ``hooks/_kimi_hook_entry.py``）。

**每多一个自带 hook 入口的 host，这里就必须多一条**：漏一条时该 host 的 stamp 会被无视，
在 transcript 探测落空的场合直接掉进「未知 → Codex」兜底。Codex 兜底对 Codex 自己是安全的
（发错键会**响亮**地整条 Failed），对 Kimi 则是**静默 allow**，RVF 完全失声、零报错。

Codex 不在表里，也不需要在：它没有自己的 stamp，而它的两条正向证据（会话路径、rollout
schema）排在本表之前。
"""


def resolve_current_session_host_adapter(event: dict[str, Any]) -> Any:
    """装配**当前这次 Stop 事件所属会话**的 HostAdapter（≠ 父会话 host）。

    出口线材必须按「谁在读这次 hook 输出」决定，而既有的
    ``resolve_host_adapter(parent_origin["host_kind"])`` 问的是父会话——两者在
    Kanban task / fork 场景下会不同，用错会把 ``hookSpecificOutput`` 发给 Codex
    （其 ``stop.command.output`` 是 ``additionalProperties:false``，整条 Failed）。

    **判据顺序刻意「先否定 Claude、后采信 stamp」**，因为两个方向的误判代价不对称：

    - Claude 被误判成 Codex：只是少了个非错误通道（降级到 ``decision: block``），仍能工作。
    - Codex 被误判成 Claude：出口里会出现 ``hookSpecificOutput.additionalContext``，而
      Codex 的 ``stop.command.output`` 是 ``additionalProperties: false`` ⇒ **整条 hook
      输出被判 Failed**，RVF 在 Codex 上静默停摆（``codex exec`` 下零警告）。

    而 ``source.provider`` stamp 并不可信到可以压过路径/内容证据：它是
    ``hooks/_claude_hook_entry.py`` 在**它自己那道 foreign check 之后** ``setdefault``
    上去的。那道 check 一旦漏判（自定义 ``CODEX_HOME``、或事件只带它没查的路径键），一个
    真实的 Codex 会话就会带着 ``claude-code`` stamp 走到这里。所以顺序是：

    1. 任一会话路径落在 Codex rollout 根下（默认 ``~/.codex/sessions/`` 或
       ``$CODEX_HOME/sessions/``）→ Codex。**否定 Claude 的证据排在 stamp 之前。**
    2. ``detect_transcript_format`` 嗅出当前会话 transcript 是 Codex rollout schema →
       Codex。这条不依赖任何 env / 路径约定，是路径约定被改掉时的真相源。
    3. ``detect_transcript_format`` 嗅出当前会话是 **Kimi** wire schema → Kimi；嗅出
       Claude transcript schema → Claude。理由与第 2 条一致：schema 是文件内容的事实，
       stamp 只是入口自己 ``setdefault`` 的，所以**整层** schema 证据排在**整层** stamp 之前。
       Kimi 方向的代价更高：它若被误判成 Claude 会收到 ``hookSpecificOutput.additionalContext``，
       而 Kimi 的 hook 输出 schema 是 ``looseObject`` 且只读 ``permissionDecision`` ⇒
       **静默 allow、零报错**，RVF 完全失声。实测（2026-08-23）：不加 Kimi schema 这条时，
       一个带 ``claude-code`` stamp 的 Kimi 会话确实被判成 Claude —— 这不是假想，Kimi 的 shim
       要做大量事件归一，复用或误配 Claude 入口逻辑就会打上那个 stamp。
    4. ``event["source"]["provider"]`` stamp——各 host 的 plugin 入口亲手打的标
       （``claude-code`` → Claude，``kimi-code`` → Kimi）。在没有任何 schema / 路径反证时
       它是最强的正向 host 证据，也是 **transcript 不可读 / 尚未落盘时唯一的线索**。
       Kimi 尤其依赖这一层：它的 Stop 事件原本**没有** ``transcript_path``，那个键由
       ``hooks/_kimi_hook_entry.py`` 合成（会话目录 → ``agents/main/wire.jsonl``），而合成
       有两条正常的落空路径——会话目录还没进 ``session_index.jsonl`` 且扫桶没命中，以及
       ``wire.jsonl`` 尚未落盘（``resolve_agent_wire_transcript_path`` 要求 ``is_file()``）。
       落空即探测为 ``None`` ⇒ 兜底 Codex ⇒ 发 ``decision:block`` ⇒ Kimi 静默 allow。
       这是 HOST-2（「Claude 在 transcript 不可读时仍采信 stamp」）的 Kimi 对偶。
    5. 探测结果本身（``HOST_CLAUDE`` → Claude；``None`` → resolver 兜底 Codex + 阻塞通道），
       沿用「无法识别退回 Codex」口径：兜底到最保守的通道，绝不赌未知 host 认识
       ``hookSpecificOutput``。

    **stamp 之间不排序，因为 ``provider`` 只有一个值**；而 stamp 整体仍排在第 1–3 条
    schema / 路径证据之后，所以即使某个入口的归属判据漏判、把 stamp 打到了别家的会话上，
    正向的 Codex 证据依旧最先命中——Codex 方向的既有不对称性一个字符都没被放松。

    ⚠️ **兜底的不对称性只覆盖 Codex 方向。** 「未知 → Codex + 阻塞通道」之所以安全，是因为
    Codex 收到不认识的键会**响亮**地整条 Failed。Kimi 方向没有这层保护：任何把 Kimi 判成
    别家的路径都是静默失效，所以 Kimi 只能靠**正向识别**，兜底救不了它。
    """
    if event_session_path_is_codex_rollout(event):
        return resolve_host_adapter(HOST_CODEX)
    session_path = first_readable_session_path(event)
    detected = None
    if session_path is not None:
        try:
            detected = detect_transcript_format(session_path)
        except Exception:  # noqa: BLE001 - 探测失败按未知处理，走兜底
            detected = None
    if detected == HOST_CODEX:
        return resolve_host_adapter(HOST_CODEX)
    if detected == HOST_KIMI_CODE:
        return resolve_host_adapter(HOST_KIMI_CODE)
    if detected == HOST_CLAUDE:
        # 第 3.5 条：**所有** schema 证据都排在**所有** stamp 之前。对既有行为是恒等的
        # ——探测出 Claude 时，旧代码要么走 claude-code stamp 分支、要么落到末尾的
        # ``resolve_host_adapter(detected)``，两条路都返回 Claude。它存在是为了让新增的
        # 第二枚 stamp（kimi-code）无法抢走一个内容上明确是 Claude 的会话。
        return resolve_host_adapter(HOST_CLAUDE)
    source = event.get("source")
    if isinstance(source, dict):
        stamped_host = HOST_BY_PLUGIN_ENTRY_SOURCE_PROVIDER_STAMP.get(source.get("provider"))
        if stamped_host is not None:
            return resolve_host_adapter(stamped_host)
    return resolve_host_adapter(detected)


_STOP_HOOK_REENTRY_BUDGET_DECISION_HANDED_OFF_TO_THE_EXIT_LAYER: dict[tuple[str, str], Any] = {}
"""决策层已为**同一次出口**消费好、等着出口层取走的预算判定，key = ``(session_id, reason_code)``。

同一次 Stop 出口现在有两个提问方：决策层（必须在提交 ``mark_injected`` 这类「宣称 agent 已收到」
的副作用**之前**问）与出口层 ``emit_stop_hook_payload``（所有驱动型出口的通用兜底）。两方各真
消费一次的话，每个 Stop 会把连续计数 +2，预算实际只剩一半——一条看起来在保护、实际提前一倍
触发的闸。

刻意是**取走即清**的一次性交接，不是长驻缓存：交接只覆盖「决策层刚刚为之消费、随后就要发出去」
的那一次出口。做成常驻 memo 会让「本进程内同一 reason_code 的后续出口一律免费」，那才是真正
危险的方向（预算恒不生效）。跨 turn 的累积一律由预算模块自己的落盘状态负责。
"""


def _consume_stop_hook_reentry_budget_for_event(
    event: dict[str, Any] | None,
    reason_code: str,
    *,
    hand_off_to_the_exit_layer: bool = False,
) -> Any | None:
    """为一次「将要驱动 agent」的出口消费跨 turn 再入预算；判不出会话身份时返回 ``None``。

    digest source 取 ``reason_code`` 而**不是**驱动正文：正文里带着每次新铸的
    ``RVF_DECISION_TOKEN``，拿它做 digest 会让「digest 变化」这条复位路径**每次都命中**，
    预算永不生效——一条看起来在保护、实际恒真的假保险丝。``reason_code`` 是「这次驱动属于
    哪一类事」的稳定标识，同一类事被反复催才是要抓的形状。

    ``hand_off_to_the_exit_layer=True`` 由**决策层**使用：它在提交任何「宣称 agent 已收到」的
    副作用之前先问预算，随后出口层还会为同一次出口再问一次，故把本次判定挂进交接槽，让出口层
    取走而不是重复消费。默认 ``False``——出口层自己的兜底消费**绝不**回填交接槽，否则下一次
    出口就会白捡这一份，交接退化成「同 reason_code 免费通行」的常驻 memo。

    整段 best-effort：预算模块自身出任何问题都不得让 Stop hook 失声（fail open），
    代价只是这一次不设防。
    """
    if event is None:
        return None
    try:
        session_identifier = session_id_from_event(event)
        if not session_identifier:
            return None
        decision = consume_stop_hook_reentry_budget(
            session_identifier=session_identifier,
            stable_agent_directive_scope_digest_source=reason_code,
            state_directory=session_hook_state_dir(),
        )
        if hand_off_to_the_exit_layer:
            _STOP_HOOK_REENTRY_BUDGET_DECISION_HANDED_OFF_TO_THE_EXIT_LAYER[
                (session_identifier, reason_code)
            ] = decision
        return decision
    except Exception:  # noqa: BLE001 - 预算故障绝不阻断 Stop 出口
        return None


def _take_stop_hook_reentry_budget_decision_for_stop_hook_exit(
    event: dict[str, Any] | None, reason_code: str
) -> Any | None:
    """出口层取本次出口的预算判定：优先取走决策层刚交接的那一份，没有则自己真消费一次。

    「取走即清」：交接项只能被兑现一次，之后的出口一律回到真消费，这样本函数对**没有**决策层
    前置判定的驱动型出口（如 ``reengage_decision``）行为逐字不变。
    """
    if event is not None:
        try:
            session_identifier = session_id_from_event(event)
        except Exception:  # noqa: BLE001 - 身份解析失败退回真消费路径
            session_identifier = None
        if session_identifier:
            handed_off = _STOP_HOOK_REENTRY_BUDGET_DECISION_HANDED_OFF_TO_THE_EXIT_LAYER.pop(
                (session_identifier, reason_code), None
            )
            if handed_off is not None:
                return handed_off
    return _consume_stop_hook_reentry_budget_for_event(event, reason_code)


def emit_stop_hook_payload(
    *,
    audience: str,
    agent_directive: AgentDirectiveStopHookOutput | None,
    developer_only_payload: dict[str, Any] | None,
    host_adapter: Any,
    ledger: RunLedger | None,
    reason_code: str,
    event: dict[str, Any] | None = None,
) -> None:
    """Stop hook 的**唯一**出口：受众翻译 → guardian 告警并入 → 留痕 → stdout。

    顺序是刻意的：guardian 告警会在构造**之后**改写 payload（往 ``systemMessage``
    追加），所以留痕必须排在它后面，否则 ledger 记的不是真正发出去的那份。

    翻译失败（受众契约被违反）不得让 hook 失声——Stop hook 必须 fail open。此时
    降级成一条 developer-only 的诊断，并在 ledger 里如实记下违反原因。
    """
    channel = host_adapter.stop_hook_agent_directive_channel()

    # 跨 turn 再入预算：只对**会驱动 agent 再跑一回合**的出口计数。宿主自己的保险丝不够用——
    # Kimi 的 `stop_hook_active` 恒 false 且引擎的续跑标记每 turn 复位 ⇒ 「driven 一回合 → agent
    # 停下 → 再 driven 一回合」在宿主侧完全没有上界。超预算时降级成 developer_only 诊断，
    # 而不是静默丢弃（开发者会失去唯一线索），也不是照旧驱动（那样这道闸就白加了）。
    reentry_budget_decision = None
    if audience == STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW and agent_directive is not None:
        reentry_budget_decision = _take_stop_hook_reentry_budget_decision_for_stop_hook_exit(
            event, reason_code
        )
        if reentry_budget_decision is not None and not reentry_budget_decision.agent_directive_permitted:
            developer_only_payload = {
                "continue": True,
                "systemMessage": reentry_budget_decision.exhaustion_diagnostic,
            }
            agent_directive = None
            audience = STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY

    try:
        payload = render_stop_hook_payload(
            audience=audience,
            agent_directive=agent_directive,
            developer_only_payload=developer_only_payload,
            agent_directive_channel=channel,
        )
    except Exception as exc:  # noqa: BLE001 - hook 必须 fail open
        payload = {
            "continue": True,
            "systemMessage": (
                "review-validate-fix: Stop hook 出口受众契约被违反，已降级为诊断"
                f"（reason={reason_code}）：{type(exc).__name__}: {exc}"
            ),
        }
        audience = STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY

    payload = _apply_guardian_liveness_alarm(payload)

    if ledger is not None:
        try:
            ledger.event(
                phase="gate",
                event="stop_hook_payload_emitted",
                status="completed",
                reason_code=reason_code,
                stop_hook_output_audience=audience,
                stop_hook_agent_directive_channel=(
                    channel if audience == STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW else None
                ),
                # 宿主留痕能力差异极大（只有 Claude 能从 transcript 事后取证；Codex rollout
                # 完全不承载 hook 记录），故最终正文以 RVF 自己的 ledger 为真相源。
                emitted_payload=payload,
                stop_hook_reentry_budget_consecutive_count=(
                    reentry_budget_decision.consecutive_agent_directive_count
                    if reentry_budget_decision is not None
                    else None
                ),
                stop_hook_reentry_budget_exhausted=(
                    (not reentry_budget_decision.agent_directive_permitted)
                    if reentry_budget_decision is not None
                    else None
                ),
            )
        except Exception:  # noqa: BLE001 - 留痕永不阻断出口
            pass

    if payload is not None:
        emit(payload)


def emit_developer_only_stop_hook_payload(
    payload: dict[str, Any] | None,
    *,
    host_adapter: Any,
    ledger: RunLedger | None,
    reason_code: str,
) -> None:
    """``developer_only`` 受众的便捷出口（失败 / 降级 / skip / 诊断一律走这条）。"""
    emit_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
        agent_directive=None,
        developer_only_payload=payload,
        host_adapter=host_adapter,
        ledger=ledger,
        reason_code=reason_code,
    )


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    host_adapter = resolve_current_session_host_adapter(event)
    latest_user = latest_user_message_from_event(event)
    ledger = start_stop_hook_ledger(event)
    # S1b：与 backend 无关的跨 task stranded-pending 扫荡，必须早于 suppress/launch 分支——
    # 即便本次 Stop 自身被 suppress 或不 launch，也要让它把**别的** task 遗留的 stale pending
    # 浮现给用户（打破 flow-1-self-rising 自升循环）。整段 best-effort，永不抛、不改主流程结果。
    sweep_stranded_kanban_followup_pending(event, ledger)
    # 整体 suppress 必须早于 handoff、dirty gate 和 backend launch，避免子会话停止时继续生成 review/fork artifact。
    if explicit_suppress_requested(event, latest_user) and parse_session_hook_control(latest_user) is None:
        # detached RVF/analyze/guardian 子会话显式 suppress：它们按设计永不产生发起注入，
        # 因此既不计 liveness 分母，也不向无用户受众的子会话回灌 guardian 告警。
        decision = suppressed_decision(event, ledger)
        unpark_self_parked_card(event, ledger, reason="suppressed")
        # 刻意不并入 guardian 告警：detached RVF/analyze/guardian 子会话按设计永不产生发起
        # 注入，既不计 liveness 分母、也没有用户受众可以看告警。故直接 emit，不走收口。
        if decision.payload is not None:
            emit(decision.payload)
        return 0

    # S8·R23:只统计通过 explicit suppress 的用户侧 Stop。无候选 Stop 仍会进入分母，
    # 这是当前保守 canary 的已知限制，guardian liveness 文档已明确。
    _record_guardian_stop_event_heartbeat()

    decision = evaluate_stop_event(event, ledger)
    if decision.action == "launch":
        provider_health_decision = provider_health_guard_decision(decision, event, ledger)
        if provider_health_decision is not None and provider_health_decision.payload is not None:
            unpark_self_parked_card(event, ledger, reason="provider_health_block")
            emit_stop_hook_payload(
                audience=provider_health_decision.audience,
                agent_directive=provider_health_decision.agent_directive,
                developer_only_payload=provider_health_decision.payload,
                host_adapter=host_adapter,
                ledger=ledger,
                reason_code=provider_health_decision.reason_code,
                event=event,
            )
            return 0
        workspace_guard_payload = kanban_task_workspace_guard_payload(
            event=event,
            cwd=decision.cwd,
            ledger=ledger,
        )
        if workspace_guard_payload is not None:
            unpark_self_parked_card(event, ledger, reason="workspace_guard_block")
            emit_developer_only_stop_hook_payload(
                workspace_guard_payload,
                host_adapter=host_adapter,
                ledger=ledger,
                reason_code="kanban_task_workspace_guard",
            )
            return 0
        result = launch_backend(decision, event, ledger)
        # park 泄漏安全阀：仅当本次 Stop 成功派发了 self-rising followup（resume 在路上）才保留
        # park。成功信号是 dispatch 成功路径置的内部键 `_rvf_keep_self_park`（hook_payload 顶层无
        # status 键，不能用 result["status"] 判断）；其余结局（派发失败 / kanban 早退 skip / 非
        # followup backend）= 无 resume → unpark。emit 前 pop 掉该内部键，不泄漏给 Claude。
        keep_park = _launch_result_keeps_self_park(result)
        if isinstance(result, dict):
            result.pop(_RVF_KEEP_SELF_PARK_FLAG, None)
        if not keep_park:
            unpark_self_parked_card(event, ledger, reason="no_followup_dispatch")
        emit_developer_only_stop_hook_payload(
            result,
            host_adapter=host_adapter,
            ledger=ledger,
            reason_code=decision.reason_code,
        )
        return 0
    # skip / 不 launch（含 handoff 完成）= 一般无 resume → unpark；但 followup 仍活跃 / dispatch
    # 仍在途的 skip（resume 在路上）必须保留 park，否则会在 followup 落地前误解除卡片抑制。
    if _skip_decision_should_unpark(decision):
        unpark_self_parked_card(event, ledger, reason="stop_no_launch")
    # 受众翻译 + S8·R23 发起层静默死亡告警并入出口(告警在场时静默停升格为 systemMessage)，
    # 全部收在 emit_stop_hook_payload 内。
    emit_stop_hook_payload(
        audience=decision.audience,
        agent_directive=decision.agent_directive,
        developer_only_payload=decision.payload,
        host_adapter=host_adapter,
        ledger=ledger,
        reason_code=decision.reason_code,
        event=event,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
