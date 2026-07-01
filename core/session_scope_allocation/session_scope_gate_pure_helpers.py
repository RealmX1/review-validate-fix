"""Host-中性的 stop-session-scope gate 纯 helper（去-codex S10h-c · 精简迁）。

从引擎 `stop_review_validate_fix.py` 的 SESSION_ALLOC 簇里抽出**零引擎耦合**
的纯值类型 / 分类器 / patch-ownership 完整性计算：它们不做 I/O、不碰 host/event、不驱动
ledger，只在 manifest / allocator-result / env / 内存数据结构上做纯函数计算。gate/allocator
的 event-边界解析器与 ledger 驱动主体本轮刻意留引擎（见计划 S10h-c 决策：B 精简迁）。

R-1：本文件零 host 身份常量 / host 归属键 token（core 非 host_adapter 文件不得命名 host
身份）。仅依赖 stdlib，无 `from core…` import，故无需 pyroot 自举。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class _StopContextError(Exception):
    """Raised by `resolve_stop_context` when the Stop event provided a session
    transcript path but it isn't readable. The orchestrator unwraps the
    `skip_payload` attribute and short-circuits the gate."""

    def __init__(self, skip_payload_value: dict[str, Any]) -> None:
        super().__init__("stop context unresolved")
        self.skip_payload = skip_payload_value


@dataclass
class SessionScopePrecheck:
    checked: bool = False
    context: dict[str, Any] | None = None
    skip_payload: dict[str, Any] | None = None
    route_paths: list[str] | None = None


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def session_change_type_from_manifest(manifest: dict[str, Any] | None) -> str | None:
    if not isinstance(manifest, dict):
        return None
    owned_paths = _string_list(manifest.get("owned_paths"))
    owned_dirty_paths = _string_list(manifest.get("owned_dirty_paths"))
    if not owned_paths:
        return "no_codebase_changes"
    if not owned_dirty_paths:
        return "no_dirty_codebase_changes"
    return "dirty_codebase_changes"


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


REASON_PATCH_OWNERSHIP_INCOMPLETE = "patch_ownership_incomplete"


def _patch_ownership_expected(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {"unit_ids": [], "paths": [], "unresolved": []}
    ownership = manifest.get("patch_ownership")
    if not isinstance(ownership, dict):
        return {"unit_ids": [], "paths": [], "unresolved": []}
    return {
        "unit_ids": _string_list(ownership.get("expected_apply_patch_unit_ids")),
        "paths": _string_list(ownership.get("expected_apply_patch_paths")),
        "unresolved": (
            [item for item in ownership.get("unresolved_owned_patch_hunks", []) if isinstance(item, dict)]
            if isinstance(ownership.get("unresolved_owned_patch_hunks"), list)
            else []
        ),
    }


def _allocated_unit_ids_from_result(result: dict[str, Any]) -> list[str]:
    scope = result.get("scope")
    if isinstance(scope, dict):
        return _string_list(scope.get("unit_ids"))
    return _string_list(result.get("unit_ids"))


def _allocated_paths_from_result(result: dict[str, Any]) -> list[str]:
    scope = result.get("scope")
    if isinstance(scope, dict):
        return _string_list(scope.get("paths"))
    return _string_list(result.get("paths"))


def patch_ownership_incomplete_details(
    manifest: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    expected = _patch_ownership_expected(manifest)
    expected_unit_ids = set(expected["unit_ids"])
    allocated_unit_ids = set(_allocated_unit_ids_from_result(result))
    missing_unit_ids = sorted(expected_unit_ids - allocated_unit_ids)
    expected_paths = set(expected["paths"])
    allocated_paths = set(_allocated_paths_from_result(result))
    missing_paths = sorted(expected_paths - allocated_paths)
    unresolved = expected["unresolved"]
    if not unresolved and not missing_unit_ids:
        return None
    return {
        "reason_code": REASON_PATCH_OWNERSHIP_INCOMPLETE,
        "unresolved_owned_patch_hunks": unresolved,
        "missing_apply_patch_unit_ids": missing_unit_ids,
        "missing_apply_patch_paths": missing_paths,
        "expected_apply_patch_unit_count": len(expected_unit_ids),
        "allocated_unit_count": len(allocated_unit_ids),
    }
