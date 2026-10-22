"""持续关闭无 Kanban task 的 Codex GUI 自动 Stop 流程。

这是版本控制内的产品策略，不是一次部署的环境覆盖：没有 TTL，也不接受会话级 on 或
channel 切换解除。只有用户明确要求恢复后才修改此策略。手动 UPS / CLI 入口不调用它。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401
from adapters.codex.transcript import read_codex_originator
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import SESSION_PATH_KEYS
from host_adapter_resolver import resolve_current_session_host_adapter
from kanban_followup_lock import clear_markers_belonging_to_handed_off_run
from rvf_analyze_advisory import current_kanban_task_id
from rvf_handoff import handoff_path_from_event, validate_handoff_path
from rvf_run_finalize import finalize_for_handoff, surface_finalize_record_errors


CODEX_GUI_WITHOUT_KANBAN_TASK_STOP_HOOK_DISABLED_REASON = (
    "codex_gui_without_kanban_task_stop_hook_disabled"
)


def codex_gui_without_kanban_task_stop_hook_is_disabled(event: dict[str, Any]) -> bool:
    """仅正向确认 Desktop 且无 task 绑定时关闭，不把 CLI / 未知客户端推断成 GUI。"""
    if current_kanban_task_id(event):
        return False
    if resolve_current_session_host_adapter(event).cline_kanban_agent_id() != "codex":
        return False
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            originator = read_codex_originator(Path(value).expanduser())
        except (OSError, ValueError, RuntimeError):
            continue
        if originator is not None:
            return originator.strip().casefold() == "codex desktop"
    return False


def disabled_codex_gui_stop_hook_payload(
    event: dict[str, Any], ledger: Any,
) -> dict[str, Any] | None:
    """返回 developer-only 的 skip；唯一允许的运行副作用是有效 handoff 的静默收尾。"""
    if not codex_gui_without_kanban_task_stop_hook_is_disabled(event):
        return None

    # reviewer / analyze 子线程的显式 suppress 仍优先于收尾副作用。
    suppressed = any(
        os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "y", "on"}
        for name in ("RVF_SUPPRESS", "RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD")
    )
    handoff_path = None if suppressed else handoff_path_from_event(event)
    if handoff_path is not None:
        valid, invalid_reason = validate_handoff_path(handoff_path)
        if valid:
            try:
                # 仅有效交接收尾才加载核心解析；身份与 suppress 均复用在途流程的同一契约，
                # 不进入 dirty / scope / dispatch 等自动 gate。
                from stop_review_validate_fix import (
                    explicit_suppress_requested,
                    kanban_followup_lock_repository_anchor,
                    kanban_followup_lock_session_id,
                    latest_user_message_from_event,
                )

                if not explicit_suppress_requested(event, latest_user_message_from_event(event)):
                    record = finalize_for_handoff(
                        handoff_path=handoff_path, event=event,
                        decision_kind="codex-gui-without-kanban-task-silent-handoff",
                    )
                    surface_finalize_record_errors(ledger, record)
                    # 无法定位 run 时不清锁。只清本 session + repository 的 marker，
                    # 不从 handoff 的父 task 元数据反推 task，不碰其它会话。
                    if record is not None:
                        removed, kept = clear_markers_belonging_to_handed_off_run(
                            task_id=None, session_id=kanban_followup_lock_session_id(event),
                            run_id=record.get("run_id"), run_dir=record.get("run_dir"),
                            repo=kanban_followup_lock_repository_anchor(
                                event, cwd=event.get("cwd"), marker=record,
                            ),
                        )
                        ledger.event(
                            phase="complete", event="codex_gui_silent_handoff_finalized",
                            status="completed", reason_code="handoff_file_ready",
                            handoff_path=str(handoff_path), removed_marker_paths=removed,
                            kept_in_progress_markers=kept,
                        )
            except Exception as exc:
                ledger.event(
                    phase="complete", event="codex_gui_silent_handoff_finalize_failed",
                    status="warning", reason_code="finalize_error", level="warn",
                    error={"kind": type(exc).__name__, "message": str(exc)},
                )
        else:
            ledger.event(
                phase="complete", event="codex_gui_silent_handoff_invalid",
                status="skipped", reason_code=f"handoff_file_{invalid_reason}",
            )

    return ledger.hook_payload(
        status="skipped", reason_code=CODEX_GUI_WITHOUT_KANBAN_TASK_STOP_HOOK_DISABLED_REASON,
        continue_=True,
        message="无 Kanban task 的 Codex GUI 自动 RVF 已持续关闭；手动触发和静默收尾仍可用。",
        detail="直到用户明确要求恢复前保持关闭，redeploy 不解除此策略。",
    )
