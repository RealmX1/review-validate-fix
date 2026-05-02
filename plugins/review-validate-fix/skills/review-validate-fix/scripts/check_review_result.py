#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


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


def validate_issue(issue: Any, index: int, errors: list[str], excluded_prefixes: list[str]) -> None:
    if not isinstance(issue, dict):
        errors.append(f"issues[{index}] must be an object")
        return
    path = issue.get("path")
    line = issue.get("line")
    message = issue.get("message")
    if not isinstance(path, str) or not is_relative_repo_path(path):
        errors.append(f"issues[{index}].path must be a relative repo path without '..'")
    elif is_excluded(path, excluded_prefixes):
        errors.append(f"issues[{index}].path is excluded by scope contract")
    if not isinstance(line, int) or line < 1:
        errors.append(f"issues[{index}].line must be an integer >= 1")
    if not isinstance(message, str) or not message.strip():
        errors.append(f"issues[{index}].message must be a non-empty string")


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

    for index, issue in enumerate(issues):
        validate_issue(issue, index, errors, excluded_prefixes)

    request_types: list[str] = []
    for index, request in enumerate(requests):
        request_type = validate_request(request, index, errors)
        if request_type is not None:
            request_types.append(request_type)

    if kind == "no_issues":
        if issues:
            errors.append("no_issues result must not include issues")
        if requests:
            errors.append("no_issues result must not include requests")
    elif kind == "issues":
        if not issues:
            errors.append("issues result must include at least one issue")
        if requests:
            errors.append("issues result must not include requests")
    elif kind == "request":
        if issues:
            errors.append("request result must not include issues")
        if not requests:
            errors.append("request result must include at least one request")

    return {
        "valid": not errors,
        "kind": kind if not errors else "invalid",
        "issue_count": len(issues) if not errors and kind == "issues" else 0,
        "request_count": len(requests) if not errors and kind == "request" else 0,
        "request_types": sorted(set(request_types)) if not errors and kind == "request" else [],
        "lock_request_count": request_types.count("lock_request") if not errors and kind == "request" else 0,
        "issues": issues if not errors and kind == "issues" else [],
        "requests": requests if not errors and kind == "request" else [],
        "errors": errors,
    }


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
    else:
        for error in result["errors"]:
            print(error, file=sys.stderr)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
