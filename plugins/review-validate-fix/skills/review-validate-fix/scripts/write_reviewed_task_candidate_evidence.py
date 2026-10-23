#!/usr/bin/env python3
"""Write completion-gated RVF evidence consumed by evidence-aware base handback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rvf_merge_dispositions  # noqa: E402
from validation_result_cache import (  # noqa: E402
    CONTENT_MANIFEST_ALGORITHM,
    candidate_content_manifest_sha256,
    canonical_json_bytes,
    clean_git_environment,
    validated_validation_record_reference,
    write_json_with_digest,
)


SCHEMA_NAME = "rvf-reviewed-task-candidate-evidence"
SCHEMA_VERSION = 1
EVIDENCE_RELATIVE_PATH = Path("rvf/evidence/reviewed-task-candidate-evidence.json")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")
REVIEW_KINDS = {"implementation", "base_drift_integration"}
REVIEW_CONCLUSIONS = {"no_issues", "fixes_applied_and_validated"}
# 四个 tip 任一为 None ⇒ scope contract 未 opt-in reviewed candidate evidence（CandidateEvidenceNotApplicable）。
SCOPE_CONTRACT_TIP_NAMES_REQUIRED_FOR_CANDIDATE_EVIDENCE_OPT_IN = (
    "comparison_base_tip",
    "reviewed_base_tip",
    "reviewed_task_tip",
    "candidate_task_tip",
)


class StaleReviewedTaskCandidateEvidence(ValueError):
    pass


class CandidateEvidenceNotApplicable(ValueError):
    pass


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=clean_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"git {arguments[0]} failed")
    return completed.stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """``git merge-base --is-ancestor`` 的布尔封装。

    **不能用上面的 ``git()``**：``--is-ancestor`` 正是靠**非零退出码**表达「不是祖先」这个合法
    答案，而 ``git()`` 见非零就 raise，会把「不是祖先」变成异常、拿不到布尔值。
    """
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        env=clean_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def validated_canonical_artifact(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular JSON file: {path}")
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(payload, dict) or encoded != canonical_json_bytes(payload):
        raise ValueError(f"{label} must use canonical JSON bytes")
    sidecar = Path(f"{path}.sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError(f"{label} digest sidecar is missing or unsafe")
    actual = hashlib.sha256(encoded).hexdigest()
    if sidecar.read_bytes() != f"{actual}\n".encode("ascii"):
        raise ValueError(f"{label} digest mismatch")
    return payload, actual


def validated_scope_contract(run_dir: Path, repo: Path) -> tuple[dict[str, Any], Path, str]:
    path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = read_json_object(path, "scope contract")
    canonical_scope = contract.get("canonical_scope")
    if not isinstance(canonical_scope, dict):
        raise ValueError("scope contract canonical_scope is missing")
    scope_digest = hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest()
    if contract.get("scope_hash") != scope_digest:
        raise ValueError("scope contract hash mismatch")
    if Path(str(contract.get("repo", ""))).resolve() != repo:
        raise ValueError("scope contract targets a different repository")
    return contract, path, hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def validate_scope_tips(repo: Path, contract: Mapping[str, Any], current_head: str) -> None:
    if contract.get("review_kind") not in {"implementation", "base_drift_integration"}:
        raise ValueError("scope contract review kind is invalid")
    tip_names = SCOPE_CONTRACT_TIP_NAMES_REQUIRED_FOR_CANDIDATE_EVIDENCE_OPT_IN
    if any(contract.get(name) is None for name in tip_names):
        raise CandidateEvidenceNotApplicable(
            "scope contract does not opt into reviewed candidate evidence"
        )
    for name in ("base_ref", "remote_name"):
        value = contract.get(name)
        if not isinstance(value, str) or not value.strip() or value.startswith("-"):
            raise ValueError(f"scope contract {name} is missing or invalid")
    for name in tip_names:
        tip = contract.get(name)
        if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tip):
            raise ValueError(f"scope contract {name} is missing or invalid")
        git(repo, "cat-file", "-e", f"{tip}^{{commit}}")
        if not is_ancestor(repo, tip, current_head):
            raise ValueError(f"current task HEAD does not descend from scope contract {name}")


def completed_review(run_dir: Path, handoff_text: str) -> tuple[int, str]:
    plan = read_json_object(run_dir / "artifacts/reviewers/reviewer-plan.json", "reviewer plan")
    reviewers = plan.get("reviewers")
    if plan.get("status") != "completed":
        raise ValueError("reviewer plan is not completed")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise ValueError("reviewer plan must contain exactly two reviewers")
    reviewer_ids: list[str] = []
    any_reviewer_reported_issues = False
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) or reviewer.get("returncode") != 0:
            raise ValueError("reviewer plan contains an incomplete reviewer")
        reviewer_id = reviewer.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id:
            raise ValueError("reviewer plan reviewer_id is invalid")
        reviewer_ids.append(reviewer_id)
        result = read_json_object(
            run_dir / "artifacts/reviewers" / reviewer_id / "review-result.json",
            f"review result {reviewer_id}",
        )
        result_issues = result.get("issues")
        if (
            result.get("schema_version") != 1
            or result.get("kind") not in {"no_issues", "issues"}
            or not isinstance(result_issues, list)
            or result.get("requests") != []
            or (result.get("kind") == "no_issues" and result_issues)
            or (result.get("kind") == "issues" and not result_issues)
        ):
            raise ValueError(f"review result {reviewer_id} is not protocol-valid")
        any_reviewer_reported_issues = any_reviewer_reported_issues or bool(result_issues)
        for issue in result_issues:
            if not isinstance(issue, dict):
                raise ValueError(f"review result {reviewer_id} has an invalid issue")
    if len(set(reviewer_ids)) != 2:
        raise ValueError("reviewer plan reviewer ids must be distinct")
    if not re.search(r"handoff_status:\s*COMPLETED", handoff_text, re.IGNORECASE) or not re.search(
        r"review_status:\s*COMPLETED", handoff_text, re.IGNORECASE
    ):
        raise ValueError("handoff and review must both be completed")
    if re.search(r"\[(?:elevated|failed)\]", handoff_text, re.IGNORECASE):
        raise ValueError("handoff contains unresolved elevated or failed outcomes")
    # 「转 backlog」段可选：范围外问题照常被采纳成 fix-issue，到 validate-fix 阶段只验证不修、
    # 属实即提交 Kanban backlog——它是已完成的结论（不是未解决），计入 canonical 总数。
    summary_match = re.search(
        r"共\s*(\d+)\s*条\s*[，,]\s*(\d+)\s*真实修复\s*/\s*(\d+)\s*误报\s*"
        r"(?:/\s*(\d+)\s*转\s*backlog\s*)?/\s*(\d+)\s*升级",
        handoff_text,
        re.IGNORECASE,
    )
    if summary_match is None:
        raise ValueError("handoff canonical issue summary is missing")
    canonical_count, fixed_count, false_positive_count = map(int, summary_match.groups()[:3])
    submitted_to_kanban_backlog_count = int(summary_match.group(4) or 0)
    elevated_count = int(summary_match.group(5))
    fixed_outcomes = len(re.findall(r"\[fixed\]", handoff_text, re.IGNORECASE))
    false_positive_outcomes = len(re.findall(r"\[false_positive\]", handoff_text, re.IGNORECASE))
    submitted_to_kanban_backlog_outcomes = len(
        re.findall(r"\[submitted_to_kanban_backlog\]", handoff_text, re.IGNORECASE)
    )
    if (
        elevated_count != 0
        or fixed_count + false_positive_count + submitted_to_kanban_backlog_count != canonical_count
        or fixed_outcomes != fixed_count
        or false_positive_outcomes != false_positive_count
        or submitted_to_kanban_backlog_outcomes != submitted_to_kanban_backlog_count
    ):
        raise ValueError("handoff canonical issue summary does not match completed outcomes")
    # reviewer 报过 issue（或主 agent 登记过非 reviewer 来源的 fix-issue）时，合并处置是 canonical 计数的
    # 唯一事实源：每条 reviewer issue 都有处置、每条 fix-issue 都有来源且都已 upsert。全部 dropped 时
    # 它覆盖 0 条 fix-issue，canonical 计数 0 即合法——不能再用「reviewer 报过 issue ⇒ 计数 > 0」判，
    # 那会让按 Merge Rule 6 全部过滤掉的 run 永远写不出 evidence、无法 land。
    if any_reviewer_reported_issues or rvf_merge_dispositions.merge_dispositions_path(run_dir).exists():
        dispositions = rvf_merge_dispositions.require_passing_merge_dispositions(
            run_dir, require_fix_issue_cross_reference=True
        )
        accounted_fix_issue_count = len(dispositions.accounted_fix_issue_ids)
        if canonical_count != accounted_fix_issue_count:
            raise ValueError(
                f"handoff canonical issue count {canonical_count} disagrees with merge dispositions "
                f"({accounted_fix_issue_count} fix-issues)"
            )
    elif canonical_count > 0:
        raise ValueError("handoff canonical issue count disagrees with reviewer results")
    return canonical_count, "no_issues" if canonical_count == 0 else "fixes_applied_and_validated"


def merge_dispositions_errors_that_would_refuse_candidate_evidence(repo: Path, run_dir: Path) -> tuple[str, ...]:
    """``rvf_land commit`` 在 git commit **之前**调用：提前跑 ``completed_review`` 的处置强校验。

    否则缺失/过期的处置要到提交之后才被 evidence 拒绝（``refused_fail_closed``），而提交不回滚。
    只在 evidence 真会走到处置分支时判定，判据与 ``write_evidence`` 同源：scope contract 指向本 worktree
    （跨 worktree 模式下 contract 指向另一个 worktree，evidence 本就无从写起）、四个 tip 齐全（opt-in），
    且 reviewer 报过 issue 或处置工件已存在。其余情形返回空——那里 evidence 根本不校验处置，提前拦截
    只会凭空多一道闸（未 opt-in 的 run、两腿都 no_issues 且没有处置工件的 run 因此照常 land；T4 之前起、reviewer 报过 issue 却没写处置的在途 run 会在这里被拦下，需先补 `rvf merge write`）。"""
    try:
        contract = read_json_object(run_dir / "artifacts/inputs/scope.contract.json", "scope contract")
    except ValueError:
        return ()
    if Path(str(contract.get("repo", ""))).resolve() != repo.resolve() or any(
        contract.get(name) is None for name in SCOPE_CONTRACT_TIP_NAMES_REQUIRED_FOR_CANDIDATE_EVIDENCE_OPT_IN
    ):
        return ()
    try:
        any_reviewer_reported_issues = any(
            leg.issues for leg in rvf_merge_dispositions.read_canonical_reviewer_leg_results(run_dir)
        )
    except ValueError:
        # reviewer plan 读不出时 evidence 会以别的理由拒绝；此处只在处置工件已存在时继续判它。
        any_reviewer_reported_issues = False
    if not any_reviewer_reported_issues and not rvf_merge_dispositions.merge_dispositions_path(run_dir).exists():
        return ()
    return rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True).errors


def validated_validation_records(
    run_dir: Path, candidate_digest: str, common_dir: Path
) -> list[dict[str, Any]]:
    index, _index_digest = validated_canonical_artifact(
        run_dir / "artifacts/validation-records.json", "validation record index"
    )
    references = index.get("validation_records")
    if not isinstance(references, list) or not references:
        raise ValueError("at least one passed validation record is required")
    validated: list[dict[str, Any]] = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("validation record reference must be an object")
        record, _record_digest = validated_validation_record_reference(
            reference, expected_git_common_directory=common_dir
        )
        required = dict(reference)
        if required["status"] != "passed" or required["exit_code"] != 0:
            raise ValueError("candidate evidence requires passed validation records")
        if required["candidate_content_manifest_sha256"] != candidate_digest:
            raise ValueError("validation record was produced for different candidate content")
        validated.append(required)
    return validated


def optional_receipt(
    path_value: Any, digest_value: Any, label: str, *, required_path: Path
) -> tuple[str | None, str | None]:
    if path_value is None and digest_value is None:
        return None, None
    if not isinstance(path_value, str) or not isinstance(digest_value, str):
        raise ValueError(f"{label} path and digest must both be present")
    path = Path(path_value)
    if not path.is_absolute() or path.absolute() != required_path.absolute():
        raise ValueError(f"{label} must use the task git-dir receipt path")
    _payload, actual_digest = validated_canonical_artifact(path, label)
    if actual_digest != digest_value:
        raise ValueError(f"{label} contract digest mismatch")
    return str(path), actual_digest




def expected_parent_evidence_receipt(
    repo: Path, contract: Mapping[str, Any], evidence_path: Path
) -> tuple[str | None, str | None]:
    path_value = contract.get("candidate_evidence_path")
    digest_value = contract.get("candidate_evidence_digest")
    if path_value is None and digest_value is None:
        return None, None
    if not isinstance(path_value, str) or not isinstance(digest_value, str):
        raise ValueError("candidate evidence parent path and digest must both be present")
    configured_path = Path(path_value)
    allowed_current = configured_path.is_absolute() and configured_path.absolute() == evidence_path.absolute()
    allowed_immutable = (
        configured_path.is_absolute()
        and configured_path.parent.absolute() == (evidence_path.parent / "immutable").absolute()
        and configured_path.name == f"{digest_value}-reviewed-task-candidate-evidence.json"
    )
    if not (allowed_current or allowed_immutable):
        raise ValueError("candidate evidence parent escaped the task git-dir evidence directory")
    if configured_path.is_symlink():
        raise ValueError("candidate evidence parent must not be a symlink")
    if configured_path.is_file():
        _payload, _validated_path, actual_digest = validated_reviewed_task_candidate_evidence(
            repo, configured_path, require_current_subject=False
        )
        if actual_digest == digest_value:
            return str(configured_path), actual_digest
    if configured_path.absolute() != evidence_path.absolute():
        raise ValueError("candidate evidence parent digest mismatch")
    immutable_path = (
        evidence_path.parent
        / "immutable"
        / f"{digest_value}-reviewed-task-candidate-evidence.json"
    )
    _payload, _validated_path, actual_digest = validated_reviewed_task_candidate_evidence(
        repo, immutable_path, require_current_subject=False
    )
    if actual_digest != digest_value:
        raise ValueError("archived candidate evidence parent digest mismatch")
    return str(immutable_path), actual_digest


# 落地 delta 收敛判据的 scope 全集（08-19 处方 §四·步骤 1 第三条：四个集合的并集，不是两个）。
REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES = (
    "primary_files",
    "fix_allowlist",
    "task_change_paths",
    "background_files",
)

# 跨仓逐字一致的错误标识串（my-ai-setup 侧 message 也包含它们，便于跨仓 grep 同一根线）。
LANDED_TIP_ANCESTRY_ERROR = "landed tip does not descend from the reviewed tip"
LANDED_DELTA_ESCAPED_SCOPE_ERROR = "landed delta escaped the reviewed scope"


def reviewed_scope_path_universe(contract: Mapping[str, Any]) -> set[str]:
    universe: set[str] = set()
    for field_name in REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES:
        values = contract.get(field_name)
        if isinstance(values, list):
            universe.update(value for value in values if isinstance(value, str) and value)
    return universe


def landed_delta_paths(repo: Path, reviewed_tip: str, landed_tip: str) -> list[str]:
    """落地 delta 的路径口径：``--no-renames`` + ``-z``，repo root 相对，去重后排序。

    **必须带 ``--no-renames``**（与 receipt 事实字段那三个数组的口径**有意不同**）：开启改名检测时
    ``--name-only`` 对一次 rename **只打印目的路径**、源路径消失，于是「把文件从 scope 内改名到
    scope 外」会整个逃过收敛判据。加上它之后源与目的都出现，两端都必须在 scope 内。
    本仓已有同向先例：``tests/test_rvf_handoff_intake.py`` 的
    ``test_handoff_intake_treats_rename_old_and_new_paths_as_scoped``。
    """
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", reviewed_tip, landed_tip, "--"],
        cwd=repo,
        env=clean_git_environment(),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError(
            completed.stderr.decode(errors="replace").strip() or "git diff failed"
        )
    return sorted({os.fsdecode(path) for path in completed.stdout.split(b"\0") if path})


def assert_landed_from_reviewed_tip(
    repo: Path,
    *,
    reviewed_tip: str,
    landed_tip: str,
    scope_paths: set[str],
    label: str,
) -> list[str]:
    """``candidate_task_tip`` 家族的统一判据（跨仓 2026-08-26 定稿，三个承重点共用）。

    S1 幂等：两 tip 相等直接通过，落地 delta 为空。
    S2 祖先性：``reviewed_tip`` 必须是 ``landed_tip`` 的祖先。
    S3 收敛：落地 delta 必须 ⊆ reviewed scope 全集。

    **S2 与 S3 的先后不可交换**：非祖先时 ``git diff`` 仍会给出一个「对称差」式结果，先跑 S3 会把
    「task 被 rewind」误报成「delta 越界」，诊断串就对不上了。

    为什么不能只用等号：``candidate_task_tip`` 按定义就是 prepare/handback 那一刻的 tip，而 land
    只要提交了任何东西 HEAD 就前移——等号对「有提交」的轮次结构性不可满足。但光放开等号又会漏掉
    「rvf-land 的最小修正可以落地 reviewer 从没看过的字节」这个洞（该洞今天就存在，只是被死锁掩着），
    所以 S3 是放开 S2 的**配套闸**，不是可选项。
    """
    if reviewed_tip == landed_tip:
        return []
    if not is_ancestor(repo, reviewed_tip, landed_tip):
        raise ValueError(f"{label}: {LANDED_TIP_ANCESTRY_ERROR} ({reviewed_tip} -> {landed_tip})")
    paths = landed_delta_paths(repo, reviewed_tip, landed_tip)
    escaped = sorted(path for path in paths if path not in scope_paths)
    if escaped:
        raise ValueError(
            f"{label}: {LANDED_DELTA_ESCAPED_SCOPE_ERROR}: " + ", ".join(escaped)
        )
    return paths


def restamp_scope_contract_for_landing(
    repo: Path,
    run_dir: Path,
    contract: Mapping[str, Any],
    contract_path: Path,
    contract_digest: str,
    *,
    landed_tip: str,
    run_id: str | None,
) -> str:
    """把 ``candidate_task_tip`` 刷成落地后的 HEAD，并留下可追溯的 provenance。返回 restamp 前的 tip。

    **为什么要改一份已被评审、且被哈希覆盖的契约**：消费侧（my-ai-setup ``verify_scope_contract_
    identity``）要求 ``contract.candidate_task_tip == evidence.subject.head_oid``。而 prepare 在
    未提交工作的 implementation review 里，把「未来那个 candidate commit 的**父提交**」填进了
    ``candidate_task_tip``——是「把 candidate 的基座当成了 candidate」。land 提交之后两值必不等，
    该快路径对「有提交」的轮次结构性不可用。

    **诚实性靠三件事补回来**（缺一不可）：
      1. 逐字冻结被评审的那份契约到 ``artifacts/inputs/scope.contract.reviewed.json``（首次为准，
         不覆盖），其 sha256 就是记录里的 ``reviewed_contract_sha256``；
      2. ``landing_restamp`` provenance 带 ``restamped_at``，重复 restamp **append 成列表**、不覆盖；
      3. evidence ``subject.reviewed_task_tip`` = restamp 之前的 tip。

    **残留的不诚实（必须知情）**：tracker 与 finalize 记录里的 scope_hash 是评审当时的值，restamp 后
    与 contract 文件不再相等。``landing_restamp[].reviewed_scope_hash`` 是找回这条链接的**唯一凭据**，
    因此它不是可选字段。

    ⚠ ``landing_restamp`` 只放 contract 顶层，**绝不进 ``canonical_scope``**——否则会污染 scope_hash 语义。
    """
    reviewed_tip = str(contract.get("candidate_task_tip"))
    if reviewed_tip == landed_tip:
        return reviewed_tip

    delta_paths = assert_landed_from_reviewed_tip(
        repo,
        reviewed_tip=reviewed_tip,
        landed_tip=landed_tip,
        scope_paths=reviewed_scope_path_universe(contract),
        label="rvf-land contract restamp",
    )

    reviewed_contract_path = contract_path.parent / "scope.contract.reviewed.json"
    if not reviewed_contract_path.exists():
        # canonical 字节（而非原文件的 indent=2 字节）：这样它的 sha256 恰好等于 contract_digest，
        # 记录里的 reviewed_contract_sha256 才可被下游直接复算核对。
        write_json_with_digest(reviewed_contract_path, contract)

    prior_canonical_scope = contract.get("canonical_scope")
    canonical_scope = dict(prior_canonical_scope) if isinstance(prior_canonical_scope, Mapping) else {}
    canonical_scope["candidate_task_tip"] = landed_tip
    restamped_scope_hash = hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest()

    restamped_contract = dict(contract)
    restamped_contract["candidate_task_tip"] = landed_tip
    restamped_contract["canonical_scope"] = canonical_scope
    restamped_contract["scope_hash"] = restamped_scope_hash
    prior_restamps = restamped_contract.get("landing_restamp")
    restamp_history = list(prior_restamps) if isinstance(prior_restamps, list) else []
    restamp_history.append(
        {
            "reviewed_contract_path": str(reviewed_contract_path),
            "reviewed_contract_sha256": contract_digest,
            "reviewed_scope_hash": contract.get("scope_hash"),
            "reviewed_candidate_task_tip": reviewed_tip,
            "landed_candidate_task_tip": landed_tip,
            "landed_delta_paths": delta_paths,
            "restamped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
        }
    )
    restamped_contract["landing_restamp"] = restamp_history

    temporary_path = contract_path.with_name(f"{contract_path.name}.restamp-tmp")
    temporary_path.write_text(
        json.dumps(restamped_contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, contract_path)
    return reviewed_tip




def semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    comparable = dict(payload)
    comparable.pop("created_at", None)
    review = comparable.get("review")
    if isinstance(review, dict):
        comparable["review"] = {key: value for key, value in review.items() if key != "completed_at"}
    return comparable


def require_non_empty_string(payload: Mapping[str, Any], name: str, label: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reviewed task candidate evidence {label}.{name} is invalid")
    return value


def validate_iso8601_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reviewed task candidate evidence {label} is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"reviewed task candidate evidence {label} is invalid") from exc


def validate_linked_canonical_artifact(
    payload: Mapping[str, Any], path_name: str, digest_name: str, label: str
) -> tuple[Path | None, str | None]:
    path_value = payload.get(path_name)
    digest_value = payload.get(digest_name)
    if path_value is None and digest_value is None:
        return None, None
    if (
        not isinstance(path_value, str)
        or not Path(path_value).is_absolute()
        or not isinstance(digest_value, str)
        or not SHA256_PATTERN.fullmatch(digest_value)
    ):
        raise ValueError(f"reviewed task candidate evidence {label} path/digest pair is invalid")
    linked_path = Path(path_value)
    _linked_payload, actual_digest = validated_canonical_artifact(linked_path, label)
    if actual_digest != digest_value:
        raise ValueError(f"reviewed task candidate evidence {label} digest mismatch")
    return linked_path, digest_value


def validate_scope_identity(
    scope: Mapping[str, Any], review: Mapping[str, Any], subject: Mapping[str, Any]
) -> dict[str, Any]:
    contract_path_value = require_non_empty_string(scope, "contract_path", "scope")
    contract_path = Path(contract_path_value)
    contract_digest = require_non_empty_string(scope, "contract_sha256", "scope")
    scope_digest = require_non_empty_string(scope, "scope_digest", "scope")
    if (
        not contract_path.is_absolute()
        or not SHA256_PATTERN.fullmatch(contract_digest)
        or not SHA256_PATTERN.fullmatch(scope_digest)
        or contract_path.is_symlink()
        or not contract_path.is_file()
    ):
        raise ValueError("reviewed task candidate evidence scope identity is invalid")
    contract = read_json_object(contract_path, "candidate evidence scope contract")
    if hashlib.sha256(canonical_json_bytes(contract)).hexdigest() != contract_digest:
        raise ValueError("reviewed task candidate evidence scope contract digest mismatch")
    canonical_scope = contract.get("canonical_scope")
    if (
        not isinstance(canonical_scope, dict)
        or hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest() != scope_digest
        or contract.get("scope_hash") != scope_digest
        or contract.get("run_id") != review.get("run_id")
        or contract.get("base_ref") != subject.get("base_ref")
        or contract.get("remote_name") != subject.get("remote_name")
    ):
        raise ValueError("reviewed task candidate evidence scope contract identity mismatch")
    return contract


def validate_receipt_links(
    receipts: Mapping[str, Any],
    *,
    repo: Path,
    git_dir: Path,
    review_kind: str,
    subject: Mapping[str, Any],
    scope_paths: set[str],
) -> None:
    parent_path, parent_digest = validate_linked_canonical_artifact(
        receipts, "parent_evidence_path", "parent_evidence_sha256", "parent candidate evidence"
    )
    if review_kind == "base_drift_integration" and parent_path is None:
        raise ValueError("base drift integration evidence requires a parent candidate evidence")
    if parent_path is not None:
        expected_parent_directory = (git_dir / EVIDENCE_RELATIVE_PATH).parent / "immutable"
        if (
            parent_path.parent.absolute() != expected_parent_directory.absolute()
            or parent_path.name
            != f"{parent_digest}-reviewed-task-candidate-evidence.json"
        ):
            raise ValueError("reviewed task candidate parent evidence path is invalid")
    integration_path, _integration_digest = validate_linked_canonical_artifact(
        receipts,
        "integration_candidate_path",
        "integration_candidate_sha256",
        "handback integration candidate receipt",
    )
    if review_kind == "base_drift_integration" and integration_path is None:
        raise ValueError(
            "base drift integration evidence requires its handback integration receipt"
        )
    if integration_path is not None:
        expected_integration_path = (
            git_dir
            / "base-branch-sync"
            / "reviewed-task-handback-integration-candidate.json"
        )
        if integration_path.absolute() != expected_integration_path.absolute():
            raise ValueError("handback integration candidate receipt path is invalid")
        integration_receipt = read_json_object(
            integration_path, "handback integration candidate receipt"
        )
        if (
            integration_receipt.get("reviewed_candidate_evidence_sha256")
            != parent_digest
            or integration_receipt.get("base_tip_at_candidate")
            != subject.get("reviewed_base_tip")
            or integration_receipt.get("base_ref") != subject.get("base_ref")
            or integration_receipt.get("remote_name") != subject.get("remote_name")
        ):
            raise ValueError("handback integration receipt does not match candidate evidence")
        # 承重点 ②（跨仓 2026-08-26 定稿）：``candidate_task_tip == subject.head_oid`` 这条等号与
        # contract 侧那条同型同病——base_drift_integration 轮次只要在 land 里提交了任何修复，HEAD
        # 就前移、等号必然失配。改用统一谓词：祖先性 + 落地 delta 收敛。
        # my-ai-setup 侧 ``…-optimistic-handback.py:554`` 逐字实现同一套判据。
        integration_candidate_tip = integration_receipt.get("candidate_task_tip")
        landed_tip = subject.get("head_oid")
        if not isinstance(integration_candidate_tip, str) or not GIT_OBJECT_ID_PATTERN.fullmatch(
            integration_candidate_tip
        ):
            raise ValueError("handback integration receipt candidate task tip is invalid")
        if not isinstance(landed_tip, str) or not GIT_OBJECT_ID_PATTERN.fullmatch(landed_tip):
            raise ValueError("reviewed task candidate evidence head_oid is invalid")
        assert_landed_from_reviewed_tip(
            repo,
            reviewed_tip=integration_candidate_tip,
            landed_tip=landed_tip,
            scope_paths=scope_paths,
            label="handback integration candidate receipt",
        )


def validated_reviewed_task_candidate_evidence(
    repo_argument: Path,
    path: Path | None = None,
    *,
    require_current_subject: bool = True,
) -> tuple[dict[str, Any], Path, str]:
    repo = Path(git(repo_argument, "rev-parse", "--show-toplevel")).resolve(strict=True)
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    common_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)
    evidence_path = path or git_dir / EVIDENCE_RELATIVE_PATH
    payload, digest = validated_canonical_artifact(evidence_path, "reviewed task candidate evidence")
    subject = payload.get("subject")
    review = payload.get("review")
    scope = payload.get("scope")
    records = payload.get("validation_records")
    receipts = payload.get("receipts")
    if (
        payload.get("schema") != SCHEMA_NAME
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(subject, dict)
        or not isinstance(review, dict)
        or not isinstance(scope, dict)
        or not isinstance(records, list)
        or not records
        or not isinstance(receipts, dict)
        or not isinstance(payload.get("created_at"), str)
        or review.get("protocol_valid") is not True
    ):
        raise ValueError("reviewed task candidate evidence schema is incomplete")
    worktree_realpath = require_non_empty_string(subject, "worktree_realpath", "subject")
    common_dir_realpath = require_non_empty_string(subject, "git_common_dir_realpath", "subject")
    head_oid = require_non_empty_string(subject, "head_oid", "subject")
    tree_oid = require_non_empty_string(subject, "candidate_tree_oid", "subject")
    content_algorithm = require_non_empty_string(
        subject, "candidate_content_manifest_algorithm", "subject"
    )
    content_digest = require_non_empty_string(
        subject, "candidate_content_manifest_sha256", "subject"
    )
    comparison_base_tip = require_non_empty_string(subject, "comparison_base_tip", "subject")
    reviewed_base_tip = require_non_empty_string(subject, "reviewed_base_tip", "subject")
    base_ref = require_non_empty_string(subject, "base_ref", "subject")
    remote_name = require_non_empty_string(subject, "remote_name", "subject")
    if (
        not Path(worktree_realpath).is_absolute()
        or not Path(common_dir_realpath).is_absolute()
        or Path(worktree_realpath).resolve() != repo
        or Path(common_dir_realpath).resolve() != common_dir
        or not GIT_OBJECT_ID_PATTERN.fullmatch(head_oid)
        or not GIT_OBJECT_ID_PATTERN.fullmatch(tree_oid)
        or not GIT_OBJECT_ID_PATTERN.fullmatch(comparison_base_tip)
        or not GIT_OBJECT_ID_PATTERN.fullmatch(reviewed_base_tip)
        or content_algorithm != CONTENT_MANIFEST_ALGORITHM
        or not SHA256_PATTERN.fullmatch(content_digest)
        or base_ref.startswith("-")
        or remote_name.startswith("-")
    ):
        raise ValueError("reviewed task candidate evidence subject identity is invalid")
    # K5：``reviewed_task_tip``（restamp 之前的 candidate tip）是 evidence 自身的可追溯凭据。
    # 它可以缺席（restamp 机制之前写出的旧 evidence），但一旦出现就必须是合法 oid 且是 HEAD 的祖先
    # ——否则这份「reviewer 当时看的是哪个 tip」的声明就是假的。
    reviewed_task_tip = subject.get("reviewed_task_tip")
    if reviewed_task_tip is not None:
        if not isinstance(reviewed_task_tip, str) or not GIT_OBJECT_ID_PATTERN.fullmatch(
            reviewed_task_tip
        ):
            raise ValueError("reviewed task candidate evidence reviewed_task_tip is invalid")
        git(repo, "cat-file", "-e", f"{reviewed_task_tip}^{{commit}}")
        if not is_ancestor(repo, reviewed_task_tip, head_oid):
            raise ValueError(
                "reviewed task candidate evidence head_oid does not descend from reviewed_task_tip"
            )
    for tip in (head_oid, comparison_base_tip, reviewed_base_tip):
        git(repo, "cat-file", "-e", f"{tip}^{{commit}}")
    git(repo, "cat-file", "-e", f"{tree_oid}^{{tree}}")

    run_id = require_non_empty_string(review, "run_id", "review")
    review_kind = require_non_empty_string(review, "review_kind", "review")
    conclusion = require_non_empty_string(review, "conclusion", "review")
    canonical_issue_count = review.get("canonical_issue_count")
    if (
        review_kind not in REVIEW_KINDS
        or conclusion not in REVIEW_CONCLUSIONS
        or not isinstance(canonical_issue_count, int)
        or isinstance(canonical_issue_count, bool)
        or canonical_issue_count < 0
        or (conclusion == "no_issues") != (canonical_issue_count == 0)
        or not run_id
    ):
        raise ValueError("reviewed task candidate evidence review identity is invalid")
    validate_iso8601_timestamp(review.get("completed_at"), "review.completed_at")
    validate_iso8601_timestamp(payload.get("created_at"), "created_at")
    evidence_contract = validate_scope_identity(scope, review, subject)
    validate_receipt_links(
        receipts,
        repo=repo,
        git_dir=git_dir,
        review_kind=review_kind,
        subject=subject,
        scope_paths=reviewed_scope_path_universe(evidence_contract),
    )
    for reference in records:
        if not isinstance(reference, dict):
            raise ValueError("reviewed task candidate validation reference is invalid")
        validated_validation_record_reference(
            reference, expected_git_common_directory=common_dir
        )
        if (
            reference.get("status") != "passed"
            or reference.get("exit_code") != 0
            or reference.get("candidate_content_manifest_sha256") != content_digest
        ):
            raise ValueError("reviewed task candidate validation reference is not reusable")
    if require_current_subject:
        current_content_digest = candidate_content_manifest_sha256(repo)
        if (
            head_oid != git(repo, "rev-parse", "HEAD")
            or tree_oid != git(repo, "rev-parse", "HEAD^{tree}")
            or content_digest != current_content_digest
        ):
            raise StaleReviewedTaskCandidateEvidence("reviewed task candidate evidence is stale")
    return payload, evidence_path, digest


def require_unchanged_clean_candidate(
    repo: Path, *, head_oid: str, tree_oid: str, content_digest: str
) -> None:
    if (
        git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
        or git(repo, "rev-parse", "HEAD") != head_oid
        or git(repo, "rev-parse", "HEAD^{tree}") != tree_oid
        or candidate_content_manifest_sha256(repo) != content_digest
    ):
        raise ValueError("task candidate changed while reviewed evidence was being written")


def write_evidence(repo_argument: Path, run_dir: Path) -> dict[str, Any]:
    repo = Path(git(repo_argument, "rev-parse", "--show-toplevel")).resolve(strict=True)
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    common_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    if git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all"):
        raise ValueError("task worktree must be clean after the rvf-land task commit")
    candidate_head_oid = git(repo, "rev-parse", "HEAD")
    candidate_tree_oid = git(repo, "rev-parse", "HEAD^{tree}")
    contract, contract_path, contract_digest = validated_scope_contract(run_dir, repo)
    validate_scope_tips(repo, contract, candidate_head_oid)
    handoff_path = run_dir / "artifacts/handoff.md"
    if handoff_path.is_symlink() or not handoff_path.is_file():
        raise ValueError("completed handoff artifact is required")
    handoff_text = handoff_path.read_text(encoding="utf-8")
    issue_count, conclusion = completed_review(run_dir, handoff_text)
    candidate_digest = candidate_content_manifest_sha256(repo)
    validation_records = validated_validation_records(
        run_dir, candidate_digest, common_dir
    )
    # K5 的 restamp 是本函数里**唯一的写操作**，因此它必须排在**全部只读闸之后**：
    # worktree clean、scope tips、handoff 双 COMPLETED、以及每一条 validation record 都 passed
    # 且对应当前内容。08-19 处方原文即「restamp 只能发生在 finalize 与 reviewer 全部完成之后」。
    #
    # 曾经把它放在 `completed_review` 之前——那样一轮**未通过评审**的 run（例如双评审不成立、
    # handoff 仍是 PENDING）会先被改写 `scope.contract.json`、并落下一份 `scope.contract.reviewed.json`，
    # 然后才 fail-closed。那正是「不得留下半改的 contract」这条性质要禁止的形态，只是触发它的
    # 不是越界 delta 而是未完成的评审。
    reviewed_task_tip = restamp_scope_contract_for_landing(
        repo,
        run_dir,
        contract,
        contract_path,
        contract_digest,
        landed_tip=candidate_head_oid,
        run_id=contract.get("run_id"),
    )
    # 重新读一遍：contract_digest / scope_hash 必须来自 restamp **之后**的文件，否则 evidence 会记下
    # 一份与盘上文件对不上的身份，消费侧四条等式立刻全挂。重读顺带复验 scope_hash 自洽。
    contract, contract_path, contract_digest = validated_scope_contract(run_dir, repo)
    validate_scope_tips(repo, contract, candidate_head_oid)
    evidence_path = git_dir / EVIDENCE_RELATIVE_PATH
    if evidence_path.parent.resolve() != (git_dir / "rvf/evidence").resolve():
        raise ValueError("candidate evidence parent escaped the task git directory")
    integration_candidate_path, integration_candidate_digest = optional_receipt(
        contract.get("integration_candidate_receipt_path"),
        contract.get("integration_candidate_receipt_digest"),
        "handback integration candidate receipt",
        required_path=(
            git_dir
            / "base-branch-sync"
            / "reviewed-task-handback-integration-candidate.json"
        ),
    )
    now = datetime.now(timezone.utc).isoformat()
    existing: dict[str, Any] | None = None
    existing_digest: str | None = None
    if evidence_path.exists() or Path(f"{evidence_path}.sha256").exists():
        existing, _existing_path, existing_digest = validated_reviewed_task_candidate_evidence(
            repo, evidence_path, require_current_subject=False
        )
    expected_parent_path, expected_parent_digest = expected_parent_evidence_receipt(
        repo, contract, evidence_path
    )
    if expected_parent_digest is not None and existing is None:
        raise ValueError("candidate evidence parent requires an existing current evidence pointer")
    existing_is_expected_parent = bool(
        expected_parent_digest is not None and existing_digest == expected_parent_digest
    )
    if expected_parent_digest is not None and not existing_is_expected_parent:
        existing_receipts = existing.get("receipts") if existing is not None else None
        if (
            not isinstance(existing_receipts, dict)
            or existing_receipts.get("parent_evidence_sha256") != expected_parent_digest
        ):
            raise ValueError("current candidate evidence does not descend from the contracted parent")
    receipts = {
        "parent_evidence_path": expected_parent_path,
        "parent_evidence_sha256": expected_parent_digest,
        "integration_candidate_path": integration_candidate_path,
        "integration_candidate_sha256": integration_candidate_digest,
    }
    if existing is not None and expected_parent_digest is None:
        prior_receipts = existing.get("receipts")
        if isinstance(prior_receipts, dict):
            receipts["parent_evidence_path"] = prior_receipts.get("parent_evidence_path")
            receipts["parent_evidence_sha256"] = prior_receipts.get("parent_evidence_sha256")
    payload = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "subject": {
            "git_common_dir_realpath": str(common_dir),
            "worktree_realpath": str(repo),
            "head_oid": candidate_head_oid,
            # restamp 之前的 candidate tip：让 evidence 自身仍说得出「reviewer 当时看的是哪个 tip」。
            "reviewed_task_tip": reviewed_task_tip,
            "candidate_tree_oid": candidate_tree_oid,
            "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
            "candidate_content_manifest_sha256": candidate_digest,
            "comparison_base_tip": contract.get("comparison_base_tip"),
            "reviewed_base_tip": contract.get("reviewed_base_tip"),
            "base_ref": contract.get("base_ref"),
            "remote_name": contract.get("remote_name"),
        },
        "review": {
            "run_id": contract.get("run_id"),
            "review_kind": contract.get("review_kind", "implementation"),
            "conclusion": conclusion,
            "protocol_valid": True,
            "canonical_issue_count": issue_count,
            "completed_at": existing.get("review", {}).get("completed_at", now) if existing else now,
        },
        "scope": {
            "contract_path": str(contract_path),
            "contract_sha256": contract_digest,
            "scope_digest": contract.get("scope_hash"),
        },
        "validation_records": validation_records,
        "receipts": receipts,
        "created_at": existing.get("created_at", now) if existing else now,
    }
    require_unchanged_clean_candidate(
        repo,
        head_oid=candidate_head_oid,
        tree_oid=candidate_tree_oid,
        content_digest=candidate_digest,
    )
    if existing is not None and semantic_payload(existing) == semantic_payload(payload):
        return {"status": "reused", "candidate_evidence_path": str(evidence_path), "candidate_evidence_sha256": existing_digest}
    if expected_parent_digest is not None and not existing_is_expected_parent:
        raise ValueError("current candidate evidence differs from the contracted integration result")
    if existing is not None and existing_digest is not None:
        immutable_path = evidence_path.parent / "immutable" / f"{existing_digest}-reviewed-task-candidate-evidence.json"
        write_json_with_digest(immutable_path, existing)
        payload["receipts"]["parent_evidence_path"] = str(immutable_path)
        payload["receipts"]["parent_evidence_sha256"] = existing_digest
        payload["created_at"] = now
        payload["review"]["completed_at"] = now
    require_unchanged_clean_candidate(
        repo,
        head_oid=candidate_head_oid,
        tree_oid=candidate_tree_oid,
        content_digest=candidate_digest,
    )
    digest = write_json_with_digest(evidence_path, payload)
    return {"status": "written", "candidate_evidence_path": str(evidence_path), "candidate_evidence_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write completion-gated reviewed task candidate evidence.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--if-eligible",
        action="store_true",
        help="Exit successfully with skipped status for a non-opt-in/incomplete-tip contract.",
    )
    arguments = parser.parse_args()
    try:
        result = write_evidence(Path(arguments.repo).expanduser().resolve(), Path(arguments.run_dir).expanduser().resolve())
    except CandidateEvidenceNotApplicable as exc:
        if arguments.if_eligible:
            print(
                json.dumps(
                    {"status": "skipped_not_applicable", "reason": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
