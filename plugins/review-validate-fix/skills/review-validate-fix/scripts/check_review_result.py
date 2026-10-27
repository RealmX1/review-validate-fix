#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from reviewer_request_progress_identity import reviewer_request_content_identity


SCHEMA_VERSION = 1
KINDS = {"no_issues", "issues", "request"}
REQUEST_TYPES = {
    "lock_request",
    "standard_request",
    "measurement_request",
    "subtask_request",
    "context_request",
}
STANDARD_DOMAINS = {"simplification", "security", "performance"}
SUBTASK_TYPES = {
    "read_only_investigation",
    "security_check",
    "performance_measurement",
    "simplification_probe",
}
CONTEXT_NEEDS = {"file", "manifest", "packet", "prior-output", "test-result"}
ISSUE_KINDS = {"REAL", "NIT", "ELEVATE"}
ISSUE_SEVERITIES = {"high", "medium", "low"}
# reviewer 对每条 issue 的「是否属于本轮范围」判定（缺省 = in）。outside 的 issue 照常通过
# review、照常被合并采纳，到 validate-fix 阶段只验证不修、由 validator 提交到 Kanban backlog。
ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE = "in_current_round_scope"
ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE = "outside_current_round_scope"
ISSUE_SCOPE_RELATIONS = {
    ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE,
    ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE,
}


def fail(errors: list[str], *, json_output: bool) -> int:
    payload = {
        "valid": False,
        "kind": "invalid",
        "issue_count": 0,
        "request_count": 0,
        "request_types": [],
        "errors": errors,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for error in errors:
            print(error, file=sys.stderr)
    return 1


def is_relative_repo_path(value: str) -> bool:
    if not value or value.strip() != value:
        return False
    path = Path(value)
    if path.is_absolute():
        return False
    if value in {".", ".."}:
        return False
    return ".." not in path.parts


def load_scope_contract(path: Path | None) -> dict[str, Any]:
    if path is None:
        env_path = os.environ.get("RVF_SCOPE_CONTRACT")
        path = Path(env_path) if env_path else None
    if path is None:
        return {}
    with path.expanduser().open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("scope contract root must be a JSON object")
    return payload


def is_excluded(path: str, prefixes: list[str]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    for prefix in prefixes:
        clean_prefix = str(prefix).replace("\\", "/").strip("/")
        if clean_prefix and (normalized == clean_prefix or normalized.startswith(clean_prefix + "/")):
            return True
    return False


def excluded_path_prefixes_from_contract(contract: dict[str, Any], errors: list[str]) -> list[str]:
    prefixes = contract.get("excluded_path_prefixes")
    if prefixes is None:
        canonical_scope = contract.get("canonical_scope", {})
        if isinstance(canonical_scope, dict):
            prefixes = canonical_scope.get("excluded_path_prefixes", [])
        else:
            errors.append("scope contract canonical_scope must be an object")
            prefixes = []
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        errors.append("scope contract excluded_path_prefixes must be a string array")
        return []
    return prefixes


def issue_scope_relation(issue: dict[str, Any]) -> str:
    """缺省按 in 处理：旧工件与没写该字段的 reviewer 维持「本轮范围内」语义。"""
    value = issue.get("scope_relation")
    return value if isinstance(value, str) and value else ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE


def validate_issue(
    issue: Any,
    index: int,
    errors: list[str],
    excluded_prefixes: list[str],
    warnings: list[str],
    issue_indices_in_excluded_paths_one_based: list[int],
) -> None:
    if not isinstance(issue, dict):
        errors.append(f"issues[{index}] must be an object")
        return
    path = issue.get("path")
    line = issue.get("line")
    message = issue.get("message")
    kind = issue.get("kind")
    severity = issue.get("severity")
    if not isinstance(path, str) or not is_relative_repo_path(path):
        errors.append(f"issues[{index}].path must be a relative repo path without '..'")
    elif is_excluded(path, excluded_prefixes):
        # 护栏而非闸门：一条落在排除区的 issue 不再让整条 reviewer 腿作废（那会触发换腿 /
        # 重派，代价远大于这一条 issue 本身）。只记 warning，合并时这类 issue 只能 dropped
        # （由 rvf_merge_dispositions 强制）。
        warnings.append(f"issues[{index}].path is excluded by scope contract; merge must drop it")
        issue_indices_in_excluded_paths_one_based.append(index + 1)
    scope_relation = issue.get("scope_relation")
    if scope_relation is not None and scope_relation not in ISSUE_SCOPE_RELATIONS:
        errors.append(
            f"issues[{index}].scope_relation must be one of: {', '.join(sorted(ISSUE_SCOPE_RELATIONS))}"
        )
    if not isinstance(line, int) or line < 1:
        errors.append(f"issues[{index}].line must be an integer >= 1")
    if not isinstance(message, str) or not message.strip():
        errors.append(f"issues[{index}].message must be a non-empty string")
    if not isinstance(kind, str) or kind not in ISSUE_KINDS:
        errors.append(
            f"issues[{index}].kind must be one of: {', '.join(sorted(ISSUE_KINDS))}"
        )
    if not isinstance(severity, str) or severity not in ISSUE_SEVERITIES:
        errors.append(
            f"issues[{index}].severity must be one of: {', '.join(sorted(ISSUE_SEVERITIES))}"
        )


def non_empty_string(request: dict[str, Any], field: str, errors: list[str], prefix: str) -> str | None:
    value = request.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{field} must be a non-empty string")
        return None
    return value


def validate_request(request: Any, index: int, errors: list[str]) -> str | None:
    prefix = f"requests[{index}]"
    if not isinstance(request, dict):
        errors.append(f"{prefix} must be an object")
        return None
    request_type = request.get("type")
    status = request.get("status", "pending")
    if status not in {"pending", "responded", "resolved"}:
        errors.append(f"{prefix}.status must be pending, responded or resolved")
    if status in {"responded", "resolved"}:
        non_empty_string(request, "response", errors, prefix)
    if status == "resolved":
        non_empty_string(request, "resolution", errors, prefix)
    if request_type not in REQUEST_TYPES:
        errors.append(f"{prefix}.type must be one of: {', '.join(sorted(REQUEST_TYPES))}")
        return None

    if request_type == "lock_request":
        for field in ("name", "command", "reason"):
            non_empty_string(request, field, errors, prefix)
    elif request_type == "standard_request":
        domain = non_empty_string(request, "domain", errors, prefix)
        non_empty_string(request, "scope", errors, prefix)
        non_empty_string(request, "reason", errors, prefix)
        if domain is not None and domain not in STANDARD_DOMAINS:
            errors.append(f"{prefix}.domain must be one of: {', '.join(sorted(STANDARD_DOMAINS))}")
    elif request_type == "measurement_request":
        for field in ("metric", "command", "reason"):
            non_empty_string(request, field, errors, prefix)
    elif request_type == "subtask_request":
        subtask_type = non_empty_string(request, "subtask_type", errors, prefix)
        non_empty_string(request, "scope", errors, prefix)
        non_empty_string(request, "reason", errors, prefix)
        if subtask_type is not None and subtask_type not in SUBTASK_TYPES:
            errors.append(f"{prefix}.subtask_type must be one of: {', '.join(sorted(SUBTASK_TYPES))}")
    elif request_type == "context_request":
        need = non_empty_string(request, "need", errors, prefix)
        non_empty_string(request, "reason", errors, prefix)
        if need is not None and need not in CONTEXT_NEEDS:
            errors.append(f"{prefix}.need must be one of: {', '.join(sorted(CONTEXT_NEEDS))}")
    return str(request_type)


def classify(path: Path, *, scope_contract: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {
            "valid": False,
            "kind": "invalid",
            "issue_count": 0,
            "request_count": 0,
            "request_types": [],
            "errors": [f"missing review result artifact: {path}"],
        }
    except json.JSONDecodeError as exc:
        return {
            "valid": False,
            "kind": "invalid",
            "issue_count": 0,
            "request_count": 0,
            "request_types": [],
            "errors": [f"invalid JSON: {exc}"],
        }

    if not isinstance(payload, dict):
        errors.append("result root must be a JSON object")
        payload = {}

    try:
        contract = load_scope_contract(scope_contract)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        contract = {}
        errors.append(f"invalid scope contract: {exc}")
    excluded_prefixes = excluded_path_prefixes_from_contract(contract, errors)

    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    kind = payload.get("kind")
    if kind not in KINDS:
        errors.append(f"kind must be one of: {', '.join(sorted(KINDS))}")
        kind = "invalid"

    issues = payload.get("issues", [])
    requests = payload.get("requests", [])
    if not isinstance(issues, list):
        errors.append("issues must be an array")
        issues = []
    if not isinstance(requests, list):
        errors.append("requests must be an array")
        requests = []

    warnings: list[str] = []
    issue_indices_in_excluded_paths_one_based: list[int] = []
    for index, issue in enumerate(issues):
        validate_issue(
            issue, index, errors, excluded_prefixes, warnings, issue_indices_in_excluded_paths_one_based
        )

    request_types: list[str] = []
    request_identities: set[str] = set()
    for index, request in enumerate(requests):
        if isinstance(request, dict):
            identity = reviewer_request_content_identity(request)
            if identity in request_identities:
                errors.append("重复的请求内容身份")
            request_identities.add(identity)
            request["request_id"] = identity
        request_type = validate_request(request, index, errors)
        if request_type is not None:
            request_types.append(request_type)

    pending_requests = [request for request in requests if isinstance(request, dict) and request.get("status", "pending") != "resolved"]

    if kind == "no_issues":
        if issues:
            errors.append("no_issues result must not include issues")
        if pending_requests:
            errors.append("no_issues result must not include requests")
        audit_summary = payload.get("audit_summary")
        if not isinstance(audit_summary, str) or not audit_summary.strip():
            errors.append("no_issues result must include a non-empty audit_summary")
    elif kind == "issues":
        if not issues:
            errors.append("issues result must include at least one issue")
        if pending_requests:
            errors.append("issues result must not include requests")
        all_issues_outside_current_round_scope = bool(issues) and all(
            isinstance(issue, dict)
            and issue_scope_relation(issue) == ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE
            for issue in issues
        )
        audit_summary = payload.get("audit_summary")
        if all_issues_outside_current_round_scope and not (
            isinstance(audit_summary, str) and audit_summary.strip()
        ):
            # 全部是范围外问题时，本轮范围本身等于「clean」，需要和 no_issues 一样给出审计依据。
            # 只给非致命 warning：做成硬校验会让整腿无效并触发重派，代价过高。
            warnings.append(
                "all issues are outside_current_round_scope but no current-round audit_summary was written; "
                "pass --current-round-scope-audit-summary"
            )
    elif kind == "request":
        if not requests:
            errors.append("request result must include at least one request")

    completed_issues = issues if not errors else []
    evidence_complete, evidence_gaps = frozen_input_evidence_completeness(contract)
    review_complete = not errors and kind in {"issues", "no_issues"} and not pending_requests and evidence_complete is True
    return {
        "valid": not errors,
        "kind": kind if not errors else "invalid",
        "issue_count": len(issues) if not errors else 0,
        "review_complete": review_complete,
        "pending_request_count": len(pending_requests) if not errors else 0,
        "frozen_evidence_gaps": evidence_gaps,
        "outside_current_round_scope_issue_count": sum(
            1
            for issue in completed_issues
            if isinstance(issue, dict)
            and issue_scope_relation(issue) == ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE
        ),
        "issue_indices_in_excluded_paths_one_based": (
            issue_indices_in_excluded_paths_one_based if completed_issues else []
        ),
        "request_count": len(requests) if not errors and kind == "request" else 0,
        "request_types": sorted(set(request_types)) if not errors and kind == "request" else [],
        "lock_request_count": request_types.count("lock_request") if not errors and kind == "request" else 0,
        "audit_summary": payload.get("audit_summary") if not errors and kind in {"no_issues", "issues"} else None,
        "issues": issues if not errors else [],
        "requests": requests if not errors else [],
        "warnings": warnings if not errors else [],
        "errors": errors,
    }


def frozen_input_evidence_completeness(contract: dict[str, Any]) -> tuple[bool, list[str]]:
    if contract.get("frozen_evidence_complete", True) is not True:
        return False, ["冻结输入存在未核验 unit"]
    index_path = contract.get("frozen_evidence_index")
    if not index_path:
        return True, []
    try:
        index_bytes = Path(index_path).read_bytes()
        if hashlib.sha256(index_bytes).hexdigest() != contract.get("frozen_evidence_index_sha256"):
            raise ValueError("冻结证据索引摘要不一致")
        index = json.loads(index_bytes)
        candidate = (contract.get("review_candidate_snapshot") or {}).get("checkpoint_commit")
        if candidate and index.get("candidate_commit") != candidate:
            raise ValueError("冻结证据不属于当前候选")
        expected_units = contract.get("primary_units")
        units = index["units"]
        if expected_units and set(expected_units) != {unit["unit_id"] for unit in units}:
            raise ValueError("冻结证据未覆盖全部 primary_units")
        checked_files = set()
        for unit in units:
            if unit.get("evidence_status") != "verified":
                raise ValueError("冻结 unit 仍有证据缺口")
            for field in ("patch", "before", "after"):
                descriptor = unit.get(field)
                if not descriptor or descriptor["file"] in checked_files:
                    continue
                digest = hashlib.sha256()
                with Path(descriptor["file"]).open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                if digest.hexdigest() != descriptor["sha256"]:
                    raise ValueError("冻结 unit 内容摘要不一致")
                checked_files.add(descriptor["file"])
        return True, []
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return False, [str(exc)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate canonical RVF reviewer result artifacts.")
    parser.add_argument("result_file", help="Review result JSON artifact.")
    parser.add_argument("--scope-contract", help="scope.contract.json. Defaults to RVF_SCOPE_CONTRACT.")
    parser.add_argument("--json", action="store_true", help="Print JSON classification.")
    args = parser.parse_args()

    result = classify(
        Path(args.result_file),
        scope_contract=Path(args.scope_contract).expanduser().resolve() if args.scope_contract else None,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["valid"]:
        print(result["kind"])
        for warning in result.get("warnings") or []:
            print(f"warning: {warning}", file=sys.stderr)
    else:
        for error in result["errors"]:
            print(error, file=sys.stderr)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
