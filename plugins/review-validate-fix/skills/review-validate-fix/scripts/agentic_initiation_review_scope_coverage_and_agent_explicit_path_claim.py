#!/usr/bin/env python3
"""agent 发起的一轮 RVF：如实报告「正式审查覆盖了多少候选路径」，并支持 agent 显式认领未覆盖的路径。

背景（08ac4 调查）：

- **覆盖率被高估，agent 多数时候不告诉用户。** start 回执写「自动 scope N 路径」，dispatch 回执写
  「primary_files N」，N 都是候选集大小，而真正进正式审查（tracker ``primary_units`` / ``fix_allowlist``）的
  常常只有一小部分。92 个漏审 run 里 67 个从没提过。
- **``rvf scope include`` 补不进正式审查。** include 只把路径并进 ``primary_files``（land 的 stage 边界、
  reviewer 的提示），不进 ``fix_allowlist`` / ``primary_units``，路径仍在 protected。agent 用了 11 次，被补
  的路径无一例外仍不在审查名单里。

本模块提供三件事：

1. :func:`compute_review_scope_coverage_against_candidate_paths`：X/Y = 正式覆盖（``fix_allowlist``）/ 候选集
   （扣掉 agent 排除的），未覆盖的按 session-manifest 里的原因分组；
2. :func:`render_review_scope_coverage_summary_line`：一行人话，start / claim / include / dispatch 回执共用；
3. :func:`claim_paths_into_formal_review_scope_of_prepared_run`：``rvf scope claim`` 的实现——认领落 tracker、
   重建 session-manifest（认领作为 ``agent_explicit_claim`` 证据注入）、按并集重新分配 lease、改写 scope
   contract（认领路径进 ``primary_files`` / ``fix_allowlist`` / ``primary_units``，移出 protected / background）、
   用冻结的命令重渲 review packet。有 lease 的 run 里只有真正进了新 tracker scope 的认领路径才算正式覆盖；
   重分配失败（旧 lease 已释放）时清空 contract 的 lease 字段、退回按文件定范围。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

UNCOVERED_REASON_INCLUDED_WITHOUT_FORMAL_REVIEW = "included_without_formal_review"
UNCOVERED_REASON_NOT_IN_FORMAL_REVIEW_SCOPE_REASON_UNKNOWN = "not_in_formal_review_scope_reason_unknown"
# ``rvf scope claim`` 之后本轮 tracker lease 的四种结局（结果字段 ``tracker_lease_state_after_claim``）。
TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_NEW_LEASE_ALLOCATED = (
    "prior_tracker_lease_released_and_new_lease_allocated"
)
TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_REALLOCATION_FAILED_RUN_HAS_NO_LEASE = (
    "prior_tracker_lease_released_and_reallocation_failed_run_now_has_no_tracker_lease"
)
TRACKER_LEASE_STATE_AFTER_CLAIM_REALLOCATION_SKIPPED_PRIOR_LEASE_KEPT = (
    "tracker_lease_reallocation_skipped_prior_lease_kept_claimed_paths_not_admitted"
)
TRACKER_LEASE_STATE_AFTER_CLAIM_RUN_NEVER_HAD_TRACKER_LEASE = "run_never_had_tracker_lease_scope_is_file_based"

UNCOVERED_REASON_LABELS_IN_CHINESE = {
    "contested": "有争议",
    "written_by_other_agent_session": "其他会话写的",
    "no_agent_session_tool_call_matches": "谁都对不上",
    UNCOVERED_REASON_INCLUDED_WITHOUT_FORMAL_REVIEW: "已 include、未进正式审查",
    UNCOVERED_REASON_NOT_IN_FORMAL_REVIEW_SCOPE_REASON_UNKNOWN: "原因不明",
}


def _json_or_none(path: Any) -> dict[str, Any] | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_scope_contract_and_session_manifest_of_overlay(
    overlay: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    artifacts = overlay.get("prepare_artifacts") if isinstance(overlay.get("prepare_artifacts"), Mapping) else {}
    contract = _json_or_none(artifacts.get("scope_contract"))
    manifest_path = artifacts.get("session_manifest")
    if not manifest_path and isinstance(contract, dict):
        manifest_path = contract.get("session_manifest_path")
    return contract, _json_or_none(manifest_path)


def compute_review_scope_coverage_against_candidate_paths(
    *,
    overlay: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    session_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """X/Y：正式审查覆盖的候选路径数 / 候选路径数（扣掉 agent 排除的）。

    「正式审查覆盖」= scope contract 的 ``fix_allowlist``：它与 tracker ``primary_units`` 同源，是 reviewer 必审、
    fix 可写、审完能标 reviewed 的那一份。``primary_files`` 不算——include 进去的路径也在里面。"""
    candidate_paths = [str(path) for path in (overlay.get("auto_scope_paths") or [])]
    excluded_paths = {
        str(entry.get("path")) for entry in (overlay.get("exclusions") or []) if isinstance(entry, Mapping) and entry.get("path")
    }
    included_paths = {
        str(entry.get("path")) for entry in (overlay.get("inclusions") or []) if isinstance(entry, Mapping) and entry.get("path")
    }
    formal_review_paths = {str(path) for path in ((contract or {}).get("fix_allowlist") or [])}
    reason_by_path: dict[str, str] = {}
    for reason, entries in ((session_manifest or {}).get("uncovered_dirty_paths_by_reason") or {}).items():
        for entry in entries or []:
            if isinstance(entry, Mapping) and entry.get("path"):
                reason_by_path[str(entry["path"])] = str(reason)
    considered = [path for path in candidate_paths if path not in excluded_paths]
    covered = [path for path in considered if path in formal_review_paths]
    uncovered_by_reason: dict[str, list[str]] = {}
    for path in considered:
        if path in formal_review_paths:
            continue
        if path in included_paths:
            reason = UNCOVERED_REASON_INCLUDED_WITHOUT_FORMAL_REVIEW
        else:
            reason = reason_by_path.get(path, UNCOVERED_REASON_NOT_IN_FORMAL_REVIEW_SCOPE_REASON_UNKNOWN)
        uncovered_by_reason.setdefault(reason, []).append(path)
    uncovered_without_agent_decision = [
        path for path in considered if path not in formal_review_paths and path not in included_paths
    ]
    return {
        "candidate_path_count": len(candidate_paths),
        "excluded_candidate_path_count": len(candidate_paths) - len(considered),
        "considered_candidate_path_count": len(considered),
        "formal_review_covered_candidate_path_count": len(covered),
        "formal_review_scope_known": contract is not None,
        "uncovered_candidate_paths_by_reason": uncovered_by_reason,
        "uncovered_candidate_paths_without_agent_decision": uncovered_without_agent_decision,
    }


def render_review_scope_coverage_summary_line(coverage: Mapping[str, Any], *, token: str, rvf_command: str) -> str:
    if not coverage.get("formal_review_scope_known"):
        return "正式审查覆盖：未知（读不到 scope contract）。"
    covered = coverage.get("formal_review_covered_candidate_path_count", 0)
    considered = coverage.get("considered_candidate_path_count", 0)
    excluded = coverage.get("excluded_candidate_path_count", 0)
    line = f"正式审查覆盖 {covered}/{considered} 个候选路径"
    if excluded:
        line += f"（另有 {excluded} 个已排除）"
    uncovered_by_reason = coverage.get("uncovered_candidate_paths_by_reason") or {}
    uncovered_total = sum(len(paths) for paths in uncovered_by_reason.values())
    if not uncovered_total:
        return line + "。"
    parts = [
        f"{UNCOVERED_REASON_LABELS_IN_CHINESE.get(reason, reason)} {len(paths)}"
        for reason, paths in sorted(uncovered_by_reason.items())
    ]
    line += f"；⚠ 未覆盖 {uncovered_total} 个（{'、'.join(parts)}）"
    if coverage.get("uncovered_candidate_paths_without_agent_decision"):
        line += (
            f"。本会话写的用 `{rvf_command} scope claim --token {token} <path>… --reason \"…\"` 认领进正式审查，"
            f"不该审的用 `{rvf_command} scope exclude`；确认就这样派发时 dispatch 加 `--accept-uncovered \"<理由>\"`"
        )
    return line + "。"


# --------------------------------------------------------------------------------------------
# rvf scope claim
# --------------------------------------------------------------------------------------------


def _git_dirty_paths(repo: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], cwd=repo, capture_output=True, check=False
    )
    parts = [part for part in completed.stdout.split(b"\0") if part]
    paths: set[str] = set()
    index = 0
    while index < len(parts):
        record = parts[index].decode("utf-8", "surrogateescape")
        if len(record) >= 4:
            paths.add(record[3:])
            if "R" in record[:2] or "C" in record[:2]:
                index += 1
                if index < len(parts):
                    paths.add(parts[index].decode("utf-8", "surrogateescape"))
        index += 1
    return paths


def _recompute_scope_contract_hash(contract: dict[str, Any]) -> None:
    import prepare_review_run  # noqa: PLC0415 - 复用 canonical 序列化，保证与 prepare / finalize 同口径

    prior_canonical_scope = contract.get("canonical_scope")
    canonical_scope = dict(prior_canonical_scope) if isinstance(prior_canonical_scope, Mapping) else {}
    canonical_scope.update(
        {
            "version": contract.get("version", prepare_review_run.SCOPE_CONTRACT_VERSION),
            "repo": contract.get("repo"),
            "scope_mode": contract.get("scope_mode"),
            "primary_files": contract.get("primary_files") or [],
            "background_files": contract.get("background_files") or [],
            "protected_files": contract.get("protected_files") or [],
            "canonical_issues": contract.get("canonical_issues") or [],
            "fix_allowlist": contract.get("fix_allowlist") or [],
            "excluded_path_prefixes": contract.get("excluded_path_prefixes") or [],
        }
    )
    contract["canonical_scope"] = canonical_scope
    contract["scope_hash"] = hashlib.sha256(prepare_review_run.canonical_json_bytes(canonical_scope)).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def claim_paths_into_formal_review_scope_of_prepared_run(
    *,
    overlay: Mapping[str, Any],
    token_payload: Mapping[str, Any],
    paths: Sequence[str],
    reason: str,
    token: str,
    tracker_reviewer_id: str,
    tracker_lease_ttl_seconds: int | None,
) -> dict[str, Any]:
    """把 ``paths`` 认领进本轮的正式审查。返回结构化结果；参数 / 状态问题以 ``status`` 报出，不抛。"""
    from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: PLC0415
    from core.session_scope_allocation.session_change_manifest import build_manifest  # noqa: PLC0415

    import prepare_review_run  # noqa: PLC0415

    repo = Path(str(overlay.get("repo") or overlay.get("worktree") or "")).expanduser().resolve()
    session_id = overlay.get("session_id") if isinstance(overlay.get("session_id"), str) else None
    run_id = overlay.get("run_id") if isinstance(overlay.get("run_id"), str) else None
    artifacts = overlay.get("prepare_artifacts") if isinstance(overlay.get("prepare_artifacts"), Mapping) else {}
    contract_path_raw = artifacts.get("scope_contract")
    if not (isinstance(contract_path_raw, str) and contract_path_raw):
        return {"status": "no_scope_contract_artifact"}
    contract_path = Path(contract_path_raw)
    contract = _json_or_none(contract_path_raw)
    if contract is None:
        return {"status": "unreadable_scope_contract", "scope_contract": contract_path_raw}

    requested = []
    for raw_path in paths:
        path = str(raw_path).strip()
        while path.startswith("./"):
            path = path[2:]
        if path:
            requested.append(path)
    candidate_paths = {str(path) for path in (overlay.get("auto_scope_paths") or [])}
    dirty = _git_dirty_paths(repo)
    rejected = [path for path in requested if path not in candidate_paths and path not in dirty]
    accepted = [path for path in dict.fromkeys(requested) if path not in rejected]
    if not accepted:
        return {"status": "no_claimable_paths", "rejected_paths_not_in_candidates_or_dirty": rejected}

    if session_id:
        claim_record = reviewable_unit_diff_tracker.record_session_path_claims_by_agent_explicit_declaration(
            repo=repo, session_id=session_id, paths=accepted, reason=reason, decision_token=token, run_id=run_id
        )
    else:
        claim_record = {"status": "skipped_no_session_identity"}

    candidate_summary = token_payload.get("candidate_summary") if isinstance(token_payload.get("candidate_summary"), Mapping) else {}
    committed_baseline = candidate_summary.get("committed_round_baseline") or None
    committed_base_ref = candidate_summary.get("committed_round_base_ref") or None
    transcript_path_raw = overlay.get("transcript_path")
    transcript_path = Path(transcript_path_raw).expanduser() if isinstance(transcript_path_raw, str) and transcript_path_raw else None
    artifacts_dir = contract_path.parent.parent if contract_path.parent.name == "inputs" else contract_path.parent
    old_lease_id = contract.get("tracker_lease_id") if isinstance(contract.get("tracker_lease_id"), str) else None
    old_primary = [str(path) for path in contract.get("primary_files") or []]
    old_background = [str(path) for path in contract.get("background_files") or []]

    reallocation: dict[str, Any] = {"status": "not_attempted"}
    reallocation_attempted_after_releasing_prior_lease = False
    manifest: dict[str, Any] | None = None
    if transcript_path is not None and transcript_path.is_file():
        manifest = build_manifest(
            repo,
            transcript_path,
            tracker_run_id=run_id,
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
            enable_layered_session_write_attribution=True,
        )
    new_tracker_scope: dict[str, Any] | None = None
    if old_lease_id and not (session_id and run_id and manifest is not None):
        # 跳过重分配：旧 lease 没释放、仍然存活，contract 的 lease 字段原样保留。
        reallocation = {
            "status": "skipped_prior_lease_kept",
            "prior_lease_id": old_lease_id,
            "skip_reason": "session_identity_missing"
            if not session_id
            else ("run_id_missing" if not run_id else "session_transcript_missing"),
        }
    if old_lease_id and session_id and run_id and manifest is not None:
        reallocation_attempted_after_releasing_prior_lease = True
        prior_scope_path = artifacts_dir / "tracker-scope.json"
        prior_unit_ids: list[str] = []
        prior_scope = _json_or_none(str(prior_scope_path))
        if prior_scope is not None:
            prior_unit_ids = [str(unit_id) for unit_id in prior_scope.get("unit_ids") or []]
        reviewable_unit_diff_tracker.lease_release(repo=str(repo), lease_id=old_lease_id, reason="agent_explicit_claim_reallocation")
        patch_ownership = manifest.get("patch_ownership") if isinstance(manifest.get("patch_ownership"), dict) else {}
        allocation = reviewable_unit_diff_tracker.allocate_review_scope(
            repo=repo,
            session_id=session_id,
            run_id=run_id,
            reviewer_id=tracker_reviewer_id,
            output_scope_path=prior_scope_path,
            holder_kind="reviewer",
            dry_run=False,
            lease_ttl_seconds=tracker_lease_ttl_seconds,
            transcript_max_line_number=patch_ownership.get("transcript_max_line_number")
            if isinstance(patch_ownership.get("transcript_max_line_number"), int)
            else None,
            auto_claim_observed=False,
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
            union_with_prior_scope_unit_ids=prior_unit_ids or None,
        )
        reallocation = {
            "status": allocation.get("status"),
            "released_lease_id": old_lease_id,
            "new_lease_id": allocation.get("lease_id"),
        }
        if allocation.get("status") == "allocated" and prior_scope_path.is_file():
            new_tracker_scope = prepare_review_run.load_tracker_scope(prior_scope_path)
            inputs_tracker_scope = artifacts_dir / "inputs" / "tracker-scope.json"
            if inputs_tracker_scope.parent.is_dir():
                shutil.copyfile(prior_scope_path, inputs_tracker_scope)
        elif allocation.get("status") == "allocated" and isinstance(allocation.get("lease_id"), str):
            # 报 allocated 却没落下 scope 文件：按重分配失败处理，新 lease 不留孤儿（contract 不会引用它）。
            reviewable_unit_diff_tracker.lease_release(
                repo=str(repo), lease_id=allocation["lease_id"], reason="agent_explicit_claim_reallocation_scope_file_missing"
            )
            reallocation["released_new_lease_id_because_scope_file_missing"] = allocation["lease_id"]

    # session-manifest：把 tracker scope 拼进去（与 prepare 同口径），供重渲 packet。
    manifest_artifact_path = artifacts.get("session_manifest") or contract.get("session_manifest_path")
    if manifest is not None and isinstance(manifest_artifact_path, str) and manifest_artifact_path:
        if new_tracker_scope is not None:
            manifest.setdefault("tracker", {})
            if isinstance(manifest["tracker"], dict):
                manifest["tracker"]["tracker_scope"] = new_tracker_scope
        for destination in {Path(manifest_artifact_path), artifacts_dir / "inputs" / "session-manifest.json"}:
            if destination.parent.is_dir():
                _write_json(destination, manifest)

    # scope contract。口径：有 lease 的 run 里，reviewer 按 primary_units 审，只有真正进了 tracker scope 的路径
    # 才算正式覆盖（进 fix_allowlist / primary_files）；没进的保持未覆盖，dispatch 覆盖闸照拦。没有 lease 的
    # run（本来就没有，或重分配失败后已清空 lease 字段）按文件定范围，认领路径照常进 fix_allowlist。
    if new_tracker_scope is not None:
        tracker_lease_state_after_claim = TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_NEW_LEASE_ALLOCATED
        new_primary = sorted(dict.fromkeys(str(path) for path in new_tracker_scope.get("paths") or []))
        contract["primary_units"] = sorted(set(new_tracker_scope["unit_ids"]))
        contract["tracker_lease_id"] = new_tracker_scope["lease_id"]
        contract["tracker_scope_hash"] = new_tracker_scope["scope_hash"]
        if isinstance(new_tracker_scope.get("transcript_max_line_number"), int):
            contract["tracker_transcript_max_line_number"] = new_tracker_scope["transcript_max_line_number"]
    elif reallocation_attempted_after_releasing_prior_lease:
        # 旧 lease 已释放、新 lease 没分配成：contract 不得再引用死 lease（下游 finalize 会拿它收尾），
        # 清成与 prepare 无 tracker scope 时同形的 None，本轮退回按文件定范围。
        tracker_lease_state_after_claim = (
            TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_REALLOCATION_FAILED_RUN_HAS_NO_LEASE
        )
        contract["primary_units"] = None
        contract["tracker_lease_id"] = None
        contract["tracker_scope_hash"] = None
        contract["tracker_transcript_max_line_number"] = None
        new_primary = sorted(dict.fromkeys([*old_primary, *accepted]))
    elif old_lease_id:
        # 跳过了重分配，旧 lease 仍在：既有单元照常审、照常标 reviewed，认领路径进不了这个 lease。
        tracker_lease_state_after_claim = TRACKER_LEASE_STATE_AFTER_CLAIM_REALLOCATION_SKIPPED_PRIOR_LEASE_KEPT
        new_primary = sorted(dict.fromkeys(old_primary))
    else:
        tracker_lease_state_after_claim = TRACKER_LEASE_STATE_AFTER_CLAIM_RUN_NEVER_HAD_TRACKER_LEASE
        new_primary = sorted(dict.fromkeys([*old_primary, *accepted]))
    new_primary_set = set(new_primary)
    claimed_paths_admitted_into_formal_review_scope = [path for path in accepted if path in new_primary_set]
    claimed_paths_not_admitted_into_formal_review_scope = [path for path in accepted if path not in new_primary_set]
    new_background = sorted(path for path in dict.fromkeys([*old_background, *old_primary]) if path not in new_primary_set)
    contract["primary_files"] = new_primary
    contract["fix_allowlist"] = sorted(dict.fromkeys([*new_primary]))
    contract["background_files"] = new_background
    contract["protected_files"] = list(new_background)
    _recompute_scope_contract_hash(contract)
    _write_json(contract_path, contract)
    inputs_contract = artifacts_dir / "inputs" / "scope.contract.json"
    if inputs_contract.is_file() and inputs_contract.resolve() != contract_path.resolve():
        shutil.copyfile(contract_path, inputs_contract)

    rerender: dict[str, Any]
    try:
        rerender = prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        rerender = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    lease_after_claim = contract.get("tracker_lease_id") if isinstance(contract.get("tracker_lease_id"), str) else None
    return {
        "status": "claimed",
        "claimed_paths": accepted,
        "rejected_paths_not_in_candidates_or_dirty": rejected,
        "tracker_claim_record": claim_record,
        "tracker_reallocation": reallocation,
        "tracker_lease_state_after_claim": tracker_lease_state_after_claim,
        "tracker_lease_id_after_claim": lease_after_claim,
        "run_has_tracker_lease_after_claim": lease_after_claim is not None,
        "claimed_paths_admitted_into_formal_review_scope": claimed_paths_admitted_into_formal_review_scope,
        # 有 lease 的 run 里没能进 tracker scope 的认领路径（例如在别的会话的在途 lease 里，或缺 transcript
        # 跳过了重分配）：不并进正式名单、仍算未覆盖。
        "claimed_paths_not_admitted_into_formal_review_scope": claimed_paths_not_admitted_into_formal_review_scope,
        "scope_contract": str(contract_path),
        "scope_hash": contract.get("scope_hash"),
        "packet_rerender": rerender,
    }
