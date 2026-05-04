#!/usr/bin/env python3
"""为 ``/rvf-analyze`` 事后复盘 agent 准备确定性 scaffold。

模块职责：从已 finalize 的 RVF run 目录里读取产物（``summary.json`` /
``trajectory/`` / ``workspace-diff.json`` / ``artifacts/reviewers/<id>/`` 等），
把所有可以机械抽取的事实（计数、路径、issue 列表、patch 调用列表）预填到两份
artifact 里：

- ``<run_dir>/artifacts/analysis/summary.md``：Markdown 叙事骨架，每节先列出
  确定性事实，再以 ``<!-- TODO(rvf-analyze): ... -->`` 注释标出待 LLM 补全的
  位置。
- ``<run_dir>/artifacts/analysis/causality.json``：结构化 ``issues[]`` 与
  ``patches[]`` 列表，``candidate_patch_call_ids`` 等 mapping 字段留空，由 LLM
  agent 填写。

设计意图：让 LLM agent 接管时已有结构清晰的起点；同时保证即便 LLM 步骤被跳过，
用户仍可读到一份带骨架的 artifact。

本模块不依赖任何同 plugin 的兄弟脚本（不 import ``trajectory_capture`` 等），
保持自包含；所有 schema 字段名以本模块文档为准。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ANALYSIS_SCHEMA_VERSION = 1
ANALYSIS_DIR_NAME = "analysis"


# --------------------------------------------------------------------------- #
# 公共数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalysisInputs:
    """已发现的分析输入路径集合。缺失文件对应字段为 ``None`` / 空列表。"""

    run_dir: Path
    summary_json: Path | None
    handoff_md: Path | None
    workspace_diff_json: Path | None
    workspace_diff_patch: Path | None
    trajectory_jsonl: Path | None
    trajectory_index_json: Path | None
    rvf_rollout_jsonl: Path | None
    pre_rvf_dir: Path | None
    reviewer_results: list[Path] = field(default_factory=list)
    reviewer_trajectories: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class ScaffoldStats:
    """从输入里确定性抽取的数值，用于预填 scaffold。"""

    run_id: str | None
    decision_kind: str | None
    finalize_started_at: str | None
    finalize_completed_at: str | None
    pre_rvf_source_kind: str | None
    post_rvf_source_kind: str | None
    trajectory_record_count: int
    trajectory_kind_counts: dict[str, int]
    patch_event_count: int
    reviewer_count: int
    reviewer_issue_counts: dict[str, int]
    workspace_changed_path_count: int
    workspace_head_before: str | None
    workspace_head_after: str | None


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )


def _safe_load_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _rel_or_abs(path: Path | None, run_dir: Path) -> str:
    """运行目录内的路径以相对路径展示，否则保留绝对路径。

    ``path`` 为 ``None`` 时返回 ``"-"``；解析失败时回退到原始字符串。
    """
    if path is None:
        return "-"
    try:
        return str(path.resolve().relative_to(run_dir.resolve()))
    except (ValueError, OSError):
        return str(path)


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """逐行解析 JSONL；非字典 / 解析失败的行会被跳过。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                yield obj


# --------------------------------------------------------------------------- #
# 输入发现
# --------------------------------------------------------------------------- #


def discover_inputs(run_dir: Path) -> AnalysisInputs:
    """扫描 ``<run_dir>/artifacts/*`` 并填充 ``AnalysisInputs``。

    纯只读。文件 / 目录必须实际存在才会被填进去；reviewer 列表按 ID 排序。
    """
    run_dir = run_dir.expanduser().resolve()
    artifacts = run_dir / "artifacts"

    summary_json = run_dir / "summary.json"
    handoff_md = artifacts / "handoff.md"
    workspace_diff_json = artifacts / "workspace-diff.json"
    workspace_diff_patch = artifacts / "workspace-diff.patch"
    rvf_traj_dir = artifacts / "trajectory" / "rvf"
    trajectory_jsonl = rvf_traj_dir / "trajectory.jsonl"
    trajectory_index_json = rvf_traj_dir / "trajectory.index.json"
    rvf_rollout_jsonl = rvf_traj_dir / "rollout.codex.jsonl"

    pre_rvf_dir = artifacts / "trajectory" / "pre-rvf"
    pre_manifest = pre_rvf_dir / "manifest.json"

    reviewer_results: list[Path] = []
    reviewer_trajectories: list[Path] = []

    reviewers_dir = artifacts / "reviewers"
    if reviewers_dir.is_dir():
        for entry in sorted(reviewers_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            result = entry / "review-result.json"
            if result.is_file():
                reviewer_results.append(result)

    reviewers_traj_dir = rvf_traj_dir / "reviewers"
    if reviewers_traj_dir.is_dir():
        for entry in sorted(reviewers_traj_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            traj = entry / "trajectory.jsonl"
            if traj.is_file():
                reviewer_trajectories.append(traj)

    return AnalysisInputs(
        run_dir=run_dir,
        summary_json=summary_json if summary_json.is_file() else None,
        handoff_md=handoff_md if handoff_md.is_file() else None,
        workspace_diff_json=workspace_diff_json
        if workspace_diff_json.is_file()
        else None,
        workspace_diff_patch=workspace_diff_patch
        if workspace_diff_patch.is_file()
        else None,
        trajectory_jsonl=trajectory_jsonl if trajectory_jsonl.is_file() else None,
        trajectory_index_json=trajectory_index_json
        if trajectory_index_json.is_file()
        else None,
        rvf_rollout_jsonl=rvf_rollout_jsonl if rvf_rollout_jsonl.is_file() else None,
        pre_rvf_dir=pre_rvf_dir if pre_manifest.is_file() else None,
        reviewer_results=reviewer_results,
        reviewer_trajectories=reviewer_trajectories,
    )


# --------------------------------------------------------------------------- #
# 统计聚合
# --------------------------------------------------------------------------- #


def _reviewer_id_from_result_path(path: Path) -> str:
    """``artifacts/reviewers/<id>/review-result.json`` -> ``<id>``。"""
    return path.parent.name


def _extract_review_issues(payload: Any) -> list[dict[str, Any]]:
    """从 review-result.json 内容中抽取 issues 列表（防御性解析）。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("issues")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _count_trajectory(
    inputs: AnalysisInputs,
) -> tuple[int, dict[str, int], int]:
    """返回 (record_count, kind_counts, patch_event_count)。

    优先信任 ``trajectory.index.json`` 给出的总计数 / kind_counts；缺失或损坏时
    降级到逐行扫描 ``trajectory.jsonl``。``patch_event_count`` 始终需要扫描原始
    NDJSON 才能识别（index 没拆出来）。
    """
    record_count = 0
    kind_counts: dict[str, int] = {}
    patch_event_count = 0

    index_payload = _safe_load_json(inputs.trajectory_index_json)
    if isinstance(index_payload, dict):
        rc = index_payload.get("record_count")
        if isinstance(rc, int) and rc >= 0:
            record_count = rc
        kc = index_payload.get("kind_counts")
        if isinstance(kc, dict):
            kind_counts = {
                str(k): int(v)
                for k, v in kc.items()
                if isinstance(v, (int, bool)) and not isinstance(v, bool)
            }

    traj = inputs.trajectory_jsonl
    if traj is None:
        return record_count, kind_counts, patch_event_count

    fallback_record_count = 0
    fallback_kind_counts: dict[str, int] = {}
    for record in _iter_jsonl(traj):
        fallback_record_count += 1
        kind = record.get("kind")
        if isinstance(kind, str):
            fallback_kind_counts[kind] = fallback_kind_counts.get(kind, 0) + 1
        if (
            kind == "tool_call"
            and record.get("tool") == "apply_patch"
        ):
            refs = record.get("artifact_refs")
            if isinstance(refs, list) and len(refs) > 0:
                patch_event_count += 1

    if record_count == 0:
        record_count = fallback_record_count
    if not kind_counts:
        kind_counts = fallback_kind_counts

    return record_count, kind_counts, patch_event_count


def gather_stats(inputs: AnalysisInputs) -> ScaffoldStats:
    """纯聚合，无 LLM、无判断。容忍输入缺失 / 损坏：以 0 / None 表达。

    设计原则：永远不要 raise；缺数据本身会通过 scaffold 中的零计数显形，由
    ``/rvf-analyze`` skill 决定如何对外呈现。
    """
    summary_payload = _safe_load_json(inputs.summary_json)
    if not isinstance(summary_payload, dict):
        summary_payload = {}
    finalize = summary_payload.get("finalize")
    if not isinstance(finalize, dict):
        finalize = {}
    trajectory_block = finalize.get("trajectory")
    if not isinstance(trajectory_block, dict):
        trajectory_block = {}
    workspace_block = finalize.get("workspace_diff")
    if not isinstance(workspace_block, dict):
        workspace_block = {}

    run_id = summary_payload.get("run_id")
    if not isinstance(run_id, str):
        run_id = None

    decision_kind = finalize.get("decision_kind")
    if not isinstance(decision_kind, str):
        decision_kind = None

    started_at = finalize.get("started_at")
    if not isinstance(started_at, str):
        started_at = None
    completed_at = finalize.get("completed_at")
    if not isinstance(completed_at, str):
        completed_at = None

    pre_kind = trajectory_block.get("pre_rvf_source_kind")
    post_kind = trajectory_block.get("post_rvf_source_kind")
    if not isinstance(pre_kind, str):
        pre_kind = None
    if not isinstance(post_kind, str):
        post_kind = None

    record_count, kind_counts, patch_event_count = _count_trajectory(inputs)

    reviewer_issue_counts: dict[str, int] = {}
    for result_path in inputs.reviewer_results:
        rid = _reviewer_id_from_result_path(result_path)
        payload = _safe_load_json(result_path)
        issues = _extract_review_issues(payload)
        reviewer_issue_counts[rid] = len(issues)

    head_before: str | None = None
    head_after: str | None = None
    changed_path_count = 0

    diff_payload = _safe_load_json(inputs.workspace_diff_json)
    if isinstance(diff_payload, dict):
        hb = diff_payload.get("head_before")
        ha = diff_payload.get("head_after")
        if isinstance(hb, str) and hb:
            head_before = hb
        if isinstance(ha, str) and ha:
            head_after = ha
        changed = diff_payload.get("changed_paths")
        if isinstance(changed, list):
            changed_path_count = len(changed)
    else:
        # 退化到 summary.finalize.workspace_diff 给的 head/path count
        hb = workspace_block.get("head_before")
        ha = workspace_block.get("head_after")
        if isinstance(hb, str) and hb:
            head_before = hb
        if isinstance(ha, str) and ha:
            head_after = ha
        cpc = workspace_block.get("changed_path_count")
        if isinstance(cpc, int) and cpc >= 0:
            changed_path_count = cpc

    return ScaffoldStats(
        run_id=run_id,
        decision_kind=decision_kind,
        finalize_started_at=started_at,
        finalize_completed_at=completed_at,
        pre_rvf_source_kind=pre_kind,
        post_rvf_source_kind=post_kind,
        trajectory_record_count=record_count,
        trajectory_kind_counts=dict(kind_counts),
        patch_event_count=patch_event_count,
        reviewer_count=len(inputs.reviewer_results),
        reviewer_issue_counts=reviewer_issue_counts,
        workspace_changed_path_count=changed_path_count,
        workspace_head_before=head_before,
        workspace_head_after=head_after,
    )


# --------------------------------------------------------------------------- #
# Markdown scaffold
# --------------------------------------------------------------------------- #


def _format_kind_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "（无）"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"`{k}`={v}" for k, v in items)


def _format_reviewer_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "（无）"
    items = sorted(counts.items(), key=lambda kv: kv[0])
    return ", ".join(f"`{k}`={v}" for k, v in items)


def _todo(text: str) -> str:
    return f"<!-- TODO(rvf-analyze): {text} -->"


def scaffold_summary_md(
    inputs: AnalysisInputs,
    stats: ScaffoldStats,
    out_path: Path,
) -> Path:
    """写出 Markdown 骨架。所有节标题为中文 H2，固定顺序。

    每节先列确定性事实，再以 ``<!-- TODO(rvf-analyze): ... -->`` 注释提示 LLM
    需要补的部分。原子写（``.tmp`` + ``os.replace``）。
    """
    run_dir = inputs.run_dir
    lines: list[str] = []

    lines.append(f"# RVF 复盘 — {stats.run_id or '(未知 run_id)'}")
    lines.append("")
    lines.append(f"_生成时间：{_utc_now()}_")
    lines.append("")

    # 概览
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- run_id: `{stats.run_id or '-'}`")
    lines.append(f"- run_dir: `{run_dir}`")
    lines.append(f"- decision_kind: `{stats.decision_kind or '-'}`")
    lines.append(
        f"- finalize 开始: `{stats.finalize_started_at or '-'}` / 结束: "
        f"`{stats.finalize_completed_at or '-'}`"
    )
    lines.append(f"- summary.json: `{_rel_or_abs(inputs.summary_json, run_dir)}`")
    lines.append(f"- handoff.md: `{_rel_or_abs(inputs.handoff_md, run_dir)}`")
    lines.append("")
    lines.append(_todo("用 1-2 段叙述本次 RVF 的诱因、最终走向（handoff/cancel/...）和最显著的成果或风险"))
    lines.append("")

    # 触发上下文 (pre-RVF)
    lines.append("## 触发上下文 (pre-RVF)")
    lines.append("")
    lines.append(f"- pre_rvf_source_kind: `{stats.pre_rvf_source_kind or '-'}`")
    lines.append(f"- pre-rvf 目录: `{_rel_or_abs(inputs.pre_rvf_dir, run_dir)}`")
    lines.append("")
    lines.append(_todo("基于 pre-rvf 轨迹概括触发 RVF 时父会话正在做什么、暴露了哪些线索"))
    lines.append("")

    # RVF 自身轨迹
    lines.append("## RVF 自身轨迹")
    lines.append("")
    lines.append(f"- trajectory.jsonl: `{_rel_or_abs(inputs.trajectory_jsonl, run_dir)}`")
    lines.append(
        f"- trajectory.index.json: `{_rel_or_abs(inputs.trajectory_index_json, run_dir)}`"
    )
    lines.append(f"- post_rvf_source_kind: `{stats.post_rvf_source_kind or '-'}`")
    lines.append(f"- 总记录数: `{stats.trajectory_record_count}`")
    lines.append(
        f"- 各 kind 计数: {_format_kind_counts(stats.trajectory_kind_counts)}"
    )
    lines.append(f"- apply_patch 事件: `{stats.patch_event_count}`")
    lines.append("")
    lines.append(_todo("梳理 RVF 自身 agent 的关键阶段（review/validate/fix/handoff）和 patch 时间线"))
    lines.append("")

    # Reviewer 发现
    lines.append("## Reviewer 发现")
    lines.append("")
    lines.append(f"- reviewer_count: `{stats.reviewer_count}`")
    lines.append(
        f"- 每位 reviewer 的 issue 数: {_format_reviewer_counts(stats.reviewer_issue_counts)}"
    )
    if inputs.reviewer_results:
        lines.append("- review-result.json:")
        for path in inputs.reviewer_results:
            lines.append(f"  - `{_rel_or_abs(path, run_dir)}`")
    if inputs.reviewer_trajectories:
        lines.append("- reviewer trajectory.jsonl:")
        for path in inputs.reviewer_trajectories:
            lines.append(f"  - `{_rel_or_abs(path, run_dir)}`")
    lines.append("")
    lines.append(_todo("逐 reviewer 抽取主要 issue 类别、误报模式与遗漏，配合 causality.json 的 issues[] 一起读"))
    lines.append("")

    # 工作区改动
    lines.append("## 工作区改动")
    lines.append("")
    lines.append(
        f"- workspace-diff.json: `{_rel_or_abs(inputs.workspace_diff_json, run_dir)}`"
    )
    lines.append(
        f"- workspace-diff.patch: `{_rel_or_abs(inputs.workspace_diff_patch, run_dir)}`"
    )
    lines.append(f"- head_before: `{stats.workspace_head_before or '-'}`")
    lines.append(f"- head_after: `{stats.workspace_head_after or '-'}`")
    lines.append(f"- changed_paths: `{stats.workspace_changed_path_count}`")
    lines.append("")
    lines.append(_todo("总结 RVF 真正改了哪些文件 / 哪些是新增 vs 修改 vs 删除，并指出与 issue 的对应关系"))
    lines.append("")

    # 待 LLM 补全的叙事
    lines.append("## 待 LLM 补全的叙事")
    lines.append("")
    lines.append(_todo("在这里给出整体故事线：哪些 issue 真的转化成了 patch、哪些没有、为什么；本次 RVF 是否值得"))
    lines.append("")
    lines.append(_todo("如果发现可改进的 prompt / 工具行为，列出 follow-up 建议"))
    lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    _atomic_write_text(out_path, text)
    return out_path


# --------------------------------------------------------------------------- #
# causality.json scaffold
# --------------------------------------------------------------------------- #


def _issue_summary_text(item: dict[str, Any]) -> str:
    """在多种可能的字段名里挑选 issue 的人类可读描述。"""
    for key in ("summary", "title", "message", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if len(cleaned) > 200:
                return cleaned[:200]
            return cleaned
    return ""


def _issue_id(item: dict[str, Any], index: int) -> str:
    for key in ("id", "issue_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return f"issue-{index}"


def _issue_kind(item: dict[str, Any]) -> str:
    value = item.get("kind")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _issue_severity(item: dict[str, Any]) -> str:
    for key in ("severity", "confidence"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _collect_issues(inputs: AnalysisInputs) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result_path in inputs.reviewer_results:
        reviewer_id = _reviewer_id_from_result_path(result_path)
        payload = _safe_load_json(result_path)
        issues = _extract_review_issues(payload)
        for index, item in enumerate(issues):
            out.append(
                {
                    "reviewer_id": reviewer_id,
                    "issue_id": _issue_id(item, index),
                    "kind": _issue_kind(item),
                    "severity": _issue_severity(item),
                    "summary": _issue_summary_text(item),
                    "candidate_patch_call_ids": [],
                }
            )
    return out


def _collect_patches(inputs: AnalysisInputs) -> list[dict[str, Any]]:
    if inputs.trajectory_jsonl is None:
        return []
    out: list[dict[str, Any]] = []
    for record in _iter_jsonl(inputs.trajectory_jsonl):
        if record.get("kind") != "tool_call":
            continue
        if record.get("tool") != "apply_patch":
            continue
        refs = record.get("artifact_refs")
        if not isinstance(refs, list) or not refs:
            continue
        clean_refs: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            clean_refs.append(
                {
                    "path": ref.get("path"),
                    "lines": ref.get("lines"),
                    "op": ref.get("op"),
                }
            )
        raw_ref = record.get("raw_ref")
        line_no: int | None = None
        if isinstance(raw_ref, dict):
            ln = raw_ref.get("line")
            if isinstance(ln, int):
                line_no = ln
        out.append(
            {
                "call_id": record.get("call_id"),
                "ts": record.get("ts"),
                "tool": "apply_patch",
                "artifact_refs": clean_refs,
                "trajectory_line": line_no,
            }
        )
    return out


def scaffold_causality_json(
    inputs: AnalysisInputs,
    stats: ScaffoldStats,
    out_path: Path,
) -> Path:
    """写出 causality.json scaffold。两份列表都允许为空，缺源不会 raise。"""
    payload = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "run_id": stats.run_id,
        "generated_at": _utc_now(),
        "issues": _collect_issues(inputs),
        "patches": _collect_patches(inputs),
    }
    _atomic_write_json(out_path, payload)
    return out_path


# --------------------------------------------------------------------------- #
# 顶层入口
# --------------------------------------------------------------------------- #


def scaffold_run(run_dir: Path) -> dict[str, Any]:
    """便捷入口：discover → gather → 写两份 scaffold。"""
    inputs = discover_inputs(run_dir)
    stats = gather_stats(inputs)
    analysis_dir = inputs.run_dir / "artifacts" / ANALYSIS_DIR_NAME
    summary_md_path = analysis_dir / "summary.md"
    causality_json_path = analysis_dir / "causality.json"
    scaffold_summary_md(inputs, stats, summary_md_path)
    scaffold_causality_json(inputs, stats, causality_json_path)
    return {
        "summary_md_path": summary_md_path,
        "causality_json_path": causality_json_path,
        "stats_dict": {
            "run_id": stats.run_id,
            "decision_kind": stats.decision_kind,
            "finalize_started_at": stats.finalize_started_at,
            "finalize_completed_at": stats.finalize_completed_at,
            "pre_rvf_source_kind": stats.pre_rvf_source_kind,
            "post_rvf_source_kind": stats.post_rvf_source_kind,
            "trajectory_record_count": stats.trajectory_record_count,
            "trajectory_kind_counts": dict(stats.trajectory_kind_counts),
            "patch_event_count": stats.patch_event_count,
            "reviewer_count": stats.reviewer_count,
            "reviewer_issue_counts": dict(stats.reviewer_issue_counts),
            "workspace_changed_path_count": stats.workspace_changed_path_count,
            "workspace_head_before": stats.workspace_head_before,
            "workspace_head_after": stats.workspace_head_after,
        },
    }
