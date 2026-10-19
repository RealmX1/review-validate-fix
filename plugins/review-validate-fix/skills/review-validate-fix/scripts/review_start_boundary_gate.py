"""开审门禁装配：独立宿主消息 + 不可变 Git 快照 + 当前候选，缺证据即等待。"""

from __future__ import annotations

import os
import json
import uuid
import fcntl
import hashlib
from pathlib import Path
from typing import Any, Mapping

import _rvf_pyroot  # noqa: F401
from core.review_start_boundary.review_start_boundary_verification import (
    INVALIDATED, PENDING, ReviewStartBoundaryNotVerified, read_receipt,
    verify_independent_user_turn_boundary, receipt_digest, git_output,
)


def review_start_boundary_receipt_path(token: str) -> Path:
    import rvf_decision_token_store

    validated = rvf_decision_token_store.validate_token(token)
    return Path(os.environ.get("RVF_HOME") or Path.home() / ".rvf") / "review-start-independent-user-turn-boundary-receipts" / f"{validated}.json"


def _read_or_reconcile_host_boundary_receipt(token_payload: Mapping[str, Any], supplied_receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """每次读取宿主活动分支；静态 rollout 和旧 receipt 不能证明未提交的原生 rewind。"""
    import cline_kanban_client as client

    path = review_start_boundary_receipt_path(str(token_payload["token"]))
    cached_receipt = supplied_receipt or (read_receipt(path) if path.exists() else {})
    detail = token_payload.get("out_of_band_delivery_detail") or {}
    if not isinstance(detail, Mapping):
        detail = {}
    task_id = detail.get("kanban_task_id") or cached_receipt.get("kanban_task_id") or (token_payload.get("candidate_summary") or {}).get("kanban_task_id")
    key = detail.get("idempotency_key") or cached_receipt.get("idempotency_key")
    if not key and token_payload.get("initiated_by_explicit_user_request") is False:
        key = f"rvf-decision-point-{token_payload['token']}"
    project = detail.get("kanban_project_path") or cached_receipt.get("kanban_project_path")
    if not task_id or not key:
        raise ReviewStartBoundaryNotVerified(PENDING, "尚无可查询活动分支的宿主消息定位，静态 receipt 不足以开审")
    if not project:
        from kanban_followup_lock import read_decision_point_delivery_intent
        intent = read_decision_point_delivery_intent(task_id=task_id)
        if isinstance(intent, dict) and intent.get("decision_token") == token_payload["token"]:
            project = intent.get("kanban_project_path")
    if not project:
        common = Path(git_output(str(token_payload["worktree"]), "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = Path(str(token_payload["worktree"])) / common
        project = str(common.resolve().parent)
    nonce = uuid.uuid4().hex
    try:
        status = client.read_task_message_terminal_delivery_status(
            task_cmd=os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", client.DEFAULT_TASK_CMD),
            repo=Path(project), task_id=task_id, idempotency_key=key, timeout=10,
            review_start_boundary_observation_nonce=nonce,
        )
    except Exception as exc:
        raise ReviewStartBoundaryNotVerified(PENDING, "宿主尚未提供本次请求的活动分支观测") from exc
    receipt = status.get("independent_user_turn_boundary_receipt")
    observation = status.get("review_start_boundary_active_branch_observation")
    if not isinstance(receipt, dict) or not isinstance(observation, dict):
        raise ReviewStartBoundaryNotVerified(PENDING, "投递回执尚未包含独立分界与活动分支证明")
    if observation.get("request_nonce") != nonce or observation.get("kanban_task_id") != task_id or observation.get("idempotency_key") != key:
        raise ReviewStartBoundaryNotVerified(PENDING, "宿主返回的活动分支观测不是本次查询，拒绝缓存或迟到回执")
    if receipt.get("decision_token") != token_payload["token"] or receipt.get("session_id") != observation.get("session_id"):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "宿主分支、静态分界与 token 身份不一致")
    active_messages = observation.get("native_active_branch_user_message_ids")
    if not isinstance(active_messages, list) or not observation.get("native_active_branch_user_turn_id"):
        raise ReviewStartBoundaryNotVerified(PENDING, "宿主尚未提供完整活动分支消息快照")
    if active_messages.count(receipt.get("native_user_message_id")) != 1 or observation.get("native_active_branch_user_turn_id") != receipt.get("native_turn_id"):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "开审消息已不在宿主当前活动分支或已进入另一 turn")
    if cached_receipt and receipt_digest(cached_receipt) != receipt_digest(receipt):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "本次宿主分界与已冻结 receipt 不一致")
    return receipt



def _assert_or_create_immutable_boundary_binding_record(path: Path, expected: Mapping[str, Any], conflict_reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            if read_receipt(path) != dict(expected):
                raise ReviewStartBoundaryNotVerified(INVALIDATED, conflict_reason)
        else:
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.pending-write")
            try:
                temporary.write_text(json.dumps(dict(expected), sort_keys=True), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

def require_review_start_boundary(
    *, token_payload: Mapping[str, Any], repo: str, session_id: str | None = None,
    transcript_path: str | None = None, receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from core.host_adapter.current_agent_session_identity_from_environment import resolve_current_agent_session_id_from_environment
    import host_adapter_resolver
    import stop_decision_candidate_summary

    current_session = resolve_current_agent_session_id_from_environment()
    repo = git_output(repo, "rev-parse", "--show-toplevel")
    session_id = session_id or current_session or token_payload.get("session_id")
    if current_session and session_id != current_session:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "调用方当前会话与 token 的会话不一致")
    if session_id != token_payload.get("session_id") or Path(repo).resolve() != Path(str(token_payload.get("worktree") or "")).resolve():
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "调用方 session/worktree 与 token 不一致")
    token = token_payload.get("token")
    if not token:
        raise ReviewStartBoundaryNotVerified(PENDING, "尚未绑定开审决策 token")
    if receipt is None:
        path = review_start_boundary_receipt_path(str(token))
        receipt = read_receipt(path) if path.exists() else _read_or_reconcile_host_boundary_receipt(token_payload)
    native_transcript = receipt.get("transcript_path")
    if transcript_path and Path(transcript_path).resolve() != Path(str(native_transcript or "")).resolve():
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "receipt 与本轮 transcript 不一致")
    event = {"session_id": session_id, "cwd": repo, "transcript_path": native_transcript}
    adapter = host_adapter_resolver.resolve_current_session_host_adapter(event)
    reader = getattr(adapter, "read_independent_user_turn_boundary_evidence", None)
    if reader is None:
        raise ReviewStartBoundaryNotVerified(PENDING, "此宿主尚未验证独立消息与原生回退能力")
    verified = verify_independent_user_turn_boundary(
        receipt, token_payload=token_payload, repo=repo, session_id=session_id,
        native_boundary_reader=reader,
        candidate_summary_builder=lambda: stop_decision_candidate_summary.build_candidate_summary(event=event, cwd=repo, repo=repo, ledger=None),
    )
    # Git/候选核验可能耗时；状态迁移前重新读取活动分支，静态缓存不能成为授权。
    _read_or_reconcile_host_boundary_receipt(token_payload, verified)
    path = review_start_boundary_receipt_path(str(token))
    native_message_key = hashlib.sha256(json.dumps([session_id, verified["native_user_message_id"]], ensure_ascii=False).encode()).hexdigest()
    _assert_or_create_immutable_boundary_binding_record(
        path.parent.parent / "review-start-native-user-message-decision-token-bindings" / f"{native_message_key}.json",
        {"decision_token":token, "receipt_sha256":receipt_digest(verified)},
        "此原生消息已绑定另一 token 或另一份 checkpoint，不能充当新一轮起点",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f".{uuid.uuid4().hex}.pending-write")
    try:
        temporary_path.write_text(json.dumps(verified, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return verified


def require_review_start_boundary_for_prepare(*, repo: str, receipt: Mapping[str, Any] | None = None, expected_session_id: str | None = None) -> dict[str, Any]:
    if receipt is None and os.environ.get("RVF_REVIEW_START_BOUNDARY_RECEIPT"):
        receipt = read_receipt(os.environ["RVF_REVIEW_START_BOUNDARY_RECEIPT"])
    if not isinstance(receipt, dict):
        raise ReviewStartBoundaryNotVerified(PENDING, "准备输入尚未绑定独立消息与 checkpoint")
    if expected_session_id is not None and receipt.get("session_id") != expected_session_id:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "准备来源会话与独立消息不一致")
    import rvf_decision_token_store

    lookup = rvf_decision_token_store.read_decision_token(str(receipt.get("decision_token") or ""))
    if lookup.payload is None or lookup.state not in ("minted", "injected", "acknowledged", "dispatched") or (lookup.status == "expired" and not lookup.revivable):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "分界 token 尚未接受或已失效")
    verified = require_review_start_boundary(token_payload=lookup.payload, repo=repo, receipt=receipt)
    if lookup.state in ("minted", "injected"):
        # 直接 prepare 同样先过证据 gate，再接受决策；不能用它绕过 acknowledge 的分界条件。
        rvf_decision_token_store.acknowledge_decision_token(lookup.token)
    return verified


def require_prepared_run_review_start_boundary(*, repo: str | None, scope_contract: str | None, run_id: str | None = None, run_dir: str | Path | None = None) -> dict[str, Any]:
    if not repo or not scope_contract:
        raise ReviewStartBoundaryNotVerified(PENDING, "派发前缺少 repo 或已绑定分界的 scope contract")
    contract = read_receipt(scope_contract)
    receipt = require_review_start_boundary_for_prepare(repo=repo, receipt=contract.get("review_start_boundary_receipt"))
    if run_id and run_id != contract.get("run_id"):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "派发 run 与已冻结 contract 不一致")
    bind_review_start_boundary_to_one_run(receipt=receipt, run_id=run_id or contract.get("run_id"), run_dir=run_dir)
    return receipt


def bind_review_start_boundary_to_one_run(*, receipt: Mapping[str, Any], run_id: str | None, run_dir: str | Path | None) -> tuple[str, Path]:
    """在创建正式 run 目录之前占用唯一绑定；重启复用，新 run 不能借用旧消息。"""
    from core.run_ledger.run_ledger import log_root

    token = str(receipt["decision_token"])
    resolved_id = run_id or os.environ.get("RVF_RUN_ID") or f"rvf-review-start-{token}"
    resolved_directory = Path(run_dir or os.environ.get("RVF_RUN_DIR") or log_root() / "runs" / resolved_id).expanduser().resolve()
    receipt_path = review_start_boundary_receipt_path(token)
    binding_path = receipt_path.with_suffix(".run-binding.json")
    expected = {"run_id": resolved_id, "run_dir": str(resolved_directory), "receipt_sha256": receipt_digest(receipt)}
    _assert_or_create_immutable_boundary_binding_record(binding_path, expected, "此消息/checkpoint 已绑定另一 run 或另一份分界证明")
    return resolved_id, resolved_directory
