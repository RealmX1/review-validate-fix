"""自主请求下一条独立开审消息；本入口从不 acknowledge、prepare 或派发。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import _rvf_pyroot  # noqa: F401
from core.host_adapter.current_agent_session_identity_from_environment import resolve_current_agent_session_id_from_environment
from core.cline_kanban_terminal_delivery.cline_kanban_terminal_delivery_receipt import TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS


def _send_boundary_message(*, payload, summary, reason, now, token_root=None):
    import cline_kanban_client as client
    import rvf_analyze_advisory
    import rvf_decision_token_store as tokens
    import stop_review_validate_fix as stop

    event = {"cwd": payload["worktree"], "session_id": payload["session_id"]}
    task_id = rvf_analyze_advisory.current_kanban_task_id(event)
    if not task_id:
        raise ValueError("此会话没有已验证的独立消息投递通道；请提交一条明确的 RVF 用户命令")
    from core.review_start_boundary.review_start_boundary_verification import git_output
    common = Path(git_output(payload["worktree"], "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = Path(payload["worktree"]) / common
    project = common.resolve().parent
    key = f"rvf-decision-point-{payload['token']}"
    # 预留终端投递最坏预算；等待空闲的截止不能超出 token 的有效期。
    remaining = (tokens.parse_timestamp(payload["expires_at"]) - now).total_seconds()
    wait_ms = int((remaining - TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS) * 1000)
    if wait_ms <= 0:
        raise ValueError("token 剩余时效不足以独立投递，须在下次 Stop 重新提议")
    from kanban_followup_lock import write_decision_point_delivery_intent
    write_decision_point_delivery_intent(
        task_id=task_id, session_id=payload["session_id"], worktree=payload["worktree"],
        cwd=payload["worktree"], decision_token=payload["token"], candidate_fingerprint=payload["candidate_fingerprint"],
        kanban_project_path=str(project), kanban_attempt_id=None,
        origin_transcript_path=summary.get("origin_transcript_path"),
        state="recorded_before_send", idempotency_key=key,
    )
    body = stop._render_decision_point_injection_prompt(
        summary=summary, token=payload["token"], fork_context=None,
        delivery_transport=tokens.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
    )
    tokens.record_decision_point_banner_body_text_for_agent(payload["token"], body_text=body, root=token_root)
    result = client.send_task_message(
        task_cmd=os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", client.DEFAULT_TASK_CMD),
        repo=project, task_id=task_id, prompt=tokens.render_decision_point_out_of_band_boundary_marker(token=payload["token"]),
        prompt_file=None, source="review-validate-fix", idempotency_key=key, attempt_id=None,
        deliver_when_agent_idle=True, max_wait_for_agent_idle_ms=wait_ms,
        required_native_independent_user_turn_session_id=payload["session_id"],
    )
    tokens.record_out_of_band_delivery_status(payload["token"], status=result.get("terminal_delivery_status"),
                                             detail={"kanban_task_id": task_id, "kanban_project_path": str(project), "idempotency_key": key}, root=token_root)
    return result


def run_request_review_start_boundary(
    *, reason: str, decision_token: str | None = None, deferred_decision_token: str | None = None,
    roots=None, now: datetime | None = None, candidate_summary_builder: Callable | None = None,
    boundary_sender: Callable | None = None,
):
    import rvf_agent_decision_cli as cli
    import rvf_decision_token_store as tokens
    import stop_decision_candidate_summary

    roots = roots or cli.AgentDecisionRoots()
    now = now or datetime.now(timezone.utc)
    source = deferred_decision_token or decision_token
    superseded_archive_result = None
    def result(status, message, token=source, detail=None):
        return cli.CommandResult(command="request-review-start-boundary", status=status,
                                 exit_code=cli.EXIT_REVIEW_START_BOUNDARY_NOT_VERIFIED,
                                 token=token, message=message, detail={
                                     **({"source_run_terminal_archive": superseded_archive_result} if superseded_archive_result else {}),
                                     **(detail or {}),
                                 })
    if not source or bool(decision_token) == bool(deferred_decision_token) or not reason.strip():
        return result("error", "须提供唯一来源 token 与自主判断理由")
    lookup = tokens.read_decision_token(source, root=roots.token_root, now=now)
    payload = lookup.payload
    if not payload:
        return result("error", "找不到来源 token")
    current_session = resolve_current_agent_session_id_from_environment()
    if not current_session or payload.get("session_id") != current_session:
        return result("review_start_boundary_invalidated", "只能为当前会话请求开审分界")
    ordinary_source_was_superseded_by_boundary_request = (
        not deferred_decision_token and lookup.state == "withdrawn"
        and payload.get("withdrawal_cause") == "superseded_review_start_boundary"
    )
    if deferred_decision_token:
        if lookup.state != "declined" or payload.get("decline_kind") != "deferred_until_condition":
            return result("error", "自主恢复仅适用于 deferred_until_condition，不得推翻结构性谢绝")
    elif lookup.status != "valid":
        return result("review_start_boundary_invalidated", "来源 token 已失效，不能重新请求开审")
    elif lookup.state not in ("minted", "injected", "acknowledged") and not ordinary_source_was_superseded_by_boundary_request:
        return result("error", "已终态的 token 不能重新请求开审")
    newest = max(tokens.iter_decision_tokens_for_session(worktree=payload["worktree"], session_id=payload["session_id"], root=roots.token_root, now=now),
                 key=lambda record: str((record.payload or {}).get("minted_at") or ""), default=None)
    reusing_requested_boundary = False
    if newest is not None and newest.token != source:
        if (newest.payload or {}).get("boundary_request_source_token") == source and newest.status == "valid" and newest.state in ("minted", "injected"):
            payload = newest.payload
            reusing_requested_boundary = True
        else:
            return result("error", "已有更新的决策，不得用旧 token 再开一轮")
    # 已撤回的普通来源只能恢复磁盘上仍有效的同一请求，不能凭旧来源另铸一轮。
    if ordinary_source_was_superseded_by_boundary_request and not reusing_requested_boundary:
        return result("error", "已撤回来源没有可继续投递的派生请求")
    event = {"cwd": payload["worktree"], "session_id": payload["session_id"]}
    previous_summary = payload.get("candidate_summary") or {}
    if previous_summary.get("origin_transcript_path"):
        event["transcript_path"] = previous_summary["origin_transcript_path"]
    builder = candidate_summary_builder or (lambda event, repo: stop_decision_candidate_summary.build_candidate_summary(event=event, cwd=repo, repo=repo, ledger=None))
    summary = builder(event, payload["worktree"])
    if not summary.get("candidate_path_count"):
        return result("error", "当前没有可审候选")
    if event.get("transcript_path"):
        summary["origin_transcript_path"] = event["transcript_path"]
    changed = summary.get("candidate_fingerprint") != payload.get("candidate_fingerprint")
    if ordinary_source_was_superseded_by_boundary_request and changed:
        return result("review_start_boundary_invalidated", "候选已变化，不能用已撤回来源恢复旧请求")
    if not reusing_requested_boundary and payload.get("state") == "acknowledged" and payload.get("run_anchor_dir"):
        from core.run_ledger.review_run_lifecycle import read_review_run_lifecycle
        from review_run_terminal_archive import request_review_run_terminal_archive, UNSUCCESSFUL_TERMINAL_KINDS

        previous_run = Path(payload["run_anchor_dir"])
        try:
            terminal = read_review_run_lifecycle(previous_run).get("review_run_terminal_status")
            if not payload.get("last_start_failure") and terminal not in UNSUCCESSFUL_TERMINAL_KINDS:
                return result("review_start_boundary_invalidated", "此 token 已有准备中的 run；须先结束该准备尝试并释放范围，不能把旧准备转给新候选")
            superseded_archive_result = request_review_run_terminal_archive(
                run_dir=previous_run, decision_kind="superseded", reason=f"自主请求新独立分界替代失败准备：{reason}", event=event,
            )
        except Exception as exc:
            return result("review_start_boundary_pending", f"旧准备仍待终态补证：{type(exc).__name__}: {exc}")
        if superseded_archive_result.get("status") != "archived":
            return result("review_start_boundary_pending", "旧准备的依赖证据仍待恢复；归档请求已保留，未审范围不豁免")
    if not reusing_requested_boundary or changed:
        if payload.get("state") in ("minted", "injected", "acknowledged"):
            tokens.mark_withdrawn(payload["token"], root=roots.token_root, now=now,
                                  reason="自主请求下一独立开审分界", withdrawal_cause="superseded_review_start_boundary",
                                  withdraw_only_if_state_inside_lock_is=frozenset({payload["state"]}))
        mint = tokens.mint_decision_token(
            worktree=payload["worktree"], session_id=payload["session_id"], candidate_fingerprint=summary["candidate_fingerprint"],
            candidate_summary=summary, now=now, root=roots.token_root, cooldown_root=roots.cooldown_root,
            delivery_transport=tokens.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
            agent_requested_deferred_reconsideration_token=deferred_decision_token,
            extra_token_payload_fields={"boundary_request_source_token": source, "agent_condition_reconsideration_reason": reason},
        )
        if not mint.token or mint.outcome != tokens.MINT_OUTCOME_MINTED:
            return result("review_start_boundary_pending", f"重新提议未获放行：{mint.outcome}")
        payload = mint.record.payload
    try:
        fields = dict(payload=payload, summary=summary, reason=reason, now=now)
        delivery = boundary_sender(**fields) if boundary_sender else _send_boundary_message(**fields, token_root=roots.token_root)
    except Exception as exc:
        return result("review_start_boundary_pending", f"独立消息尚未验证：{type(exc).__name__}: {exc}", token=payload["token"])
    return result("review_start_boundary_pending", "独立消息已请求；等待宿主证明下一独立 turn 与对应 checkpoint，不在本 turn 开审。",
                  token=payload["token"], detail={"delivery": delivery, "initiated_by_explicit_user_request": False})
