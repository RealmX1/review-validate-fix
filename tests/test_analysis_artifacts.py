#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 合成 fixtures
# --------------------------------------------------------------------------- #


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _make_summary(run_id: str = "rvf-run-1") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "completed",
        "finalize": {
            "schema_version": 1,
            "decision_kind": "handoff",
            "started_at": "2026-05-04T01:00:00Z",
            "completed_at": "2026-05-04T01:05:00Z",
            "trajectory": {
                "trajectory_dir": "...",
                "pre_rvf_source_kind": "same-session-slice",
                "post_rvf_source_kind": "same-session-slice",
                "reviewers": ["santa", "elf"],
            },
            "workspace_diff": {
                "status": "complete",
                "head_before": "abc123",
                "head_after": "def456",
                "changed_path_count": 2,
            },
        },
    }


def _trajectory_records() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:01Z",
            "source": "codex",
            "kind": "phase_marker",
            "marker": "session_meta",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 1, "byte_range": [0, 10]},
            "summary": "session_meta",
            "artifact_refs": [],
        },
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:02Z",
            "source": "codex",
            "kind": "message",
            "role": "user",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 2},
            "summary": "user msg",
            "artifact_refs": [],
        },
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:03Z",
            "source": "codex",
            "kind": "tool_call",
            "tool": "exec_command",
            "call_id": "exec-1",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 3},
            "summary": "ls",
            "artifact_refs": [],
        },
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:04Z",
            "source": "codex",
            "kind": "tool_call",
            "tool": "apply_patch",
            "call_id": "patch-A",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 4},
            "summary": "apply_patch",
            "artifact_refs": [
                {"path": "src/foo.py", "lines": [1, 8], "op": "edit"},
            ],
        },
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:05Z",
            "source": "codex",
            "kind": "tool_call",
            "tool": "apply_patch",
            "call_id": "patch-B",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 5},
            "summary": "apply_patch",
            "artifact_refs": [
                {"path": "src/bar.py", "lines": [10, 12], "op": "create"},
            ],
        },
        # apply_patch with empty artifact_refs — should NOT count as a patch event
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:06Z",
            "source": "codex",
            "kind": "tool_call",
            "tool": "apply_patch",
            "call_id": "patch-empty",
            "raw_ref": {"file": "rollout.codex.jsonl", "line": 6},
            "summary": "apply_patch (no refs)",
            "artifact_refs": [],
        },
    ]


def _build_run(tmp_path: Path, *, with_summary: bool = True) -> Path:
    """Build a synthetic finalized run_dir with a comprehensive set of artifacts."""
    run_dir = tmp_path / "run-001"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)

    if with_summary:
        _write_json(run_dir / "summary.json", _make_summary("rvf-run-1"))

    # handoff.md
    (artifacts / "handoff.md").write_text("# Handoff\n", encoding="utf-8")

    # trajectory layout
    rvf_dir = artifacts / "trajectory" / "rvf"
    rvf_dir.mkdir(parents=True)
    records = _trajectory_records()
    _write_jsonl(rvf_dir / "trajectory.jsonl", records)
    # index says 6 records; kind_counts: phase_marker=1, message=1, tool_call=4
    _write_json(
        rvf_dir / "trajectory.index.json",
        {
            "schema_version": 1,
            "rollout_file": "rollout.codex.jsonl",
            "record_count": 6,
            "kind_counts": {
                "phase_marker": 1,
                "message": 1,
                "tool_call": 4,
            },
        },
    )
    (rvf_dir / "rollout.codex.jsonl").write_text("placeholder\n", encoding="utf-8")

    # pre-rvf manifest
    pre_dir = artifacts / "trajectory" / "pre-rvf"
    pre_dir.mkdir(parents=True)
    _write_json(
        pre_dir / "manifest.json",
        {"schema_version": 1, "source_kind": "same-session-slice"},
    )

    # workspace-diff
    _write_json(
        artifacts / "workspace-diff.json",
        {
            "schema_version": 1,
            "status": "complete",
            "head_before": "abc123",
            "head_after": "def456",
            "changed_paths": [
                {"path": "src/foo.py", "op": "modified"},
                {"path": "src/bar.py", "op": "added"},
            ],
            "git_diff_path": str(artifacts / "workspace-diff.patch"),
        },
    )
    (artifacts / "workspace-diff.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")

    # reviewers
    santa_dir = artifacts / "reviewers" / "santa"
    elf_dir = artifacts / "reviewers" / "elf"
    _write_json(
        santa_dir / "review-result.json",
        {
            "schema_version": 1,
            "kind": "issues",
            "issues": [
                {
                    "id": "santa-1",
                    "kind": "REAL",
                    "severity": "high",
                    "summary": "Null pointer in foo.py",
                    "path": "src/foo.py",
                    "line": 5,
                    "message": "Null pointer in foo.py",
                },
                {
                    "id": "santa-2",
                    "kind": "NIT",
                    "severity": "low",
                    "summary": "Style nit",
                    "path": "src/bar.py",
                    "line": 3,
                    "message": "Style nit",
                },
            ],
        },
    )
    _write_json(
        elf_dir / "review-result.json",
        {
            "schema_version": 1,
            "kind": "issues",
            "issues": [
                {
                    "id": "elf-1",
                    "kind": "REAL",
                    "severity": "medium",
                    "title": "Possible race",
                    "path": "src/race.py",
                    "line": 9,
                    "message": "Race condition risk",
                },
            ],
        },
    )

    # reviewer trajectories
    rev_traj_root = rvf_dir / "reviewers"
    (rev_traj_root / "santa").mkdir(parents=True)
    (rev_traj_root / "elf").mkdir(parents=True)
    _write_jsonl(rev_traj_root / "santa" / "trajectory.jsonl", [{"kind": "message"}])
    _write_jsonl(rev_traj_root / "elf" / "trajectory.jsonl", [{"kind": "message"}])

    return run_dir


# --------------------------------------------------------------------------- #
# discover_inputs
# --------------------------------------------------------------------------- #


def test_discover_inputs_populates_only_existing(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = tmp_path / "bare-run"
    (run_dir / "artifacts").mkdir(parents=True)
    inputs = mod.discover_inputs(run_dir)
    assert inputs.run_dir == run_dir.resolve()
    assert inputs.summary_json is None
    assert inputs.handoff_md is None
    assert inputs.workspace_diff_json is None
    assert inputs.workspace_diff_patch is None
    assert inputs.trajectory_jsonl is None
    assert inputs.trajectory_index_json is None
    assert inputs.rvf_rollout_jsonl is None
    assert inputs.pre_rvf_dir is None
    assert inputs.reviewer_results == []
    assert inputs.reviewer_trajectories == []


def test_discover_inputs_finds_multiple_reviewers_sorted(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)

    assert inputs.summary_json is not None
    assert inputs.handoff_md is not None
    assert inputs.workspace_diff_json is not None
    assert inputs.workspace_diff_patch is not None
    assert inputs.trajectory_jsonl is not None
    assert inputs.trajectory_index_json is not None
    assert inputs.rvf_rollout_jsonl is not None
    assert inputs.pre_rvf_dir is not None

    reviewer_ids = [p.parent.name for p in inputs.reviewer_results]
    assert reviewer_ids == sorted(reviewer_ids)
    assert reviewer_ids == ["elf", "santa"]
    rev_traj_ids = [p.parent.name for p in inputs.reviewer_trajectories]
    assert rev_traj_ids == sorted(rev_traj_ids)
    assert rev_traj_ids == ["elf", "santa"]


# --------------------------------------------------------------------------- #
# gather_stats
# --------------------------------------------------------------------------- #


def test_gather_stats_counts_trajectory_and_separates_patches(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    # index says 6 records; we keep that
    assert stats.trajectory_record_count == 6
    # kind_counts come from trajectory.index.json
    assert stats.trajectory_kind_counts == {
        "phase_marker": 1,
        "message": 1,
        "tool_call": 4,
    }
    # 2 apply_patch tool_calls have non-empty artifact_refs
    assert stats.patch_event_count == 2


def test_gather_stats_survives_missing_summary(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path, with_summary=False)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    # No summary -> all summary-derived fields are None
    assert stats.run_id is None
    assert stats.decision_kind is None
    assert stats.finalize_started_at is None
    assert stats.finalize_completed_at is None
    assert stats.pre_rvf_source_kind is None
    assert stats.post_rvf_source_kind is None
    # trajectory + workspace stats still derived from their own files
    assert stats.trajectory_record_count == 6
    assert stats.workspace_changed_path_count == 2


def test_gather_stats_reads_finalize_block(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.run_id == "rvf-run-1"
    assert stats.decision_kind == "handoff"
    assert stats.finalize_started_at == "2026-05-04T01:00:00Z"
    assert stats.finalize_completed_at == "2026-05-04T01:05:00Z"
    assert stats.pre_rvf_source_kind == "same-session-slice"
    assert stats.post_rvf_source_kind == "same-session-slice"
    assert stats.workspace_head_before == "abc123"
    assert stats.workspace_head_after == "def456"


def test_gather_stats_per_reviewer_issue_counts(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.reviewer_count == 2
    assert stats.reviewer_issue_counts == {"santa": 2, "elf": 1}


# --------------------------------------------------------------------------- #
# scaffold_summary_md
# --------------------------------------------------------------------------- #


def test_scaffold_summary_md_has_all_required_headers(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "summary.md"
    written = mod.scaffold_summary_md(inputs, stats, out_path)
    assert written == out_path
    text = out_path.read_text(encoding="utf-8")
    for header in (
        "## 概览",
        "## 触发上下文 (pre-RVF)",
        "## RVF 自身轨迹",
        "## Reviewer 发现",
        "## 工作区改动",
        "## 待 LLM 补全的叙事",
    ):
        assert header in text, f"missing header: {header}"
    assert "<!-- TODO(rvf-analyze):" in text


def test_scaffold_summary_md_writes_run_id_and_counts(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "summary.md"
    mod.scaffold_summary_md(inputs, stats, out_path)
    text = out_path.read_text(encoding="utf-8")
    # run_id 应出现在概览节
    overview_start = text.index("## 概览")
    next_section = text.index("## 触发上下文 (pre-RVF)")
    overview_block = text[overview_start:next_section]
    assert "rvf-run-1" in overview_block
    assert "handoff" in overview_block
    # trajectory record count 应出现在 RVF 自身轨迹节
    rvf_start = text.index("## RVF 自身轨迹")
    reviewer_start = text.index("## Reviewer 发现")
    rvf_block = text[rvf_start:reviewer_start]
    assert "6" in rvf_block
    assert "apply_patch" in rvf_block.lower() or "apply_patch" in rvf_block
    # 各 kind 的计数也应出现
    assert "tool_call" in rvf_block


# --------------------------------------------------------------------------- #
# scaffold_causality_json
# --------------------------------------------------------------------------- #


def test_scaffold_causality_json_schema_and_real_call_ids(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    # top-level keys
    for key in ("schema_version", "run_id", "generated_at", "issues", "patches"):
        assert key in payload, f"missing key: {key}"
    assert payload["schema_version"] == mod.ANALYSIS_SCHEMA_VERSION
    assert payload["run_id"] == "rvf-run-1"
    assert isinstance(payload["generated_at"], str)
    assert payload["generated_at"].endswith("Z")

    # issues
    issues = payload["issues"]
    assert isinstance(issues, list)
    assert len(issues) == 3  # 2 santa + 1 elf
    for item in issues:
        assert "candidate_patch_call_ids" in item
        assert item["candidate_patch_call_ids"] == []
        assert "reviewer_id" in item
        assert "issue_id" in item
        assert "kind" in item
        assert "summary" in item
    reviewer_ids = sorted({item["reviewer_id"] for item in issues})
    assert reviewer_ids == ["elf", "santa"]

    # patches: only the 2 with non-empty artifact_refs
    patches = payload["patches"]
    assert isinstance(patches, list)
    assert len(patches) == 2
    call_ids = [p["call_id"] for p in patches]
    assert "patch-A" in call_ids
    assert "patch-B" in call_ids
    assert "patch-empty" not in call_ids
    for patch in patches:
        assert patch["tool"] == "apply_patch"
        assert isinstance(patch["artifact_refs"], list)
        assert isinstance(patch.get("trajectory_line"), int)


def test_scaffold_causality_json_empty_when_sources_missing(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = tmp_path / "empty-run"
    (run_dir / "artifacts").mkdir(parents=True)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["issues"] == []
    assert payload["patches"] == []
    assert payload["run_id"] is None


# --------------------------------------------------------------------------- #
# scaffold_run end-to-end
# --------------------------------------------------------------------------- #


def test_scaffold_run_end_to_end(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    result = mod.scaffold_run(run_dir)
    summary_md = result["summary_md_path"]
    causality_json = result["causality_json_path"]
    assert isinstance(summary_md, Path)
    assert isinstance(causality_json, Path)
    expected_dir = run_dir.resolve() / "artifacts" / "analysis"
    assert summary_md == expected_dir / "summary.md"
    assert causality_json == expected_dir / "causality.json"
    assert summary_md.is_file()
    assert causality_json.is_file()
    stats_dict = result["stats_dict"]
    assert stats_dict["run_id"] == "rvf-run-1"
    assert stats_dict["patch_event_count"] == 2
    assert stats_dict["reviewer_count"] == 2


# --------------------------------------------------------------------------- #
# Atomic write smoke test
# --------------------------------------------------------------------------- #


def test_scaffold_summary_md_atomic_replace(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    out_path = run_dir / "artifacts" / "analysis" / "summary.md"

    stats_a = mod.gather_stats(inputs)
    mod.scaffold_summary_md(inputs, stats_a, out_path)
    first_text = out_path.read_text(encoding="utf-8")
    assert "rvf-run-1" in first_text

    # Now rewrite with a different stats payload
    stats_b = mod.ScaffoldStats(
        run_id="REPLACED-RUN",
        decision_kind="cancel",
        finalize_started_at=None,
        finalize_completed_at=None,
        pre_rvf_source_kind=None,
        post_rvf_source_kind=None,
        trajectory_record_count=0,
        trajectory_kind_counts={},
        patch_event_count=0,
        reviewer_count=0,
        reviewer_issue_counts={},
        workspace_changed_path_count=0,
        workspace_head_before=None,
        workspace_head_after=None,
    )
    mod.scaffold_summary_md(inputs, stats_b, out_path)
    second_text = out_path.read_text(encoding="utf-8")

    # Full replacement: old run_id must be gone; new one present.
    assert "REPLACED-RUN" in second_text
    assert "rvf-run-1" not in second_text
    # No leftover .tmp file in the analysis directory.
    leftover = list(out_path.parent.glob(".*.tmp"))
    assert leftover == []
