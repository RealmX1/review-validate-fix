"""只读核算 reviewed candidate 的完成事实与已应用范围扩展。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import rvf_merge_dispositions
import check_review_result
from fix_attempt_scope_expansion_classification import (
    canonical_issue_authorized_fix_paths, classify_fix_attempt_scope_expansion, scope_contract_paths,
)
from review_scope_excluded_path_prefixes import BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE
from validation_result_cache import canonical_json_bytes, clean_git_environment


class CandidateQualificationRefused(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def read_regular_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"regular JSON artifact required: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def safe_relative_paths(values: Any) -> set[str]:
    if not isinstance(values, list):
        raise ValueError("scope paths must be an array")
    paths: set[str] = set()
    for entry in values:
        value = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("scope path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
            raise ValueError(f"scope path escaped the repository: {value}")
        paths.add(value)
    return paths


def completed_reviewer_legs(run_dir: Path) -> list[rvf_merge_dispositions.CanonicalReviewerLegResult]:
    plan = read_regular_json_object(run_dir / "artifacts/reviewers/reviewer-plan.json")
    if plan.get("schema_version") != 1:
        raise ValueError("reviewer plan schema is invalid")
    reviewers = plan.get("reviewers")
    mimic_fallback = (
        plan.get("routing_rule") == "R3"
        and plan.get("needs_last_resort_fallback") is True
        and reviewers == []
        and plan.get("status") in {"planned", "completed"}
    )
    if not mimic_fallback:
        if plan.get("status") != "completed":
            raise ValueError("reviewer plan is not completed")
        if not isinstance(reviewers, list) or len(reviewers) != 2:
            raise ValueError("reviewer plan must contain exactly two reviewers")
        if any(not isinstance(leg, dict) or type(leg.get("returncode")) is not int or leg["returncode"] != 0 for leg in reviewers):
            raise ValueError("reviewer plan contains an incomplete reviewer")
    legs = rvf_merge_dispositions.read_canonical_reviewer_leg_results(run_dir)
    ids = [leg.reviewer_id for leg in legs]
    if len(ids) != 2 or len(set(ids)) != 2:
        raise ValueError("reviewer plan reviewer ids must be distinct and contain exactly two reviewers")
    if mimic_fallback and set(ids) != {"codex-mimic-reviewer-a", "codex-mimic-reviewer-b"}:
        raise ValueError("R3 requires exactly two canonical mimic reviewer results")
    for leg in legs:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", leg.reviewer_id):
            raise ValueError("reviewer plan reviewer_id is invalid")
        if leg.review_result_path.parent.is_symlink():
            raise ValueError("reviewer result directory must not be a symlink")
        result = read_regular_json_object(leg.review_result_path)
        issues = result.get("issues")
        if (
            leg.read_error or result.get("schema_version") != 1
            or result.get("kind") not in {"no_issues", "issues"}
            or not isinstance(issues, list) or result.get("requests") != []
            or (result["kind"] == "no_issues" and issues)
            or (result["kind"] == "issues" and not issues)
            or any(not isinstance(issue, dict) for issue in issues)
        ):
            raise ValueError(f"review result {leg.reviewer_id} is not protocol-valid")
        classification = check_review_result.classify(leg.review_result_path, scope_contract=run_dir / "artifacts/inputs/scope.contract.json")
        if not classification["valid"]:
            raise ValueError(f"review result {leg.reviewer_id} is not protocol-valid: {classification['errors']}")
    return legs


def canonical_fix_attempt_facts(run_dir: Path, issue_ids: set[str]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    run_id = rvf_merge_dispositions.run_id_for_run_directory(run_dir)
    contract = read_regular_json_object(run_dir / "artifacts/inputs/scope.contract.json")
    attempts_by_issue: dict[str, list[tuple[datetime, dict[str, Any], dict[str, Any] | None]]] = {}
    directory = run_dir / "artifacts/fix-attempts"
    for child in sorted(directory.iterdir()) if directory.is_dir() else []:
        if child.is_symlink() or not child.is_dir():
            raise ValueError(f"unsafe fix attempt directory: {child}")
        attempt = read_regular_json_object(child / "attempt.json")
        issue_id = attempt.get("issue_id")
        if attempt.get("run_id") != run_id or issue_id not in issue_ids or attempt.get("attempt_id") != child.name:
            raise ValueError(f"fix attempt identity disagrees with canonical issues: {child.name}")
        if attempt.get("schema_version") != 1 or Path(str(attempt.get("repo", ""))).resolve() != Path(str(contract.get("repo", ""))).resolve():
            raise ValueError(f"fix attempt repository identity mismatch: {child.name}")
        try:
            created_at = datetime.fromisoformat(str(attempt["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                raise ValueError("timezone required")
        except (KeyError, ValueError) as exc:
            raise ValueError(f"fix attempt created_at is invalid: {child.name}") from exc
        result = read_regular_json_object(child / "result.json") if (child / "result.json").exists() else None
        if result is not None and any(result.get(key) != attempt.get(key) for key in ("run_id", "issue_id", "attempt_id")):
            raise ValueError(f"fix attempt/result identity mismatch: {child.name}")
        if result is not None and result.get("schema_version") != 1:
            raise ValueError(f"fix result schema is invalid: {child.name}")
        attempts_by_issue.setdefault(str(issue_id), []).append((created_at, attempt, result))
    counts: Counter[str] = Counter()
    applied: list[dict[str, Any]] = []
    for issue_id in sorted(issue_ids):
        mirror = read_regular_json_object(rvf_merge_dispositions.fix_issue_mirror_path(run_dir, issue_id))
        if mirror.get("issue_id") != issue_id or mirror.get("run_id") != run_id:
            raise ValueError(f"canonical fix-issue identity mismatch: {issue_id}")
        attempts = sorted(attempts_by_issue.get(issue_id, []), key=lambda item: item[0])
        if not attempts or len({item[0] for item in attempts}) != len(attempts):
            raise ValueError(f"latest fix attempt is missing or ambiguous: {issue_id}")
        _created_at, latest, result = attempts[-1]
        status = result.get("status") if result else None
        if status == "fixed" and latest.get("status") == "applied":
            counts["fixed"] += 1
        elif status == "false_positive" and latest.get("status") == status:
            counts["false_positive"] += 1
        elif status == "validated_and_submitted_to_kanban_backlog" and latest.get("status") == status:
            require_durable_backlog_submission(result, run_dir)
            counts["submitted_to_kanban_backlog"] += 1
        else:
            raise ValueError(f"latest fix attempt is unresolved or not applied: {issue_id} ({latest.get('status')}/{status})")
        for _stamp, attempt, attempt_result in attempts:
            if attempt.get("status") == "applied":
                if attempt_result is None or attempt_result.get("status") != "fixed":
                    raise ValueError(f"applied attempt has no fixed result: {attempt['attempt_id']}")
                applied.append({"issue": mirror, "attempt": attempt, "result": attempt_result})
    return {name: counts[name] for name in ("fixed", "false_positive", "submitted_to_kanban_backlog")}, applied


def require_durable_backlog_submission(result: dict[str, Any], run_dir: Path) -> None:
    receipt = result.get("kanban_backlog_submission")
    if not isinstance(receipt, dict) or any(receipt.get(key) != result[key] for key in ("issue_id", "attempt_id")):
        raise ValueError("backlog terminal outcome has no identity-bound submission receipt")
    groups = receipt.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("backlog terminal outcome has no durable submission receipt")
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("backlog submission group is invalid")
        outcome = group.get("outcome")
        if outcome == "created_kanban_backlog_task" and isinstance(group.get("kanban_task_id"), str) and group["kanban_task_id"].strip():
            continue
        if outcome == "pending_kanban_runtime_unavailable":
            pending_path = Path(str(group.get("pending_prompt_path", "")))
            fingerprints = {entry.get("finding_fingerprint") for entry in group.get("findings", []) if isinstance(entry, dict)}
            if pending_path.is_file() and not pending_path.is_symlink():
                pending = read_regular_json_object(pending_path)
                if (pending.get("run_dir") == str(run_dir) and fingerprints and None not in fingerprints
                    and fingerprints <= set(pending.get("finding_fingerprints", []))):
                    continue
            # 正式重试成功会删除旧 pending；用同一 finding 的持久建卡账本证明已完成，不改旧回执。
            entries = backlog_latest_submission_by_finding_fingerprint(receipt)
            if fingerprints and None not in fingerprints and all(
                durable_backlog_ledger_entry(entries.get(fingerprint)) for fingerprint in fingerprints
            ):
                continue
        if outcome == "nothing_new_to_submit":
            findings = group.get("findings")
            if isinstance(findings, list) and findings and all(
                isinstance(finding, dict) and finding.get("outcome") == "skipped_already_submitted_identical_finding"
                and finding.get("previous_kanban_task_id") for finding in findings
            ):
                continue
            latest_by_fingerprint = backlog_latest_submission_by_finding_fingerprint(receipt)
            if findings and all(isinstance(finding, dict) and finding.get("outcome") == "skipped_already_submitted_identical_finding"
                                and durable_backlog_ledger_entry(latest_by_fingerprint.get(finding.get("finding_fingerprint")))
                                for finding in findings):
                continue
        raise ValueError("backlog terminal outcome lacks a created task or durable pending intent")


def backlog_latest_submission_by_finding_fingerprint(receipt: dict[str, Any]) -> dict[str, Any]:
    ledger_path = Path(str(receipt.get("ledger_path", "")))
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise ValueError("backlog finding has no durable ledger")
    return {entry.get("finding_fingerprint"): entry for line in ledger_path.read_text().splitlines()
            if line.strip() for entry in [json.loads(line)] if isinstance(entry, dict)}


def durable_backlog_ledger_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("outcome") == "created_kanban_backlog_task":
        return bool(entry.get("kanban_task_id"))
    if entry.get("outcome") == "pending_kanban_runtime_unavailable":
        pending = Path(str(entry.get("pending_prompt_path", "")))
        return pending.is_file() and not pending.is_symlink()
    return False


def handoff_display_consistency_errors(handoff_text: str, counts: dict[str, int]) -> list[str]:
    errors: list[str] = []
    expected = {"total": sum(counts.values()), **counts, "elevated": 0}
    patterns = {
        "total": r"(?:共|合计)\s*(\d+)\s*条",
        "fixed": r"(\d+)\s*(?:条\s*)?真实修复|真实修复\s*(\d+)",
        "false_positive": r"(\d+)\s*(?:条\s*)?误报|误报\s*(\d+)",
        "submitted_to_kanban_backlog": r"(\d+)\s*(?:转\s*)?backlog",
        "elevated": r"(\d+)\s*(?:待用户决策的\s*)?升级|升级\s*(\d+)",
    }
    markers: Counter[str] = Counter()
    in_code_block = False
    for line in handoff_text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if re.match(r"^\s*(?:[-*]\s+)?(?:共|合计)\s*\d+\s*条", line):
            for name, pattern in patterns.items():
                match = re.search(pattern, line, re.IGNORECASE)
                if match and int(next(group for group in match.groups() if group is not None)) != expected[name]:
                    errors.append(f"handoff canonical issue summary does not match completed outcomes: {name}")
        marker = re.match(r"^\s*(?:[-*]\s+|\d+[.)]\s+)\*{0,2}\[(fixed|false_positive|submitted_to_kanban_backlog|elevated|failed)\]\*{0,2}(?:\s|$)", line, re.IGNORECASE)
        if marker:
            markers[marker.group(1).lower()] += 1
    if markers["elevated"] or markers["failed"]:
        errors.append("handoff contains unresolved elevated or failed outcomes")
    if markers and any(markers[name] != counts[name] for name in counts):
        errors.append("handoff canonical issue summary does not match completed outcomes")
    return errors


def completed_review_facts(run_dir: Path, handoff_text: str) -> dict[str, Any]:
    try:
        legs = completed_reviewer_legs(run_dir)
    except ValueError as exc:
        raise CandidateQualificationRefused("reviewer_completion_invalid", str(exc)) from exc
    if not all(re.search(rf"(?im)^\s*(?:-\s*)?{name}:\s*COMPLETED\s*$", handoff_text) for name in ("handoff_status", "review_status")):
        raise CandidateQualificationRefused("handoff_completion_invalid", "handoff and review must both be completed")
    issue_ids: set[str] = set()
    if any(leg.issues for leg in legs) or rvf_merge_dispositions.merge_dispositions_path(run_dir).exists() or any((run_dir / "artifacts/fix-issues").glob("*.json")):
        try:
            dispositions = rvf_merge_dispositions.require_passing_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
        except ValueError as exc:
            raise CandidateQualificationRefused("merge_dispositions_check_failed", str(exc)) from exc
        issue_ids = set(dispositions.accounted_fix_issue_ids)
    try:
        counts, applied = canonical_fix_attempt_facts(run_dir, issue_ids)
    except (ValueError, TypeError, KeyError) as exc:
        raise CandidateQualificationRefused("canonical_fix_completion_invalid", str(exc)) from exc
    errors = handoff_display_consistency_errors(handoff_text, counts)
    if errors:
        raise CandidateQualificationRefused("handoff_display_contradicts_machine_facts", "; ".join(errors))
    return {"canonical_issue_count": len(issue_ids), "outcome_counts": counts, "applied_attempts": applied,
            "conclusion": "fixes_applied_and_validated" if issue_ids else "no_issues"}


def actual_fix_patch_changed_paths(patch_bytes: bytes, repo: Path) -> set[str]:
    """逐段消费 Git 解析的实际改动，保留 rename 两端并排除 copy 来源及空操作。"""
    changed_paths: set[str] = set()
    for section in re.split(rb"(?m)(?=^diff --git )", patch_bytes):
        if not section.strip():
            continue
        if not section.startswith(b"diff --git "):
            raise ValueError("canonical issue 主路径的实际补丁缺少 Git diff 头")
        numstats: list[list[bytes]] = []
        for reverse in (False, True):
            parsed = subprocess.run(
                ["git", "apply", "--numstat", "-z", *(["--reverse"] if reverse else [])],
                input=section, cwd=repo, env=clean_git_environment(), capture_output=True, check=False,
            )
            entries = [entry.split(b"\t", 2) for entry in parsed.stdout.split(b"\0") if entry]
            if parsed.returncode or len(entries) != 1 or len(entries[0]) != 3:
                raise ValueError("canonical issue 主路径的实际补丁无法解析为单文件改动")
            numstats.append(entries[0])
        summary = subprocess.run(
            ["git", "apply", "--summary"], input=section, cwd=repo,
            env=clean_git_environment(), capture_output=True, check=False,
        )
        if summary.returncode:
            raise ValueError("canonical issue 主路径的实际补丁摘要无法解析")
        forward, reverse = numstats
        # 0/0 仍可表示空文件增删或真实 mode 变化；同路径自重命名自身不是改动。
        file_created_or_deleted = re.search(rb"(?m)^ (?:create|delete) mode [0-7]{6} ", summary.stdout)
        mode_change = re.search(rb"(?m)^ mode change ([0-7]{6}) => ([0-7]{6}) ", summary.stdout)
        mode_changed = mode_change is not None and mode_change[1] != mode_change[2]
        if (forward[:2] == [b"0", b"0"] and forward[2] == reverse[2]
            and not file_created_or_deleted and not mode_changed):
            continue
        changed_paths.add(os.fsdecode(forward[2]))
        # 来源必须由同一有效 diff 的反向解析证明，不能从任意 rename/copy 文本行提取。
        if not re.search(rb"(?m)^ copy ", summary.stdout):
            changed_paths.add(os.fsdecode(reverse[2]))
    return safe_relative_paths(sorted(changed_paths))


def applied_scope_expansion_facts(applied_attempts: list[dict[str, Any]], scope_paths: set[str], run_dir: Path,
                                *, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    expansions: list[dict[str, Any]] = []
    for facts in applied_attempts:
        attempt, result = facts["attempt"], facts["result"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(attempt.get("attempt_id", ""))):
            raise ValueError("scope expansion attempt_id is invalid")
        if facts["issue"].get("issue_id") != attempt.get("issue_id"):
            raise ValueError("scope expansion canonical issue identity mismatch")
        if attempt.get("status") != "applied" or result.get("status") != "fixed":
            raise ValueError("scope expansion requires a fixed and applied attempt")
        if any(attempt.get(key) != result.get(key) for key in ("run_id", "issue_id", "attempt_id")):
            raise ValueError("scope expansion attempt/result identity mismatch")
        if any(facts[name].get("run_id") != contract.get("run_id") for name in ("issue", "attempt", "result")):
            raise ValueError("scope expansion facts belong to a different run")
        if any(facts[name].get("fix_issue_handling_mode", "validate_then_fix") != "validate_then_fix"
               for name in ("issue", "attempt", "result")):
            raise ValueError("validate-only issue 不能生成已应用修复的范围扩展凭据")
        changed_paths = safe_relative_paths(result.get("changed_paths", []))
        metadata = result.get("scope_expansion") or {}
        if not isinstance(metadata, dict):
            raise ValueError("scope expansion metadata must be an object")
        if metadata != (attempt.get("scope_expansion") or {}):
            raise ValueError("attempt/result scope expansion mismatch")
        expanded = safe_relative_paths(metadata.get("expanded_paths", []))
        acknowledged = safe_relative_paths(metadata.get("acknowledged_protected_or_background_paths", []))
        declared = safe_relative_paths(metadata.get("declared_paths", []))
        confirmed = safe_relative_paths(attempt.get("confirmed_acknowledged_protected_or_background_paths", []))
        classification = classify_fix_attempt_scope_expansion(
            changed_paths=sorted(changed_paths),
            boundary_paths=[*safe_relative_paths(attempt.get("issue_paths", [])), *scope_contract_paths(contract, "fix_allowlist")],
            declared_paths=sorted(declared), acknowledged_protected_or_background_paths=sorted(acknowledged),
            reason=metadata.get("reason"), scope_contract=contract, canonical_issue=facts["issue"],
        )
        canonical_declared = set(classification["canonical_issue_declared_paths"])
        if canonical_declared and safe_relative_paths(attempt.get("issue_paths", [])) != set(
            canonical_issue_authorized_fix_paths(facts["issue"])
        ):
            raise ValueError("canonical issue 主路径与 prepare 时登记的路径不一致")
        if "canonical_issue_declared_paths" in metadata and safe_relative_paths(
            metadata["canonical_issue_declared_paths"]
        ) != canonical_declared:
            raise ValueError("canonical issue 主路径的声明来源不一致")
        guarded_changes = set(classification["acknowledged_protected_or_background_paths"]) | set(
            classification["unacknowledged_protected_or_background_paths"])
        if not guarded_changes <= acknowledged & confirmed:
            raise ValueError("protected/background changes lack main-agent confirmation")
        if any(metadata.get(name) for name in ("blocked_paths", "undeclared_paths", "unacknowledged_protected_or_background_paths")):
            raise ValueError("applied attempt has undeclared or blocked scope expansion")
        if not expanded <= declared | canonical_declared or not acknowledged <= confirmed or not (expanded | acknowledged) <= changed_paths:
            raise ValueError("scope expansion lacks declaration, applied patch, or main-agent confirmation")
        if (expanded or acknowledged or classification["requires_explicit_scope_expansion_reason"]) and not classification["reason"]:
            raise ValueError("scope expansion has no reason")
        outside = changed_paths - scope_paths
        # 确认通道只覆盖冻结 guarded 路径；普通外扩仍须声明。
        if (not outside <= expanded | canonical_declared | (acknowledged & guarded_changes)
            or classification["undeclared_paths"]):
            raise ValueError("applied patch has an undeclared path outside the reviewed scope")
        if classification["blocked_paths"]:
            raise ValueError("developer-tool-managed scope expansion is forbidden")
        qualified = outside | acknowledged
        if qualified:
            if any(path.startswith(prefix) for path in qualified for prefix in BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE):
                raise ValueError("developer-tool-managed scope expansion is forbidden")
            patch = Path(str(result.get("fix_patch_path", "")))
            expected_patch = run_dir / "artifacts/fix-attempts" / attempt["attempt_id"] / "fix.patch"
            # 祖先路径可以使用别名；补丁本身仍须是绑定 attempt 的普通文件。
            if (patch.is_symlink() or expected_patch.is_symlink() or not patch.is_file()
                or patch.resolve() != expected_patch.resolve()):
                raise ValueError("scope expansion requires its actual applied patch")
            patch_bytes = patch.read_bytes()
            if canonical_declared and not canonical_declared <= actual_fix_patch_changed_paths(patch_bytes, Path(contract["repo"])):
                raise ValueError("实际补丁未包含 canonical issue 声明的主路径")
            snapshots = {name: facts[name] for name in ("issue", "attempt", "result")}
            expansions.append({"paths": sorted(qualified), "issue_id": attempt["issue_id"], "attempt_id": attempt["attempt_id"],
                               "fix_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
                               "facts": snapshots, "facts_sha256": hashlib.sha256(canonical_json_bytes(snapshots)).hexdigest()})
    return expansions
