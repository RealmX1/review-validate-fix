#!/usr/bin/env python3
"""合并阶段结构化处置：``artifacts/merge/merge_dispositions.json`` 的写入与校验（RVF 降本 T4）。

每条 reviewer issue 在合并阶段都必须有一条可机读的处置记录：

- ``adopted``：成为某条 canonical fix-issue 的来源（``issue_id`` 指向它）；
- ``merged_into``：与已被 adopted 的某条 fix-issue 同根因，并入它（``issue_id`` 指向那条）；
- ``dropped``：按 ``references/review-merge-policy.md`` Merge Rule 6 等理由丢弃，必须带 ``reason_code``。

主 agent 自审发现、不来自任何 reviewer 的 fix-issue 记在 ``non_reviewer_fix_issue_sources``
（``origin: main_agent_self_review``），与 reviewer 来源互斥。

**index 起点与 superseded 结果**：``index`` 从 1 起编号（``reviewer_issue_index_base: 1``），与
``rvf wait`` / ``rvf results`` 摘要里每腿 issue 的编号一致，指向该腿**当前 canonical**
``review-result.json`` 的 ``issues[index-1]``。reviewer 结果并非 append-only：
``--redispatch-review-request-legs-only`` 会把旧 canonical 轮转进 ``superseded-review-result-artifacts/``。
因此本工件只覆盖写入时刻的 canonical 结果，并逐腿记下它的 ``review_result_sha256``；superseded
代际一律不覆盖。canonical 结果事后变了（重派、手改），``check`` 以哈希不一致判本工件过期，
必须重新 ``write``。

**source-agnostic 隔离**：本工件放在 fixer prompt 从不引用的 ``artifacts/merge/`` 下。fix-issue 的
``source_refs`` 由本工件派生（``derive_fix_issue_source_refs``），只进 tracker 的 ``source_refs`` 列；
给 fixer 的 ``artifacts/fix-issues/<id>.json`` 镜像不含任何来源字段（``rvf_fix_issue._write_mirror``）。

强制点：
- ``rvf_fix_issue.upsert_canonical_issue``（CLI upsert 与 ``rvf fix prepare`` 共用）——本工件缺失、
  结构不合法、哈希过期或 issue_id 未被处置覆盖时拒绝 upsert；
- ``write_reviewed_task_candidate_evidence.completed_review``——reviewer 报过 issue 时要求全量 check
  通过，并以本工件覆盖的 fix-issue 数核对 handoff 的 canonical 计数（全部 dropped 时允许 N=0）；
- ``analysis_artifacts.scaffold_causality_json``——把本工件与按 reviewer 汇总的处置/最终结果写进
  ``causality.json``（backlog 440b5 Reviewer 效能评估的数据来源）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import safe_token  # noqa: E402
from validation_result_cache import canonical_json_bytes, write_json_with_digest  # noqa: E402


MERGE_DISPOSITIONS_SCHEMA_NAME = "rvf-merge-dispositions"
MERGE_DISPOSITIONS_SCHEMA_VERSION = 1
MERGE_DISPOSITIONS_RELATIVE_PATH = Path("artifacts/merge/merge_dispositions.json")
REVIEWER_ISSUE_INDEX_BASE = 1
REVIEW_RESULT_COVERAGE_CANONICAL_ONLY = "canonical_review_result_only_superseded_generations_excluded"

REVIEWER_ISSUE_DISPOSITION_ADOPTED = "adopted"
REVIEWER_ISSUE_DISPOSITION_MERGED_INTO = "merged_into"
REVIEWER_ISSUE_DISPOSITION_DROPPED = "dropped"
REVIEWER_ISSUE_DISPOSITIONS = (
    REVIEWER_ISSUE_DISPOSITION_ADOPTED,
    REVIEWER_ISSUE_DISPOSITION_MERGED_INTO,
    REVIEWER_ISSUE_DISPOSITION_DROPPED,
)

# EXPERIMENT_NODE: dropped reason_code 的枚举粒度——当前按 Merge Rule 6 的过滤类别 + scope 外 + 兜底；
# 440b5（Reviewer 效能评估）跑出分布后可能需要细分（例如把「误判」从「无法追溯」里拆出来）。
DROPPED_REVIEWER_ISSUE_REASON_CODES: dict[str, str] = {
    "style_or_preference_only": "风格偏好，不影响正确性（Merge Rule 6）",
    "generic_refactor_suggestion": "泛泛的重构建议，没有具体缺陷（Merge Rule 6）",
    "not_traceable_to_current_diff": "无法追溯到本轮 diff / scope（Merge Rule 6）",
    "outside_review_scope_protected_background_or_excluded": "落在 protected / background / excluded 路径",
    "other_with_reason": "其它理由，必须在 reason 写明",
}
DROPPED_REASON_CODE_REQUIRING_FREE_TEXT_REASON = "other_with_reason"

NON_REVIEWER_FIX_ISSUE_ORIGIN_MAIN_AGENT_SELF_REVIEW = "main_agent_self_review"
NON_REVIEWER_FIX_ISSUE_ORIGINS = (NON_REVIEWER_FIX_ISSUE_ORIGIN_MAIN_AGENT_SELF_REVIEW,)

FIX_ISSUE_SOURCE_REF_ORIGIN_REVIEWER_RESULT_ISSUE = "reviewer_result_issue"

# fix-issue payload 里一律视为「来源归因」、不得进 fixer 镜像的字段。
FIX_ISSUE_PAYLOAD_SOURCE_ATTRIBUTION_KEYS = (
    "source_refs",
    "source_reviewers",
    "source_items",
    "reviewer_id",
    "reviewer_ids",
    "reviewer_label",
    "source_reviewer",
)
# 精确的 reviewer 来源标签之外，mimic 兜底腿的固定身份前缀（见 zero-external-reviewer 兜底文档）。
FIXED_REVIEWER_SOURCE_MARKERS = ("codex-mimic-reviewer-a", "codex-mimic-reviewer-b")

MERGE_DISPOSITIONS_EXIT_OK = 0
MERGE_DISPOSITIONS_EXIT_USAGE_ERROR = 2
MERGE_DISPOSITIONS_EXIT_CHECK_FAILED = 3


class MergeDispositionsCheckFailed(ValueError):
    """处置工件缺失 / 不合法 / 过期；``errors`` 逐条列出原因。"""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__("merge dispositions check failed: " + "; ".join(self.errors))


@dataclass(frozen=True)
class CanonicalReviewerLegResult:
    """一条 reviewer 腿**当前 canonical** ``review-result.json`` 的只读快照。"""

    reviewer_id: str
    label: str | None
    review_result_path: Path
    review_result_sha256: str | None
    kind: str | None
    issues: tuple[dict[str, Any], ...]
    read_error: str | None


@dataclass(frozen=True)
class MergeDispositionsCheckResult:
    payload: dict[str, Any] | None
    errors: tuple[str, ...]
    reviewer_sourced_fix_issue_ids: tuple[str, ...]
    non_reviewer_sourced_fix_issue_ids: tuple[str, ...]
    reviewer_source_markers: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def accounted_fix_issue_ids(self) -> tuple[str, ...]:
        return (*self.reviewer_sourced_fix_issue_ids, *self.non_reviewer_sourced_fix_issue_ids)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def merge_dispositions_path(run_dir: Path) -> Path:
    return run_dir / MERGE_DISPOSITIONS_RELATIVE_PATH


def fix_issue_mirror_directory(run_dir: Path) -> Path:
    return run_dir / "artifacts" / "fix-issues"


def fix_issue_mirror_path(run_dir: Path, issue_id: str) -> Path:
    return fix_issue_mirror_directory(run_dir) / f"{safe_token(issue_id)}.json"


def reviewer_issue_sha256(issue: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(issue)).hexdigest()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def run_id_for_run_directory(run_dir: Path) -> str:
    for relative, label in (
        ("summary.json", "run summary"),
        ("artifacts/inputs/scope.contract.json", "scope contract"),
    ):
        path = run_dir / relative
        if not path.is_file():
            continue
        value = _read_json_object(path, label).get("run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"run_id missing: neither summary.json nor scope.contract.json under {run_dir} names it")


def read_canonical_reviewer_leg_results(run_dir: Path) -> list[CanonicalReviewerLegResult]:
    """按 ``reviewer-plan.json`` 的腿序读每腿 canonical ``review-result.json``（与 evidence writer 同一路径）。

    R3 最后兜底（plan ``needs_last_resort_fallback: true``、plan 里没有腿）时，两路 in-harness mimic reviewer
    把结果写在同一目录布局下，按 ``artifacts/reviewers/<reviewer-id>/review-result.json`` 目录遍历取腿
    （与 last-resort 兜底文档「下游按目录遍历消费」一致）。

    不碰 ``superseded-review-result-artifacts/``。单腿读不出时记 ``read_error`` 而不抛，交给调用方判定。"""
    reviewers_directory = run_dir / "artifacts" / "reviewers"
    plan = _read_json_object(reviewers_directory / "reviewer-plan.json", "reviewer plan")
    raw_reviewers = plan.get("reviewers")
    if (not isinstance(raw_reviewers, list) or not raw_reviewers) and plan.get("needs_last_resort_fallback") is True:
        raw_reviewers = [
            {"reviewer_id": child.name}
            for child in sorted(reviewers_directory.iterdir())
            if child.is_dir() and (child / "review-result.json").is_file()
        ]
    if not isinstance(raw_reviewers, list) or not raw_reviewers:
        raise ValueError("reviewer plan has no reviewers")
    legs: list[CanonicalReviewerLegResult] = []
    for reviewer in raw_reviewers:
        if not isinstance(reviewer, dict):
            raise ValueError("reviewer plan entry must be an object")
        reviewer_id = reviewer.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id:
            raise ValueError("reviewer plan reviewer_id is invalid")
        label = reviewer.get("label") if isinstance(reviewer.get("label"), str) else None
        result_path = reviewers_directory / reviewer_id / "review-result.json"
        try:
            encoded = result_path.read_bytes()
            payload = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            legs.append(CanonicalReviewerLegResult(reviewer_id, label, result_path, None, None, (), "missing review-result.json"))
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            legs.append(CanonicalReviewerLegResult(reviewer_id, label, result_path, None, None, (), f"unreadable review-result.json: {exc}"))
            continue
        if not isinstance(payload, dict):
            legs.append(
                CanonicalReviewerLegResult(
                    reviewer_id, label, result_path, hashlib.sha256(encoded).hexdigest(), None, (),
                    "review-result.json root is not an object",
                )
            )
            continue
        raw_issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
        issues = tuple(issue for issue in raw_issues if isinstance(issue, dict))
        read_error = None if len(issues) == len(raw_issues) else "review-result.json has a non-object issue"
        legs.append(
            CanonicalReviewerLegResult(
                reviewer_id=reviewer_id,
                label=label,
                review_result_path=result_path,
                review_result_sha256=hashlib.sha256(encoded).hexdigest(),
                kind=payload.get("kind") if isinstance(payload.get("kind"), str) else None,
                issues=issues,
                read_error=read_error,
            )
        )
    return legs


def reviewer_source_markers_for_legs(legs: Iterable[CanonicalReviewerLegResult]) -> tuple[str, ...]:
    """fix-issue payload 里不得出现的来源字样：本轮每腿的精确 label（``alternative-reviewer:<harness>``
    及其 ``#a/#b`` 变体）加 mimic 兜底腿身份。

    不用裸前缀 ``alternative-reviewer:``：RVF 仓库自审时 issue 本身就可能在讨论这个标签格式，裸前缀会让
    这类真问题无法写成 canonical issue；精确 label 才是「来源泄漏」的形态。"""
    markers: list[str] = list(FIXED_REVIEWER_SOURCE_MARKERS)
    for leg in legs:
        if leg.label and leg.label.startswith("alternative-reviewer:"):
            markers.append(leg.label)
        if leg.reviewer_id.startswith("codex-mimic-reviewer"):
            markers.append(leg.reviewer_id)
    return tuple(dict.fromkeys(markers))


def _source_attribution_key_paths_at_any_depth(value: Any, parent_path: str) -> list[str]:
    """递归收集任意深度出现的来源字段路径：顶层是裸键名（``source_refs``），嵌套形如
    ``context.source_refs`` / ``notes[0].reviewer_id``。命中的键不再往下钻（整个值都会被剥掉）。"""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{parent_path}.{key}" if parent_path else str(key)
            if key in FIX_ISSUE_PAYLOAD_SOURCE_ATTRIBUTION_KEYS:
                found.append(child_path)
            else:
                found.extend(_source_attribution_key_paths_at_any_depth(child, child_path))
    elif isinstance(value, (list, tuple)):
        for position, child in enumerate(value):
            found.extend(_source_attribution_key_paths_at_any_depth(child, f"{parent_path}[{position}]"))
    return found


def fix_issue_payload_source_attribution_errors(
    payload: Mapping[str, Any], *, source_markers: Iterable[str], label: str
) -> list[str]:
    errors: list[str] = []
    present_keys = _source_attribution_key_paths_at_any_depth(payload, "")
    if present_keys:
        errors.append(f"{label} carries source attribution fields {present_keys} (fixer must stay source-agnostic)")
    text = json.dumps(payload, ensure_ascii=False)
    leaked = [marker for marker in source_markers if marker and marker in text]
    if leaked:
        errors.append(f"{label} mentions reviewer source labels {leaked} (fixer must stay source-agnostic)")
    return errors


def _strip_source_attribution_keys_at_any_depth(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_source_attribution_keys_at_any_depth(child)
            for key, child in value.items()
            if key not in FIX_ISSUE_PAYLOAD_SOURCE_ATTRIBUTION_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_source_attribution_keys_at_any_depth(child) for child in value]
    return value


def strip_source_attribution_keys_from_fix_issue_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """任意深度剥掉来源字段（嵌套 dict / list 里的也算），给 fixer 的镜像必须完全 source-agnostic。"""
    return _strip_source_attribution_keys_at_any_depth(payload)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _non_empty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def excluded_path_prefixes_for_run(run_dir: Path) -> tuple[str, ...]:
    """本 run 冻结合同里的排除前缀（合同缺失 / 不可读 ⇒ 空，交给 Contract Gate 报合同错误）。"""
    contract_path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if not isinstance(contract, dict):
        return ()
    prefixes = contract.get("excluded_path_prefixes")
    if prefixes is None and isinstance(contract.get("canonical_scope"), dict):
        prefixes = contract["canonical_scope"].get("excluded_path_prefixes")
    if not isinstance(prefixes, list):
        return ()
    return tuple(str(prefix) for prefix in prefixes if isinstance(prefix, str))


def collect_merge_dispositions_structural_errors(
    payload: Mapping[str, Any],
    legs: list[CanonicalReviewerLegResult],
    *,
    expected_run_id: str | None,
    excluded_path_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """不依赖 fix-issue 是否已 upsert 的校验：schema、完整性、哈希一致、每条处置的字段、合并目标。

    排除区里的 reviewer issue 只能 ``dropped``：Contract Gate 已从「整腿作废」放宽为 warning
    （护栏而非闸门），这里是放宽后唯一的收口点，防止排除区问题被采纳进修复。"""
    import check_review_result  # noqa: PLC0415

    errors: list[str] = []
    if payload.get("schema") != MERGE_DISPOSITIONS_SCHEMA_NAME:
        errors.append(f"schema must be {MERGE_DISPOSITIONS_SCHEMA_NAME}")
    if payload.get("schema_version") != MERGE_DISPOSITIONS_SCHEMA_VERSION:
        errors.append(f"schema_version must be {MERGE_DISPOSITIONS_SCHEMA_VERSION}")
    if payload.get("reviewer_issue_index_base") != REVIEWER_ISSUE_INDEX_BASE:
        errors.append(f"reviewer_issue_index_base must be {REVIEWER_ISSUE_INDEX_BASE}")
    if expected_run_id is not None and payload.get("run_id") != expected_run_id:
        errors.append(f"run_id {payload.get('run_id')!r} does not match this run ({expected_run_id!r})")

    legs_by_reviewer_id = {leg.reviewer_id: leg for leg in legs}
    for leg in legs:
        if leg.read_error:
            errors.append(f"reviewer {leg.reviewer_id}: {leg.read_error}")
    raw_results = payload.get("reviewer_results")
    recorded_results: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw_results, list):
        errors.append("reviewer_results must be an array")
        raw_results = []
    for entry in raw_results:
        reviewer_id = entry.get("reviewer_id") if isinstance(entry, dict) else None
        if not isinstance(reviewer_id, str) or reviewer_id in recorded_results:
            errors.append(f"reviewer_results entry is invalid or duplicated: {entry!r}")
            continue
        recorded_results[reviewer_id] = entry
    if set(recorded_results) != set(legs_by_reviewer_id):
        errors.append(
            f"reviewer_results cover {sorted(recorded_results)} but reviewer-plan.json has {sorted(legs_by_reviewer_id)}"
        )
    for reviewer_id, entry in recorded_results.items():
        leg = legs_by_reviewer_id.get(reviewer_id)
        if leg is None:
            continue
        if entry.get("review_result_sha256") != leg.review_result_sha256:
            errors.append(
                f"reviewer {reviewer_id}: canonical review-result.json changed since dispositions were written "
                "(redispatched or edited); rerun `rvf merge write`"
            )
        if entry.get("issue_count") != len(leg.issues):
            errors.append(f"reviewer {reviewer_id}: issue_count {entry.get('issue_count')} != {len(leg.issues)}")

    raw_dispositions = payload.get("dispositions")
    if not isinstance(raw_dispositions, list):
        errors.append("dispositions must be an array")
        raw_dispositions = []
    seen_keys: set[tuple[str, int]] = set()
    adopted_targets: set[str] = set()
    merged_targets: dict[str, list[str]] = {}
    for entry in raw_dispositions:
        if not isinstance(entry, dict):
            errors.append(f"disposition entry must be an object: {entry!r}")
            continue
        reviewer_id = entry.get("reviewer_id")
        index = entry.get("index")
        where = f"disposition {reviewer_id}#{index}"
        leg = legs_by_reviewer_id.get(reviewer_id) if isinstance(reviewer_id, str) else None
        if leg is None:
            errors.append(f"{where}: reviewer_id is not a leg in reviewer-plan.json")
            continue
        if isinstance(index, bool) or not isinstance(index, int) or not (
            REVIEWER_ISSUE_INDEX_BASE <= index < REVIEWER_ISSUE_INDEX_BASE + len(leg.issues)
        ):
            errors.append(f"{where}: index out of range (1..{len(leg.issues)})")
            continue
        key = (reviewer_id, index)
        if key in seen_keys:
            errors.append(f"{where}: reviewer issue has more than one disposition")
            continue
        seen_keys.add(key)
        if entry.get("issue_sha256") != reviewer_issue_sha256(leg.issues[index - REVIEWER_ISSUE_INDEX_BASE]):
            errors.append(f"{where}: issue_sha256 does not match the current canonical reviewer issue")
        disposition = entry.get("disposition")
        issue_id = _non_empty_string(entry.get("issue_id"))
        reviewer_issue_path = leg.issues[index - REVIEWER_ISSUE_INDEX_BASE].get("path")
        if (
            disposition != REVIEWER_ISSUE_DISPOSITION_DROPPED
            and isinstance(reviewer_issue_path, str)
            and check_review_result.is_excluded(reviewer_issue_path, list(excluded_path_prefixes))
        ):
            errors.append(
                f"{where}: issue path {reviewer_issue_path} is under an excluded path prefix; it can only be "
                "dropped (reason_code outside_review_scope_protected_background_or_excluded)"
            )
        if disposition in (REVIEWER_ISSUE_DISPOSITION_ADOPTED, REVIEWER_ISSUE_DISPOSITION_MERGED_INTO):
            if issue_id is None:
                errors.append(f"{where}: {disposition} requires issue_id")
                continue
            if "reason_code" in entry:
                errors.append(f"{where}: reason_code only applies to dropped")
            if disposition == REVIEWER_ISSUE_DISPOSITION_ADOPTED:
                adopted_targets.add(issue_id)
            else:
                merged_targets.setdefault(issue_id, []).append(where)
        elif disposition == REVIEWER_ISSUE_DISPOSITION_DROPPED:
            if "issue_id" in entry:
                errors.append(f"{where}: dropped must not name an issue_id")
            reason_code = entry.get("reason_code")
            if reason_code not in DROPPED_REVIEWER_ISSUE_REASON_CODES:
                errors.append(
                    f"{where}: dropped requires reason_code in {sorted(DROPPED_REVIEWER_ISSUE_REASON_CODES)}"
                )
            elif reason_code == DROPPED_REASON_CODE_REQUIRING_FREE_TEXT_REASON and _non_empty_string(entry.get("reason")) is None:
                errors.append(f"{where}: reason_code {reason_code} requires a non-empty reason")
        else:
            errors.append(f"{where}: disposition must be one of {list(REVIEWER_ISSUE_DISPOSITIONS)}")
    for leg in legs:
        for index in range(REVIEWER_ISSUE_INDEX_BASE, REVIEWER_ISSUE_INDEX_BASE + len(leg.issues)):
            if (leg.reviewer_id, index) not in seen_keys:
                errors.append(f"reviewer issue {leg.reviewer_id}#{index} has no disposition")
    for issue_id, sources in merged_targets.items():
        if issue_id not in adopted_targets:
            errors.append(
                f"merged_into target {issue_id} has no adopted reviewer issue ({', '.join(sources)}); "
                "every fix-issue needs at least one adopted source"
            )

    raw_non_reviewer = payload.get("non_reviewer_fix_issue_sources", [])
    if not isinstance(raw_non_reviewer, list):
        errors.append("non_reviewer_fix_issue_sources must be an array")
        raw_non_reviewer = []
    non_reviewer_ids: set[str] = set()
    for entry in raw_non_reviewer:
        issue_id = _non_empty_string(entry.get("issue_id")) if isinstance(entry, dict) else None
        if issue_id is None:
            errors.append(f"non_reviewer_fix_issue_sources entry needs issue_id: {entry!r}")
            continue
        if entry.get("origin") not in NON_REVIEWER_FIX_ISSUE_ORIGINS:
            errors.append(f"non-reviewer fix-issue {issue_id}: origin must be one of {list(NON_REVIEWER_FIX_ISSUE_ORIGINS)}")
        if issue_id in non_reviewer_ids:
            errors.append(f"non-reviewer fix-issue {issue_id} is listed more than once")
        non_reviewer_ids.add(issue_id)
        if issue_id in adopted_targets or issue_id in merged_targets:
            errors.append(
                f"fix-issue {issue_id} is both reviewer-sourced and non-reviewer-sourced; "
                "a reviewer that found it is its source — drop the non-reviewer entry"
            )
    return errors


# upsert 时实际写进 tracker ``source_refs`` 列的来源快照，放在 fixer 不读的 ``artifacts/merge/`` 下。
# 全量 check 拿它和「当前处置派生的 source_refs」比对，发现「upsert 之后又改写处置」造成的 tracker 来源过期；
# check 本身不读 tracker DB（evidence 侧拿不到测试 / 非默认的 tracker log root）。
FIX_ISSUE_SOURCE_REFS_RECORDED_AT_UPSERT_RELATIVE_DIRECTORY = Path(
    "artifacts/merge/fix-issue-source-refs-recorded-at-upsert"
)


def fix_issue_source_refs_recorded_at_upsert_path(run_dir: Path, issue_id: str) -> Path:
    return run_dir / FIX_ISSUE_SOURCE_REFS_RECORDED_AT_UPSERT_RELATIVE_DIRECTORY / f"{safe_token(issue_id)}.json"


def record_fix_issue_source_refs_written_to_tracker_at_upsert(
    run_dir: Path, issue_id: str, source_refs: Iterable[Mapping[str, Any]]
) -> Path:
    """``rvf_fix_issue.upsert_canonical_issue`` 在 tracker upsert 成功之后调用：记下刚写进 tracker 的 ``source_refs``。"""
    from validation_result_cache import atomic_write  # noqa: PLC0415

    path = fix_issue_source_refs_recorded_at_upsert_path(run_dir, issue_id)
    atomic_write(
        path,
        canonical_json_bytes({"issue_id": issue_id, "source_refs": [dict(ref) for ref in source_refs]}),
    )
    return path


def _fix_issue_source_refs_recorded_at_upsert_staleness_error(
    run_dir: Path, issue_id: str, merge_dispositions_payload: Mapping[str, Any]
) -> str | None:
    """已 upsert 的 fix-issue：upsert 时记下的 tracker ``source_refs`` 必须等于当前处置派生的；否则 tracker 来源过期。"""
    rerun_hint = (
        f"rerun `rvf fix prepare` / `rvf_fix_issue.py upsert` for {issue_id} so the tracker source_refs "
        "follow the current merge dispositions"
    )
    path = fix_issue_source_refs_recorded_at_upsert_path(run_dir, issue_id)
    if not path.is_file():
        return f"fix-issue {issue_id} has no source_refs recorded at upsert ({path.relative_to(run_dir)}); {rerun_hint}"
    try:
        recorded = _read_json_object(path, "fix-issue source_refs recorded at upsert")
        current = derive_fix_issue_source_refs(merge_dispositions_payload, issue_id)
    except (KeyError, TypeError, ValueError) as exc:
        return f"fix-issue {issue_id}: cannot compare source_refs recorded at upsert ({exc}); {rerun_hint}"
    if recorded.get("issue_id") != issue_id or recorded.get("source_refs") != current:
        return (
            f"fix-issue {issue_id}: tracker source_refs recorded at upsert no longer match the current merge "
            f"dispositions (dispositions were rewritten after upsert); {rerun_hint}"
        )
    return None


def collect_fix_issue_cross_reference_errors(
    run_dir: Path,
    *,
    reviewer_sourced_fix_issue_ids: Iterable[str],
    non_reviewer_sourced_fix_issue_ids: Iterable[str],
    source_markers: Iterable[str],
    merge_dispositions_payload: Mapping[str, Any],
) -> list[str]:
    """fix-issue upsert 全部完成之后才成立的校验：每个处置目标都有镜像、每个镜像都有来源、镜像 source-agnostic，
    且每个已 upsert 的处置目标在 tracker 里的 ``source_refs``（以 upsert 时的记录为准）没有因事后改写处置而过期。"""
    errors: list[str] = []
    accounted = [*reviewer_sourced_fix_issue_ids, *non_reviewer_sourced_fix_issue_ids]
    for issue_id in accounted:
        if not fix_issue_mirror_path(run_dir, issue_id).is_file():
            errors.append(f"fix-issue {issue_id} is a disposition target but was never upserted (rvf fix prepare)")
            continue
        staleness_error = _fix_issue_source_refs_recorded_at_upsert_staleness_error(
            run_dir, issue_id, merge_dispositions_payload
        )
        if staleness_error is not None:
            errors.append(staleness_error)
    accounted_set = set(accounted)
    directory = fix_issue_mirror_directory(run_dir)
    mirror_paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    markers = tuple(source_markers)
    for mirror_path in mirror_paths:
        try:
            mirror = _read_json_object(mirror_path, "fix-issue mirror")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        issue_id = _non_empty_string(mirror.get("issue_id"))
        if issue_id is None or issue_id not in accounted_set:
            errors.append(
                f"fix-issue {issue_id or mirror_path.name} has no adopted reviewer source and is not listed as a "
                f"non-reviewer ({NON_REVIEWER_FIX_ISSUE_ORIGIN_MAIN_AGENT_SELF_REVIEW}) source"
            )
        errors.extend(
            fix_issue_payload_source_attribution_errors(
                mirror, source_markers=markers, label=f"fix-issue mirror {mirror_path.name}"
            )
        )
    return errors


def _fix_issue_ids_by_source(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reviewer_sourced: list[str] = []
    for entry in payload.get("dispositions") or []:
        if isinstance(entry, dict) and entry.get("disposition") == REVIEWER_ISSUE_DISPOSITION_ADOPTED:
            issue_id = _non_empty_string(entry.get("issue_id"))
            if issue_id is not None:
                reviewer_sourced.append(issue_id)
    non_reviewer: list[str] = []
    for entry in payload.get("non_reviewer_fix_issue_sources") or []:
        issue_id = _non_empty_string(entry.get("issue_id")) if isinstance(entry, dict) else None
        if issue_id is not None:
            non_reviewer.append(issue_id)
    return tuple(dict.fromkeys(reviewer_sourced)), tuple(dict.fromkeys(non_reviewer))


def check_merge_dispositions(
    run_dir: Path, *, require_fix_issue_cross_reference: bool
) -> MergeDispositionsCheckResult:
    """读盘上的处置工件并校验；不抛，错误逐条放进 ``errors``。

    ``require_fix_issue_cross_reference=False`` 用在 upsert 之前（fix-issue 还没全部落镜像）；
    evidence writer / ``rvf merge check`` 用 True。"""
    path = merge_dispositions_path(run_dir)
    if not path.is_file():
        return MergeDispositionsCheckResult(
            None, (f"merge dispositions are missing: {path} (run `rvf merge write` after merging reviewer issues)",), (), ()
        )
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return MergeDispositionsCheckResult(None, (f"invalid merge dispositions: {exc}",), (), ())
    if not isinstance(payload, dict):
        return MergeDispositionsCheckResult(None, ("merge dispositions must contain a JSON object",), (), ())
    errors: list[str] = []
    sidecar = Path(f"{path}.sha256")
    if not sidecar.is_file() or sidecar.read_bytes() != f"{hashlib.sha256(encoded).hexdigest()}\n".encode("ascii"):
        errors.append("merge dispositions digest sidecar is missing or does not match (rewrite with `rvf merge write`)")
    try:
        legs = read_canonical_reviewer_leg_results(run_dir)
        expected_run_id = run_id_for_run_directory(run_dir)
    except ValueError as exc:
        return MergeDispositionsCheckResult(payload, (*errors, str(exc)), (), ())
    errors.extend(
        collect_merge_dispositions_structural_errors(
            payload, legs, expected_run_id=expected_run_id,
            excluded_path_prefixes=excluded_path_prefixes_for_run(run_dir),
        )
    )
    reviewer_sourced, non_reviewer = _fix_issue_ids_by_source(payload)
    source_markers = reviewer_source_markers_for_legs(legs)
    if require_fix_issue_cross_reference:
        errors.extend(
            collect_fix_issue_cross_reference_errors(
                run_dir,
                reviewer_sourced_fix_issue_ids=reviewer_sourced,
                non_reviewer_sourced_fix_issue_ids=non_reviewer,
                source_markers=source_markers,
                merge_dispositions_payload=payload,
            )
        )
    return MergeDispositionsCheckResult(payload, tuple(errors), reviewer_sourced, non_reviewer, source_markers)


def require_passing_merge_dispositions(
    run_dir: Path, *, require_fix_issue_cross_reference: bool
) -> MergeDispositionsCheckResult:
    result = check_merge_dispositions(run_dir, require_fix_issue_cross_reference=require_fix_issue_cross_reference)
    if not result.passed:
        raise MergeDispositionsCheckFailed(result.errors)
    return result


def derive_fix_issue_source_refs(payload: Mapping[str, Any], issue_id: str) -> list[dict[str, Any]]:
    """fix-issue 的 ``source_refs`` 统一 schema，只由处置工件派生：

    - reviewer 来源：``{source_origin: reviewer_result_issue, reviewer_id, index, issue_sha256, disposition}``，
      ``disposition`` ∈ {adopted, merged_into}，``index`` 从 1 起；
    - 非 reviewer 来源：``{source_origin: main_agent_self_review}``。"""
    refs: list[dict[str, Any]] = []
    for entry in payload.get("dispositions") or []:
        if (
            isinstance(entry, dict)
            and entry.get("disposition") in (REVIEWER_ISSUE_DISPOSITION_ADOPTED, REVIEWER_ISSUE_DISPOSITION_MERGED_INTO)
            and _non_empty_string(entry.get("issue_id")) == issue_id
        ):
            refs.append(
                {
                    "source_origin": FIX_ISSUE_SOURCE_REF_ORIGIN_REVIEWER_RESULT_ISSUE,
                    "reviewer_id": entry["reviewer_id"],
                    "index": entry["index"],
                    "issue_sha256": entry["issue_sha256"],
                    "disposition": entry["disposition"],
                }
            )
    for entry in payload.get("non_reviewer_fix_issue_sources") or []:
        if isinstance(entry, dict) and _non_empty_string(entry.get("issue_id")) == issue_id:
            refs.append({"source_origin": entry["origin"]})
    return refs


def resolve_fix_issue_source_refs_for_upsert(run_dir: Path, issue_id: str) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """upsert 强制点：处置工件必须结构合法且未过期，且 ``issue_id`` 被它覆盖。

    返回 ``(source_refs, 来源字样列表)``；后者给调用方断言 payload source-agnostic 用。"""
    result = require_passing_merge_dispositions(run_dir, require_fix_issue_cross_reference=False)
    assert result.payload is not None
    if issue_id not in result.accounted_fix_issue_ids:
        raise MergeDispositionsCheckFailed(
            [
                f"issue_id {issue_id} is not a disposition target: adopt a reviewer issue into it, or list it in "
                f"non_reviewer_fix_issue_sources (origin {NON_REVIEWER_FIX_ISSUE_ORIGIN_MAIN_AGENT_SELF_REVIEW}), "
                "then rerun `rvf merge write`"
            ]
        )
    return derive_fix_issue_source_refs(result.payload, issue_id), result.reviewer_source_markers


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def _scope_contract_path_if_present(run_dir: Path) -> Path | None:
    path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    return path if path.is_file() else None


def _contract_gate_errors_for_legs(run_dir: Path, legs: list[CanonicalReviewerLegResult]) -> list[str]:
    """写入前按 Contract Gate 校验每腿：canonical 结果必须合法且为 issues / no_issues。"""
    import check_review_result  # noqa: PLC0415

    errors: list[str] = []
    scope_contract = _scope_contract_path_if_present(run_dir)
    for leg in legs:
        if leg.read_error:
            errors.append(f"reviewer {leg.reviewer_id}: {leg.read_error}")
            continue
        classified = check_review_result.classify(leg.review_result_path, scope_contract=scope_contract)
        if not classified.get("valid") or classified.get("kind") not in ("issues", "no_issues"):
            details = "; ".join(str(item) for item in classified.get("errors") or []) or f"kind={classified.get('kind')}"
            errors.append(
                f"reviewer {leg.reviewer_id}: canonical review-result is not a completed issues/no_issues result ({details})"
            )
    return errors


def build_merge_dispositions_payload(run_dir: Path, decisions: Mapping[str, Any]) -> dict[str, Any]:
    """把主 agent 的合并决定补全成处置工件：填 run_id、逐腿哈希与 issue_count、每条 ``issue_sha256``。

    ``decisions`` 形如 ``{"dispositions": [{reviewer_id, index, disposition, issue_id? | reason_code, reason?}],
    "non_reviewer_fix_issue_sources": [{issue_id, origin, reason?}]}``。"""
    legs = read_canonical_reviewer_leg_results(run_dir)
    gate_errors = _contract_gate_errors_for_legs(run_dir, legs)
    if gate_errors:
        raise MergeDispositionsCheckFailed(gate_errors)
    raw_dispositions = decisions.get("dispositions", [])
    raw_non_reviewer = decisions.get("non_reviewer_fix_issue_sources", [])
    if not isinstance(raw_dispositions, list) or not isinstance(raw_non_reviewer, list):
        raise MergeDispositionsCheckFailed(["decisions.dispositions and decisions.non_reviewer_fix_issue_sources must be arrays"])
    legs_by_reviewer_id = {leg.reviewer_id: leg for leg in legs}
    leg_order = {leg.reviewer_id: position for position, leg in enumerate(legs)}
    dispositions: list[dict[str, Any]] = []
    for raw in raw_dispositions:
        if not isinstance(raw, dict):
            raise MergeDispositionsCheckFailed([f"disposition decision must be an object: {raw!r}"])
        entry = {
            key: raw[key]
            for key in ("reviewer_id", "index", "disposition", "issue_id", "reason_code", "reason")
            if key in raw and raw[key] is not None
        }
        leg = legs_by_reviewer_id.get(entry.get("reviewer_id"))  # type: ignore[arg-type]
        index = entry.get("index")
        if (
            leg is not None
            and isinstance(index, int)
            and not isinstance(index, bool)
            and REVIEWER_ISSUE_INDEX_BASE <= index < REVIEWER_ISSUE_INDEX_BASE + len(leg.issues)
        ):
            entry["issue_sha256"] = reviewer_issue_sha256(leg.issues[index - REVIEWER_ISSUE_INDEX_BASE])
        dispositions.append(entry)
    dispositions.sort(
        key=lambda item: (leg_order.get(item.get("reviewer_id"), len(leg_order)), item.get("index") if isinstance(item.get("index"), int) else 0)
    )
    non_reviewer = [
        {key: raw[key] for key in ("issue_id", "origin", "reason") if key in raw and raw[key] is not None}
        if isinstance(raw, dict)
        else raw
        for raw in raw_non_reviewer
    ]
    run_id = run_id_for_run_directory(run_dir)
    payload = {
        "schema": MERGE_DISPOSITIONS_SCHEMA_NAME,
        "schema_version": MERGE_DISPOSITIONS_SCHEMA_VERSION,
        "run_id": run_id,
        "reviewer_issue_index_base": REVIEWER_ISSUE_INDEX_BASE,
        "review_result_coverage": REVIEW_RESULT_COVERAGE_CANONICAL_ONLY,
        "reviewer_results": [
            {
                "reviewer_id": leg.reviewer_id,
                "review_result_path": str(leg.review_result_path.relative_to(run_dir)),
                "review_result_sha256": leg.review_result_sha256,
                "kind": leg.kind,
                "issue_count": len(leg.issues),
            }
            for leg in legs
        ],
        "dispositions": dispositions,
        "non_reviewer_fix_issue_sources": non_reviewer,
    }
    errors = collect_merge_dispositions_structural_errors(
        payload, legs, expected_run_id=run_id, excluded_path_prefixes=excluded_path_prefixes_for_run(run_dir)
    )
    if errors:
        raise MergeDispositionsCheckFailed(errors)
    return payload


def write_merge_dispositions(run_dir: Path, decisions: Mapping[str, Any]) -> tuple[Path, str, dict[str, Any]]:
    payload = build_merge_dispositions_payload(run_dir, decisions)
    path = merge_dispositions_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = write_json_with_digest(path, payload)
    return path, digest, payload


# --------------------------------------------------------------------------- #
# 汇总（440b5：按 reviewer 统计采纳 / 丢弃 / 最终修复）
# --------------------------------------------------------------------------- #


def summarize_reviewer_issue_dispositions_and_final_fix_issue_outcomes(
    payload: Mapping[str, Any], fix_issue_state_by_issue_id: Mapping[str, str]
) -> dict[str, Any]:
    """按 reviewer 汇总：reviewer issue 数、各处置数、dropped 的 reason_code 分布，以及 adopted / merged_into
    的 reviewer issue 最终落到的 fix-issue 状态（tracker ``rvf_issues.state``：fixed / false_positive / …；
    没有 tracker 记录记为 ``unknown``）。"""
    per_reviewer: dict[str, dict[str, Any]] = {}
    for result in payload.get("reviewer_results") or []:
        if isinstance(result, dict) and isinstance(result.get("reviewer_id"), str):
            per_reviewer[result["reviewer_id"]] = {
                "reviewer_issue_count": result.get("issue_count", 0),
                "disposition_counts": {kind: 0 for kind in REVIEWER_ISSUE_DISPOSITIONS},
                "dropped_reason_code_counts": {},
                "final_fix_issue_state_counts_for_adopted_or_merged_reviewer_issues": {},
            }
    for entry in payload.get("dispositions") or []:
        if not isinstance(entry, dict):
            continue
        bucket = per_reviewer.get(entry.get("reviewer_id"))  # type: ignore[arg-type]
        disposition = entry.get("disposition")
        if bucket is None or disposition not in REVIEWER_ISSUE_DISPOSITIONS:
            continue
        bucket["disposition_counts"][disposition] += 1
        if disposition == REVIEWER_ISSUE_DISPOSITION_DROPPED:
            counts = bucket["dropped_reason_code_counts"]
            counts[entry.get("reason_code")] = counts.get(entry.get("reason_code"), 0) + 1
        else:
            state = fix_issue_state_by_issue_id.get(str(entry.get("issue_id")), "unknown")
            counts = bucket["final_fix_issue_state_counts_for_adopted_or_merged_reviewer_issues"]
            counts[state] = counts.get(state, 0) + 1
    non_reviewer_states = Counter(
        fix_issue_state_by_issue_id.get(str(entry.get("issue_id")), "unknown")
        for entry in payload.get("non_reviewer_fix_issue_sources") or []
        if isinstance(entry, dict)
    )
    return {
        "per_reviewer": per_reviewer,
        "non_reviewer_fix_issue_final_state_counts": dict(non_reviewer_states),
    }


def render_merge_dispositions_human_summary(path: Path, digest: str, payload: Mapping[str, Any]) -> str:
    summary = summarize_reviewer_issue_dispositions_and_final_fix_issue_outcomes(payload, {})
    lines = [f"[rvf merge] 处置已写入 {path}（sha256 {digest[:12]}…）"]
    for reviewer_id, bucket in summary["per_reviewer"].items():
        counts = bucket["disposition_counts"]
        lines.append(
            f"- 腿 {reviewer_id}：{bucket['reviewer_issue_count']} 条 → adopted {counts['adopted']} / "
            f"merged_into {counts['merged_into']} / dropped {counts['dropped']}"
        )
    for entry in payload.get("dispositions") or []:
        target = entry.get("issue_id") or f"{entry.get('reason_code')}" + (f"：{entry['reason']}" if entry.get("reason") else "")
        lines.append(f"    {entry.get('reviewer_id')}#{entry.get('index')} {entry.get('disposition')} → {target}")
    for entry in payload.get("non_reviewer_fix_issue_sources") or []:
        lines.append(f"- 非 reviewer 来源：{entry.get('issue_id')}（{entry.get('origin')}）")
    reviewer_sourced, non_reviewer = _fix_issue_ids_by_source(payload)
    targets = [*reviewer_sourced, *non_reviewer]
    if targets:
        lines.append(
            f"- 下一步：为 {', '.join(targets)} 各写一份 canonical issue JSON（issue_id 与上面一致、不写任何来源字段），"
            "`rvf fix prepare <issue.json>…`。"
        )
    else:
        lines.append("- 全部 reviewer issue 已 dropped、没有 fix-issue：走 clean path 写 handoff（共 0 条）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _read_decisions(raw_path: str) -> dict[str, Any]:
    text = sys.stdin.read() if raw_path == "-" else Path(raw_path).expanduser().read_text(encoding="utf-8")
    decisions = json.loads(text)
    if not isinstance(decisions, dict):
        raise ValueError("decisions root must be a JSON object")
    return decisions


def command_write(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    path, digest, payload = write_merge_dispositions(run_dir, _read_decisions(args.decisions_file))
    if args.json:
        print(json.dumps({"status": "written", "path": str(path), "sha256": digest, "payload": payload}, ensure_ascii=False))
    else:
        print(render_merge_dispositions_human_summary(path, digest, payload))
    return MERGE_DISPOSITIONS_EXIT_OK


def command_check(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    result = check_merge_dispositions(
        run_dir, require_fix_issue_cross_reference=not args.without_fix_issue_cross_reference
    )
    if args.json:
        print(
            json.dumps(
                {
                    "status": "passed" if result.passed else "failed",
                    "errors": list(result.errors),
                    "reviewer_sourced_fix_issue_ids": list(result.reviewer_sourced_fix_issue_ids),
                    "non_reviewer_sourced_fix_issue_ids": list(result.non_reviewer_sourced_fix_issue_ids),
                },
                ensure_ascii=False,
            )
        )
    elif result.passed:
        print(
            f"[rvf merge check] 通过：{len(result.accounted_fix_issue_ids)} 条 fix-issue"
            f"（reviewer 来源 {len(result.reviewer_sourced_fix_issue_ids)}、"
            f"非 reviewer 来源 {len(result.non_reviewer_sourced_fix_issue_ids)}）"
        )
    else:
        print("[rvf merge check] 未通过：", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
    return MERGE_DISPOSITIONS_EXIT_OK if result.passed else MERGE_DISPOSITIONS_EXIT_CHECK_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RVF 合并阶段结构化处置：merge_dispositions.json 的 write / check。")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="把合并决定补全为 artifacts/merge/merge_dispositions.json（带 .sha256）")
    write.add_argument("--run-dir", required=True)
    write.add_argument(
        "--decisions-file",
        default="-",
        help='合并决定 JSON；缺省读 stdin。形如 {"dispositions":[{"reviewer_id":…,"index":1,"disposition":'
        '"adopted","issue_id":"RVF-001"}, {…,"disposition":"dropped","reason_code":…}],'
        '"non_reviewer_fix_issue_sources":[{"issue_id":…,"origin":"main_agent_self_review"}]}',
    )
    write.add_argument("--json", action="store_true")
    write.set_defaults(func=command_write)
    check = sub.add_parser("check", help="校验处置工件（缺省含 fix-issue 交叉核对）")
    check.add_argument("--run-dir", required=True)
    check.add_argument(
        "--without-fix-issue-cross-reference",
        action="store_true",
        help="fix-issue 尚未全部 upsert 时只查结构 / 完整性 / 哈希",
    )
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=command_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MergeDispositionsCheckFailed as exc:
        print("[rvf merge] 处置不合法：", file=sys.stderr)
        for error in exc.errors:
            print(f"- {error}", file=sys.stderr)
        return MERGE_DISPOSITIONS_EXIT_CHECK_FAILED
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[rvf merge] {exc}", file=sys.stderr)
        return MERGE_DISPOSITIONS_EXIT_USAGE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
