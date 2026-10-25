#!/usr/bin/env python3
"""Write completion-gated RVF evidence consumed by evidence-aware base handback."""

from __future__ import annotations

from independent_backup_route import guard_normal_mutation, tracked_subprocess_run

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
from fix_attempt_scope_expansion_classification import (  # noqa: E402
    REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES, reviewed_scope_path_universe,
)
from reviewed_candidate_completion_facts import (  # noqa: E402
    CandidateQualificationRefused,
    applied_scope_expansion_facts,
    completed_review_facts,
    safe_relative_paths,
)
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
APPLIED_SCOPE_EXPANSION_EVIDENCE_SCHEMA_VERSION = 2
APPLIED_SCOPE_EXPANSION_RECEIPT_SCHEMA = "rvf-applied-scope-expansion-qualification-receipt"
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


def candidate_dirty_paths(repo: Path) -> set[str]:
    from rvf_handoff_intake import repo_snapshot

    snapshot = repo_snapshot(repo)
    if snapshot.get("status_returncode") != 0:
        raise ValueError("cannot determine candidate worktree status")
    return {path for entry in snapshot["dirty_paths"] for path in (entry.get("path"), entry.get("old_path")) if path}


def git(repo: Path, *arguments: str) -> str:
    completed = tracked_subprocess_run(
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
    completed = tracked_subprocess_run(
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
    for name in (*REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES, "protected_files", "excluded_path_prefixes", "repo", "review_kind", "base_ref", "remote_name",
                 *SCOPE_CONTRACT_TIP_NAMES_REQUIRED_FOR_CANDIDATE_EVIDENCE_OPT_IN):
        if contract.get(name) != canonical_scope.get(name):
            raise ValueError(f"scope contract {name} disagrees with canonical_scope")
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
    facts = completed_review_facts(run_dir, handoff_text)
    return facts["canonical_issue_count"], facts["conclusion"]


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


# 跨仓逐字一致的错误标识串（my-ai-setup 侧 message 也包含它们，便于跨仓 grep 同一根线）。
LANDED_TIP_ANCESTRY_ERROR = "landed tip does not descend from the reviewed tip"
LANDED_DELTA_ESCAPED_SCOPE_ERROR = "landed delta escaped the reviewed scope"


def landed_delta_paths(repo: Path, reviewed_tip: str, landed_tip: str) -> list[str]:
    """落地 delta 的路径口径：``--no-renames`` + ``-z``，repo root 相对，去重后排序。

    **必须带 ``--no-renames``**（与 receipt 事实字段那三个数组的口径**有意不同**）：开启改名检测时
    ``--name-only`` 对一次 rename **只打印目的路径**、源路径消失，于是「把文件从 scope 内改名到
    scope 外」会整个逃过收敛判据。加上它之后源与目的都出现，两端都必须在 scope 内。
    本仓已有同向先例：``tests/test_rvf_handoff_intake.py`` 的
    ``test_handoff_intake_treats_rename_old_and_new_paths_as_scoped``。
    """
    completed = tracked_subprocess_run(
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


def project_scope_contract_for_landing(
    repo: Path, contract: Mapping[str, Any], contract_path: Path, contract_digest: str,
    *, landed_tip: str, scope_paths: set[str],
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """在内存中构造落地 contract；所有资格通过前不改写原件或归档。"""
    reviewed_tip = str(contract.get("candidate_task_tip"))
    history = contract.get("landing_restamp") or []
    reviewed_contract_path = contract_path.parent / "scope.contract.reviewed.json"
    if history:
        if not isinstance(history, list) or not isinstance(history[0], dict):
            raise ValueError("landing restamp history is invalid")
        frozen, frozen_digest = validated_canonical_artifact(reviewed_contract_path, "reviewed scope contract")
        if frozen_digest != history[0].get("reviewed_contract_sha256"):
            raise ValueError("reviewed scope contract provenance mismatch")
        original_reviewed_tip = str(frozen.get("candidate_task_tip"))
    else:
        original_reviewed_tip = reviewed_tip
        if reviewed_contract_path.exists() or reviewed_contract_path.is_symlink():
            frozen, frozen_digest = validated_canonical_artifact(reviewed_contract_path, "reviewed scope contract")
            if frozen_digest != contract_digest:
                raise ValueError("unbound reviewed scope contract archive already exists")
    assert_landed_from_reviewed_tip(repo, reviewed_tip=original_reviewed_tip, landed_tip=landed_tip,
                                   scope_paths=scope_paths, label="rvf-land contract restamp")
    if reviewed_tip == landed_tip:
        return dict(contract), original_reviewed_tip, None
    delta_paths = assert_landed_from_reviewed_tip(repo, reviewed_tip=reviewed_tip, landed_tip=landed_tip,
                                                scope_paths=scope_paths, label="rvf-land contract restamp")
    canonical_scope = {**contract["canonical_scope"], "candidate_task_tip": landed_tip}
    projected = {**contract, "candidate_task_tip": landed_tip, "canonical_scope": canonical_scope,
                 "scope_hash": hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest()}
    projected["landing_restamp"] = [*history, {
        "reviewed_contract_path": str(reviewed_contract_path), "reviewed_contract_sha256": contract_digest,
        "reviewed_scope_hash": contract.get("scope_hash"), "reviewed_candidate_task_tip": reviewed_tip,
        "landed_candidate_task_tip": landed_tip, "landed_delta_paths": delta_paths,
        "restamped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "run_id": contract.get("run_id"),
    }]
    return projected, original_reviewed_tip, None if history else dict(contract)



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
    validated_parent_receipt_projected_for_archive: tuple[Path, str] | None = None,
) -> None:
    parent_path, parent_digest = (
        validated_parent_receipt_projected_for_archive
        if validated_parent_receipt_projected_for_archive is not None else validate_linked_canonical_artifact(
            receipts, "parent_evidence_path", "parent_evidence_sha256", "parent candidate evidence"
        )
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
        or payload.get("schema_version") not in {SCHEMA_VERSION, APPLIED_SCOPE_EXPANSION_EVIDENCE_SCHEMA_VERSION}
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
    expansion_paths = validated_applied_scope_expansion_receipt(payload, evidence_contract)
    if reviewed_task_tip is not None:
        assert_landed_from_reviewed_tip(repo, reviewed_tip=reviewed_task_tip, landed_tip=head_oid,
            scope_paths=reviewed_scope_path_universe(evidence_contract) | expansion_paths, label="reviewed candidate evidence")
    validate_receipt_links(
        receipts,
        repo=repo,
        git_dir=git_dir,
        review_kind=review_kind,
        subject=subject,
        scope_paths=reviewed_scope_path_universe(evidence_contract) | expansion_paths,
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


def validated_applied_scope_expansion_receipt(payload: Mapping[str, Any], contract: Mapping[str, Any]) -> set[str]:
    scope, subject, review = payload["scope"], payload["subject"], payload["review"]
    path_value = scope.get("applied_scope_expansion_receipt_path")
    digest_value = scope.get("applied_scope_expansion_receipt_sha256")
    if payload["schema_version"] == SCHEMA_VERSION:
        if path_value is not None or digest_value is not None:
            raise ValueError("scope expansion requires evidence schema version 2")
        return set()
    if not isinstance(path_value, str) or not isinstance(digest_value, str) or not SHA256_PATTERN.fullmatch(digest_value):
        raise ValueError("scope expansion receipt path/digest is missing")
    run_dir = Path(scope["contract_path"]).parent.parent.parent
    path = Path(path_value)
    expected = run_dir / "artifacts/applied-scope-expansion-qualification-receipts" / f"{digest_value}.json"
    if path != expected or path.parent.is_symlink():
        raise ValueError("scope expansion receipt path escaped the run")
    receipt, digest = validated_canonical_artifact(path, "applied scope expansion receipt")
    frozen_contract = contract
    if contract.get("landing_restamp"):
        frozen_contract, _frozen_digest = validated_canonical_artifact(Path(scope["contract_path"]).parent / "scope.contract.reviewed.json", "reviewed scope contract")
    if (digest != digest_value or receipt.get("schema") != APPLIED_SCOPE_EXPANSION_RECEIPT_SCHEMA
        or receipt.get("schema_version") != 1 or receipt.get("run_id") != review["run_id"]
        or receipt.get("candidate_content_manifest_sha256") != subject["candidate_content_manifest_sha256"]
        or receipt.get("reviewed_scope_hash") != frozen_contract["scope_hash"]
        or receipt.get("reviewed_contract_sha256") != hashlib.sha256(canonical_json_bytes(frozen_contract)).hexdigest()):
        raise ValueError("scope expansion receipt identity mismatch")
    expansions = receipt.get("expansions")
    if not isinstance(expansions, list) or not expansions:
        raise ValueError("scope expansion receipt has no qualified paths")
    paths: set[str] = set()
    for expansion in expansions:
        facts = expansion.get("facts") if isinstance(expansion, dict) else None
        if (not isinstance(facts, dict) or not all(isinstance(facts.get(name), dict) for name in ("issue", "attempt", "result"))
            or expansion.get("facts_sha256") != hashlib.sha256(canonical_json_bytes(facts)).hexdigest()):
            raise ValueError("scope expansion facts digest mismatch")
        if any(facts[name].get("run_id") != review["run_id"] for name in ("issue", "attempt", "result")):
            raise ValueError("scope expansion facts belong to a different run")
        actual = applied_scope_expansion_facts([facts], reviewed_scope_path_universe(frozen_contract), run_dir, contract=frozen_contract)
        if actual != [expansion]:
            raise ValueError("scope expansion facts do not prove the recorded paths or patch")
        paths.update(safe_relative_paths(expansion["paths"]))
    return paths


def require_unchanged_clean_candidate(
    repo: Path, *, head_oid: str, tree_oid: str, content_digest: str
) -> None:
    if (
        candidate_dirty_paths(repo)
        or git(repo, "rev-parse", "HEAD") != head_oid
        or git(repo, "rev-parse", "HEAD^{tree}") != tree_oid
        or candidate_content_manifest_sha256(repo) != content_digest
    ):
        raise ValueError("task candidate changed while reviewed evidence was being written")


def prepare_candidate_evidence(
    repo_argument: Path, run_dir: Path, *, pending_stage_paths: set[str] | None = None,
) -> dict[str, Any]:
    """完整只读资格检查；pending_stage_paths 表示 commit 将清除的 dirty 路径。"""
    stage = "candidate_identity_invalid"
    try:
        artifact_hashes = candidate_qualification_artifact_hashes(run_dir)
        repo = Path(git(repo_argument, "rev-parse", "--show-toplevel")).resolve(strict=True)
        git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
        common_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        contract, contract_path, contract_digest = validated_scope_contract(run_dir, repo)
        candidate_head_oid = git(repo, "rev-parse", "HEAD")
        validate_scope_tips(repo, contract, candidate_head_oid)
        if contract.get("run_id") != rvf_merge_dispositions.run_id_for_run_directory(run_dir):
            raise ValueError("run summary and scope contract identities disagree")
        stage = "candidate_worktree_not_clean"
        dirty = candidate_dirty_paths(repo)
        if dirty - (pending_stage_paths or set()):
            raise ValueError("task worktree must be clean after the rvf-land task commit")
        stage = "handoff_completion_invalid"
        handoff_path = run_dir / "artifacts/handoff.md"
        if handoff_path.is_symlink() or not handoff_path.is_file():
            raise ValueError("completed handoff artifact is required")
        facts = completed_review_facts(run_dir, handoff_path.read_text(encoding="utf-8"))
        stage = "scope_expansion_invalid"
        original_scope_paths = reviewed_scope_path_universe(contract)
        expansions = applied_scope_expansion_facts(facts["applied_attempts"], original_scope_paths, run_dir, contract=contract)
        expansion_paths = {path for expansion in expansions for path in expansion["paths"]}
        scope_paths = original_scope_paths | expansion_paths
        stage = "landed_delta_escaped_reviewed_scope"
        projected_delta = set(landed_delta_paths(repo, str(contract["candidate_task_tip"]), candidate_head_oid)) | (pending_stage_paths or set())
        escaped = sorted(projected_delta - scope_paths)
        if escaped:
            raise ValueError(LANDED_DELTA_ESCAPED_SCOPE_ERROR + ": " + ", ".join(escaped))
        candidate_digest = candidate_content_manifest_sha256(repo)
        stage = "validation_records_invalid"
        validation_records = validated_validation_records(run_dir, candidate_digest, common_dir)
        evidence_path = git_dir / EVIDENCE_RELATIVE_PATH
        stage = "candidate_artifact_directory_invalid"
        for directory in (git_dir / "rvf", evidence_path.parent, evidence_path.parent / "immutable",
                          run_dir / "artifacts/applied-scope-expansion-qualification-receipts"):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise ValueError(f"candidate artifact directory is unsafe: {directory}")
        stage = "existing_candidate_evidence_invalid"
        existing = None
        existing_digest = None
        if evidence_path.exists() or Path(f"{evidence_path}.sha256").exists():
            existing, _existing_path, existing_digest = validated_reviewed_task_candidate_evidence(repo, evidence_path, require_current_subject=False)
        expected_parent_path, expected_parent_digest = expected_parent_evidence_receipt(repo, contract, evidence_path)
        if expected_parent_digest is not None and existing is None:
            raise ValueError("candidate evidence parent requires an existing current evidence pointer")
        existing_is_expected_parent = expected_parent_digest is not None and existing_digest == expected_parent_digest
        if expected_parent_digest is not None and not existing_is_expected_parent:
            if (existing or {}).get("receipts", {}).get("parent_evidence_sha256") != expected_parent_digest:
                raise ValueError("current candidate evidence does not descend from the contracted parent")
        stage = "candidate_receipt_invalid"
        integration_path, integration_digest = optional_receipt(
            contract.get("integration_candidate_receipt_path"), contract.get("integration_candidate_receipt_digest"),
            "handback integration candidate receipt", required_path=git_dir / "base-branch-sync/reviewed-task-handback-integration-candidate.json",
        )
        receipts = {"parent_evidence_path": expected_parent_path, "parent_evidence_sha256": expected_parent_digest,
                    "integration_candidate_path": integration_path, "integration_candidate_sha256": integration_digest}
        if existing is not None and expected_parent_digest is None:
            for name in ("parent_evidence_path", "parent_evidence_sha256"):
                receipts[name] = existing["receipts"].get(name)
        # receipt 的字段与 ancestry 校验在正式写入前完成；父工件已在上面验证。
        validate_receipt_links_for_candidate(repo, git_dir, contract, candidate_head_oid, scope_paths, receipts)
        stage = "scope_contract_landing_invalid"
        projected_contract, reviewed_task_tip, archive_contract = project_scope_contract_for_landing(
            repo, contract, contract_path, contract_digest, landed_tip=candidate_head_oid, scope_paths=scope_paths,
        )
        # 提交前 HEAD 尚未推进，仍须核对既有 evidence 绑定的实际内容。
        if (
            existing
            and existing.get("scope", {}).get("contract_path") == str(contract_path)
            and (
                projected_contract != contract
                or existing["subject"]["candidate_content_manifest_sha256"] != candidate_digest
            )
        ):
            raise ValueError("same-run candidate content changed after evidence: a fresh review contract is required")
        now = datetime.now(timezone.utc).isoformat()
        scope = {"contract_path": str(contract_path),
                 "contract_sha256": hashlib.sha256(canonical_json_bytes(projected_contract)).hexdigest(),
                 "scope_digest": projected_contract["scope_hash"]}
        expansion_receipt = None
        if expansions:
            frozen_contract = archive_contract or contract
            if contract.get("landing_restamp"):
                frozen_contract, _digest = validated_canonical_artifact(contract_path.parent / "scope.contract.reviewed.json", "reviewed scope contract")
            expansion_receipt = {"schema": APPLIED_SCOPE_EXPANSION_RECEIPT_SCHEMA, "schema_version": 1,
                                 "run_id": contract["run_id"], "reviewed_scope_hash": frozen_contract["scope_hash"],
                                 "reviewed_contract_sha256": hashlib.sha256(canonical_json_bytes(frozen_contract)).hexdigest(),
                                 "candidate_content_manifest_sha256": candidate_digest, "expansions": expansions}
            receipt_digest = hashlib.sha256(canonical_json_bytes(expansion_receipt)).hexdigest()
            scope.update({"applied_scope_expansion_receipt_path": str(run_dir / "artifacts/applied-scope-expansion-qualification-receipts" / f"{receipt_digest}.json"),
                          "applied_scope_expansion_receipt_sha256": receipt_digest})
        payload = {
            "schema": SCHEMA_NAME, "schema_version": APPLIED_SCOPE_EXPANSION_EVIDENCE_SCHEMA_VERSION if expansions else SCHEMA_VERSION,
            "subject": {"git_common_dir_realpath": str(common_dir), "worktree_realpath": str(repo),
                        "head_oid": candidate_head_oid, "reviewed_task_tip": reviewed_task_tip,
                        "candidate_tree_oid": git(repo, "rev-parse", "HEAD^{tree}"),
                        "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
                        "candidate_content_manifest_sha256": candidate_digest,
                        "comparison_base_tip": contract["comparison_base_tip"], "reviewed_base_tip": contract["reviewed_base_tip"],
                        "base_ref": contract["base_ref"], "remote_name": contract["remote_name"]},
            "review": {"run_id": contract["run_id"], "review_kind": contract["review_kind"], "conclusion": facts["conclusion"],
                       "protocol_valid": True, "canonical_issue_count": facts["canonical_issue_count"],
                       "completed_at": existing["review"]["completed_at"] if existing else now},
            "scope": scope, "validation_records": validation_records, "receipts": receipts,
            "created_at": existing["created_at"] if existing else now,
        }
        if existing and expected_parent_digest is not None and not existing_is_expected_parent and semantic_payload(existing) != semantic_payload(payload):
            raise ValueError("current candidate evidence differs from the contracted integration result")
        if candidate_qualification_artifact_hashes(run_dir) != artifact_hashes:
            raise CandidateQualificationRefused("qualification_artifacts_changed", "资格工件在检查期间发生变化；请重跑检查。")
        return {"repo": repo, "run_dir": run_dir, "git_dir": git_dir, "payload": payload, "contract_path": contract_path,
                "contract_before": contract, "projected_contract": projected_contract, "archive_contract": archive_contract,
                "existing": existing, "existing_digest": existing_digest, "evidence_path": evidence_path,
                "expansion_receipt": expansion_receipt, "facts": facts,
                "qualification_artifact_hashes": artifact_hashes}
    except (CandidateEvidenceNotApplicable, CandidateQualificationRefused):
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise CandidateQualificationRefused(stage, str(exc)) from exc


def candidate_qualification_artifact_hashes(run_dir: Path) -> dict[str, str]:
    """仅覆盖资格输入，不把 prepare/result 输出或 attempt worktree 纳入快照。"""
    paths = {run_dir / relative for relative in (
        "summary.json", "artifacts/handoff.md", "artifacts/inputs/scope.contract.json",
        "artifacts/inputs/scope.contract.reviewed.json", "artifacts/inputs/scope.contract.reviewed.json.sha256",
        "artifacts/reviewers/reviewer-plan.json", "artifacts/validation-records.json", "artifacts/validation-records.json.sha256",
        "artifacts/merge/merge_dispositions.json", "artifacts/merge/merge_dispositions.json.sha256",
    )}
    for pattern in ("artifacts/reviewers/*/review-result.json", "artifacts/fix-issues/*.json",
                    "artifacts/merge/fix-issue-source-refs-recorded-at-upsert/*.json",
                    "artifacts/fix-attempts/*/attempt.json", "artifacts/fix-attempts/*/result.json", "artifacts/fix-attempts/*/fix.patch"):
        paths.update(run_dir.glob(pattern))
    return {str(path): ("symlink:" + os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(paths) if path.exists() or path.is_symlink()}


def validate_receipt_links_for_candidate(repo: Path, git_dir: Path, contract: Mapping[str, Any], head: str,
                                         scope_paths: set[str], receipts: Mapping[str, Any]) -> None:
    """与 reader 共用全部 receipt 判据；只投影已经验证的 current 父指针归档路径。"""
    projected = dict(receipts)
    projected_parent = None
    current = git_dir / EVIDENCE_RELATIVE_PATH
    if projected.get("parent_evidence_path") == str(current):
        _parent, _path, digest = validated_reviewed_task_candidate_evidence(repo, current, require_current_subject=False)
        if digest != projected["parent_evidence_sha256"]:
            raise ValueError("candidate parent changed during qualification")
        destination = current.parent / "immutable" / f"{digest}-reviewed-task-candidate-evidence.json"
        projected["parent_evidence_path"] = str(destination)
        projected_parent = (destination, digest)
    validate_receipt_links(projected, repo=repo, git_dir=git_dir, review_kind=contract["review_kind"],
                           subject={"head_oid": head, "reviewed_base_tip": contract["reviewed_base_tip"],
                                    "base_ref": contract["base_ref"], "remote_name": contract["remote_name"]},
                           scope_paths=scope_paths, validated_parent_receipt_projected_for_archive=projected_parent)


def candidate_evidence_qualification(repo: Path, run_dir: Path, *, pending_stage_paths: set[str] | None = None) -> dict[str, Any]:
    try:
        prepared = prepare_candidate_evidence(repo, run_dir, pending_stage_paths=pending_stage_paths)
        return {"status": "eligible", "reason_code": "candidate_evidence_eligible",
                "canonical_issue_count": prepared["facts"]["canonical_issue_count"],
                "outcome_counts": prepared["facts"]["outcome_counts"],
                "candidate_content_manifest_sha256": prepared["payload"]["subject"]["candidate_content_manifest_sha256"],
                "evidence_schema_version": prepared["payload"]["schema_version"],
                "consumer_contract_update_required": prepared["expansion_receipt"] is not None,
                "recovered_canonical_issue_scope_expansion_paths": sorted({
                    path for expansion in (prepared["expansion_receipt"] or {}).get("expansions", [])
                    for path in expansion["paths"]
                    if path not in (expansion["facts"]["result"].get("scope_expansion") or {}).get("expanded_paths", [])
                    and path not in (expansion["facts"]["result"].get("scope_expansion") or {}).get(
                        "acknowledged_protected_or_background_paths", [])
                })}
    except CandidateEvidenceNotApplicable as exc:
        return {"status": "skipped_not_applicable", "reason_code": "candidate_evidence_not_opted_in", "reason": str(exc)}
    except CandidateQualificationRefused as exc:
        return {"status": "refused_fail_closed", "reason_code": exc.reason_code, "reason": str(exc)}


@guard_normal_mutation("repo_argument", "write-candidate-evidence", "run_dir")
def write_evidence(repo_argument: Path, run_dir: Path) -> dict[str, Any]:
    prepared = prepare_candidate_evidence(repo_argument, run_dir)
    repo, payload = prepared["repo"], prepared["payload"]
    existing, existing_digest = prepared["existing"], prepared["existing_digest"]
    evidence_path = prepared["evidence_path"]
    subject = payload["subject"]
    if candidate_qualification_artifact_hashes(run_dir) != prepared["qualification_artifact_hashes"]:
        raise CandidateQualificationRefused("qualification_artifacts_changed", "资格工件已变化；未写入 evidence 或 contract。")
    require_unchanged_clean_candidate(repo, head_oid=subject["head_oid"], tree_oid=subject["candidate_tree_oid"],
                                     content_digest=subject["candidate_content_manifest_sha256"])
    if existing is not None and semantic_payload(existing) == semantic_payload(payload):
        return {"status": "reused", "candidate_evidence_path": str(evidence_path), "candidate_evidence_sha256": existing_digest}
    if prepared["archive_contract"] is not None:
        write_json_with_digest(prepared["contract_path"].parent / "scope.contract.reviewed.json", prepared["archive_contract"])
    if prepared["projected_contract"] != prepared["contract_before"]:
        temporary = prepared["contract_path"].with_name("scope.contract.json.landing-write")
        temporary.write_bytes(canonical_json_bytes(prepared["projected_contract"]))
        os.replace(temporary, prepared["contract_path"])
    if prepared["expansion_receipt"] is not None:
        write_json_with_digest(Path(payload["scope"]["applied_scope_expansion_receipt_path"]), prepared["expansion_receipt"])
    if existing is not None:
        immutable_path = evidence_path.parent / "immutable" / f"{existing_digest}-reviewed-task-candidate-evidence.json"
        write_json_with_digest(immutable_path, existing)
        payload["receipts"]["parent_evidence_path"] = str(immutable_path)
        payload["receipts"]["parent_evidence_sha256"] = existing_digest
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        payload["review"]["completed_at"] = payload["created_at"]
    require_unchanged_clean_candidate(repo, head_oid=subject["head_oid"], tree_oid=subject["candidate_tree_oid"],
                                     content_digest=subject["candidate_content_manifest_sha256"])
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
