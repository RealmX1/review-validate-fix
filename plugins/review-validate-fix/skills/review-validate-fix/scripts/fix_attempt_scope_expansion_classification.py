"""共享的 attempt 修改权限与冻结审查范围扩展分类；不读写工件。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping

from review_scope_excluded_path_prefixes import BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE


REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES = (
    "primary_files", "fix_allowlist", "task_change_paths", "background_files",
)


def scope_contract_paths(contract: Mapping[str, Any], field: str) -> list[str]:
    values = contract.get(field)
    if not isinstance(values, list):
        canonical = contract.get("canonical_scope")
        values = canonical.get(field, []) if isinstance(canonical, dict) else []
    return [value for value in values if isinstance(value, str) and value] if isinstance(values, list) else []


def reviewed_scope_path_universe(contract: Mapping[str, Any]) -> set[str]:
    return {path for field in REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES for path in scope_contract_paths(contract, field)}


def canonical_issue_authorized_fix_paths(issue: Mapping[str, Any]) -> list[str]:
    values = [issue.get("path"), issue.get("file")]
    values.extend(issue.get("paths", []) if isinstance(issue.get("paths"), list) else [])
    refs = issue.get("artifact_refs")
    if isinstance(refs, list):
        values.extend(ref.get("path") for ref in refs if isinstance(ref, dict))
    paths: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        path = PurePosixPath(value.strip())
        if path.is_absolute() or ".." in path.parts or "\\" in value or path.as_posix() != value.strip() or str(path) == ".":
            # artifact_refs 可指向仓库外的证据；它们不参与仓库内修改授权。
            continue
        if str(path) not in paths:
            paths.append(str(path))
    return paths


def path_matches_scope_prefixes(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes if prefix)


def classify_fix_attempt_scope_expansion(
    *, changed_paths: list[str], boundary_paths: list[str], declared_paths: list[str],
    acknowledged_protected_or_background_paths: list[str], reason: str | None,
    scope_contract: Mapping[str, Any], canonical_issue: Mapping[str, Any],
) -> dict[str, Any]:
    """修改权仍按 attempt boundary；expanded_paths 始终相对冻结合同计算。"""
    changed = list(dict.fromkeys(changed_paths))
    boundary, declared = set(boundary_paths), set(declared_paths)
    builtin_prefixes = list(BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE)
    blocked = [path for path in changed if path_matches_scope_prefixes(path, builtin_prefixes)]
    guarded_prefixes = [*scope_contract_paths(scope_contract, "protected_files"),
                        *scope_contract_paths(scope_contract, "background_files"),
                        *scope_contract_paths(scope_contract, "excluded_path_prefixes")]
    guarded = [path for path in changed if path not in blocked and path_matches_scope_prefixes(path, guarded_prefixes)]
    ordinary = [path for path in changed if path not in blocked and path not in guarded]
    outside_frozen = set(ordinary) - reviewed_scope_path_universe(scope_contract)
    issue_paths = set(canonical_issue_authorized_fix_paths(canonical_issue))
    if canonical_issue.get("fix_issue_handling_mode", "validate_then_fix") != "validate_then_fix":
        issue_paths = set()
    canonical_declared = [path for path in ordinary if path in outside_frozen and path in issue_paths & boundary]
    effective_declared = list(dict.fromkeys([*declared_paths, *canonical_declared]))
    effective_declared_set = set(effective_declared)
    outside_authority = [path for path in ordinary if path not in boundary]
    acknowledged = [path for path in guarded if path in set(acknowledged_protected_or_background_paths)]
    requires_explicit_reason = bool(outside_authority or acknowledged)
    effective_reason = (reason or "").strip()
    if not effective_reason and canonical_declared and not requires_explicit_reason:
        statement = next((str(canonical_issue[key]).strip() for key in ("problem_statement", "message", "summary", "title")
                          if canonical_issue.get(key) and str(canonical_issue[key]).strip()), "主路径的最小修复")
        effective_reason = f"canonical issue {canonical_issue.get('issue_id', '')}: {statement}"
    return {
        "expanded_paths": [path for path in ordinary if path in outside_frozen and path in effective_declared_set],
        "declared_paths": effective_declared,
        "canonical_issue_declared_paths": canonical_declared,
        "declared_paths_outside_attempt_boundary": [path for path in outside_authority if path in declared],
        "undeclared_paths": [path for path in ordinary if
                             (path not in boundary and path not in declared) or
                             (path in outside_frozen and path not in effective_declared_set)],
        "acknowledged_protected_or_background_paths": acknowledged,
        "unacknowledged_protected_or_background_paths": [path for path in guarded if path not in acknowledged],
        "blocked_paths": blocked,
        "requires_explicit_scope_expansion_reason": requires_explicit_reason,
        "reason": effective_reason,
    }
