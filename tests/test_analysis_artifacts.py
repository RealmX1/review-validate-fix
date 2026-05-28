#!/usr/bin/env python3
from __future__ import annotations

import json
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


from _rvf_test_support.loader import load_script_module as _load


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
            "raw_ref": {"file": "rollout.jsonl", "line": 1, "byte_range": [0, 10]},
            "summary": "session_meta",
            "artifact_refs": [],
        },
        {
            "schema_version": 1,
            "ts": "2026-05-04T01:00:02Z",
            "source": "codex",
            "kind": "message",
            "role": "user",
            "raw_ref": {"file": "rollout.jsonl", "line": 2},
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
            "raw_ref": {"file": "rollout.jsonl", "line": 3},
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
            "raw_ref": {"file": "rollout.jsonl", "line": 4},
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
            "raw_ref": {"file": "rollout.jsonl", "line": 5},
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
            "raw_ref": {"file": "rollout.jsonl", "line": 6},
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
            "rollout_file": "rollout.jsonl",
            "record_count": 6,
            "kind_counts": {
                "phase_marker": 1,
                "message": 1,
                "tool_call": 4,
            },
        },
    )
    (rvf_dir / "rollout.jsonl").write_text("placeholder\n", encoding="utf-8")

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
# subagent integration
# --------------------------------------------------------------------------- #


def _add_subagent(
    run_dir: Path,
    *,
    agent_id: str,
    role: str,
    nickname: str,
    patch_records: list[dict[str, Any]] | None = None,
) -> None:
    """Drop a captured subagent layout into the run's trajectory tree."""
    sub_dir = run_dir / "artifacts" / "trajectory" / "rvf" / "subagents" / agent_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        sub_dir / "manifest.json",
        {
            "schema_version": 1,
            "status": "ok",
            "spawn": {
                "agent_id": agent_id,
                "spawn_call_id": f"call_spawn_{agent_id}",
                "role": role,
                "nickname": nickname,
                "spawned_at": "2026-05-04T01:02:00Z",
                "main_rollout_line_index": 7,
                "prompt": "stub prompt",
            },
        },
    )
    records: list[dict[str, Any]] = patch_records if patch_records is not None else []
    _write_jsonl(sub_dir / "trajectory.jsonl", records)


def test_discover_inputs_picks_up_subagents(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    _add_subagent(run_dir, agent_id="agent-A", role="explorer", nickname="Faraday")
    _add_subagent(run_dir, agent_id="agent-B", role="worker", nickname="Tesla")

    inputs = mod.discover_inputs(run_dir)
    assert [s.agent_id for s in inputs.subagents] == ["agent-A", "agent-B"]
    assert inputs.subagents[0].manifest["spawn"]["role"] == "explorer"


def test_collect_patches_merges_subagent_patches_and_tags_source(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    # validate-fix subagent contributes a patch AFTER the main-rollout patches (ts later).
    _add_subagent(
        run_dir,
        agent_id="agent-fixer",
        role="worker",
        nickname="Tesla",
        patch_records=[
            {
                "schema_version": 1,
                "ts": "2026-05-04T02:00:00Z",
                "source": "codex",
                "kind": "tool_call",
                "tool": "apply_patch",
                "call_id": "subagent_patch_1",
                "raw_ref": {"file": "rollout.jsonl", "line": 12},
                "summary": "fix patch",
                "artifact_refs": [
                    {"path": "src/foo.py", "lines": [5, 5], "op": "edit"},
                ],
            }
        ],
    )

    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    patches = payload["patches"]
    # Sorted by ts; main rollout's two patches (01:00:04 / 01:00:05) precede subagent's 02:00.
    sources = [p["source_agent_id"] for p in patches]
    assert sources == [None, None, "agent-fixer"]
    fix_patch = next(p for p in patches if p["source_agent_id"] == "agent-fixer")
    assert fix_patch["call_id"] == "subagent_patch_1"
    assert fix_patch["artifact_refs"][0]["path"] == "src/foo.py"

    # Stats must surface subagent contributions distinctly from main rollout.
    assert stats.subagent_count == 1
    assert stats.subagent_patch_event_count == 1
    assert stats.patch_event_count == 2  # main rollout still has 2 patches


def test_summary_md_reports_subagent_section(tmp_path: Path) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    _add_subagent(
        run_dir,
        agent_id="agent-fixer",
        role="worker",
        nickname="Tesla",
        patch_records=[
            {
                "schema_version": 1,
                "ts": "2026-05-04T02:00:00Z",
                "kind": "tool_call",
                "tool": "apply_patch",
                "call_id": "subagent_patch_1",
                "artifact_refs": [{"path": "src/foo.py", "op": "edit", "lines": [5, 5]}],
            }
        ],
    )
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "summary.md"
    mod.scaffold_summary_md(inputs, stats, out_path)
    text = out_path.read_text(encoding="utf-8")
    assert "spawn_agent 子代理" in text
    assert "agent-fixer" in text
    assert "worker" in text


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
        "## Follow-up 建议",
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
    # 标签已 host-中性化（write-op 归一），不再写死 apply_patch
    assert "write-op" in rvf_block
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
    assert "causality_ledger_missing" not in payload["diagnostics"]


def test_scaffold_causality_json_clean_path_does_not_emit_ledger_missing(
    tmp_path: Path,
) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert "causality_ledger_missing" not in payload["diagnostics"]


def test_scaffold_summary_classifies_changed_paths_against_scope_contract(
    tmp_path: Path,
) -> None:
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)
    inputs_dir = run_dir / "artifacts" / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = run_dir / "artifacts" / "review-packet.metadata.json"
    metadata_path.write_text(
        json.dumps({"session_owned_paths": ["session/b.py"]}),
        encoding="utf-8",
    )
    (inputs_dir / "scope.contract.json").write_text(
        json.dumps(
            {
                "fix_allowlist": ["allowed/a.py"],
                "primary_files": ["allowed/a.py"],
                "review_packet_metadata_path": str(metadata_path),
                "canonical_scope": {
                    "fix_allowlist": ["allowed/a.py"],
                    "primary_files": ["allowed/a.py"],
                },
            }
        ),
        encoding="utf-8",
    )
    diff_path = run_dir / "artifacts" / "workspace-diff.json"
    diff_path.write_text(
        json.dumps(
            {
                "head_before": "abc",
                "head_after": "abc",
                "changed_paths": [
                    "allowed/a.py",
                    "session/b.py",
                    "background/c.py",
                ],
            }
        ),
        encoding="utf-8",
    )
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    out_path = run_dir / "artifacts" / "analysis" / "summary.md"
    mod.scaffold_summary_md(inputs, stats, out_path)
    text = out_path.read_text(encoding="utf-8")
    assert "changed_paths × scope contract:" in text
    assert "`in_fix_allowlist` (1):" in text
    assert "`session_owned` (1):" in text
    assert "`background_wip` (1):" in text
    assert "    - `allowed/a.py`" in text
    assert "    - `session/b.py`" in text
    assert "    - `background/c.py`" in text


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
        subagent_count=0,
        subagent_patch_event_count=0,
        reviewer_count=0,
        reviewer_issue_counts={},
        workspace_changed_path_count=0,
        workspace_head_before=None,
        workspace_head_after=None,
        trajectory_window_start=None,
    )
    mod.scaffold_summary_md(inputs, stats_b, out_path)
    second_text = out_path.read_text(encoding="utf-8")

    # Full replacement: old run_id must be gone; new one present.
    assert "REPLACED-RUN" in second_text
    assert "rvf-run-1" not in second_text
    # No leftover .tmp file in the analysis directory.
    leftover = list(out_path.parent.glob(".*.tmp"))
    assert leftover == []


# --------------------------------------------------------------------------- #
# S1.5 / A1 — host-无关 write-op 归一计数（Claude Edit/Write/MultiEdit）
# --------------------------------------------------------------------------- #


def _build_claude_run(
    tmp_path: Path,
    *,
    records: list[dict[str, Any]],
    post_kind: str = "same-session-slice",
    timestamp: str | None = None,
    run_id: str = "rvf-20260528T120000Z-claude",
    write_index: bool = False,
) -> Path:
    """最小 Claude-host run：summary + trajectory（无 index，强制扫描计数）。"""
    run_dir = tmp_path / "claude-run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": "completed",
        "finalize": {
            "schema_version": 1,
            "decision_kind": "handoff",
            "started_at": "2026-05-28T12:01:00Z",
            "completed_at": "2026-05-28T12:05:00Z",
            "trajectory": {
                "pre_rvf_source_kind": post_kind,
                "post_rvf_source_kind": post_kind,
            },
        },
    }
    if timestamp is not None:
        summary["timestamp"] = timestamp
    _write_json(run_dir / "summary.json", summary)
    rvf_dir = artifacts / "trajectory" / "rvf"
    rvf_dir.mkdir(parents=True)
    _write_jsonl(rvf_dir / "trajectory.jsonl", records)
    if write_index:
        # index 故意给"错"的全量计数，用来证明窗口化时被忽略。
        _write_json(
            rvf_dir / "trajectory.index.json",
            {"schema_version": 1, "record_count": len(records), "kind_counts": {}},
        )
    return run_dir


def _claude_write_op(ts: str, tool: str, call_id: str, path: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": ts,
        "source": "claude",
        "kind": "tool_call",
        "tool": tool,
        "call_id": call_id,
        "raw_ref": {"file": "transcript.jsonl", "line": 0},
        "summary": f"{tool} {path}",
        "artifact_refs": [{"path": path, "lines": None, "op": "edit"}],
    }


def _claude_readonly(ts: str, tool: str, call_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": ts,
        "source": "claude",
        "kind": "tool_call",
        "tool": tool,
        "call_id": call_id,
        "raw_ref": {"file": "transcript.jsonl", "line": 0},
        "summary": tool,
        "artifact_refs": [],
    }


def test_write_op_count_normalizes_claude_edit_write_multiedit(tmp_path: Path) -> None:
    """A1: Claude Edit/Write/MultiEdit 计入 write-op；Read/Bash(无 patch) 不计。"""
    mod = _load("analysis_artifacts")
    records = [
        _claude_write_op("2026-05-28T12:02:01Z", "Edit", "toolu_edit", "src/a.py"),
        _claude_write_op("2026-05-28T12:02:02Z", "Write", "toolu_write", "src/b.py"),
        _claude_write_op("2026-05-28T12:02:03Z", "MultiEdit", "toolu_multi", "src/c.py"),
        _claude_readonly("2026-05-28T12:02:04Z", "Read", "toolu_read"),
        _claude_readonly("2026-05-28T12:02:05Z", "Bash", "toolu_bash"),
    ]
    run_dir = _build_claude_run(tmp_path, records=records)
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    # 3 个 Claude write-op 被计入（旧逻辑 tool=="apply_patch" 会得 0）。
    assert stats.patch_event_count == 3
    assert stats.trajectory_window_start is None  # same-session-slice → 不窗口化

    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    patches = payload["patches"]
    assert len(patches) == 3
    # patches 保留真实工具名（不再硬编码 apply_patch）+ 真实 call_id（handoff B 主轨迹）。
    by_call = {p["call_id"]: p for p in patches}
    assert by_call["toolu_edit"]["tool"] == "Edit"
    assert by_call["toolu_write"]["tool"] == "Write"
    assert by_call["toolu_multi"]["tool"] == "MultiEdit"
    assert "toolu_read" not in by_call
    assert "toolu_bash" not in by_call


def test_write_op_count_codex_apply_patch_unchanged(tmp_path: Path) -> None:
    """A1 回归护栏：纯 Codex apply_patch fixture 计数与改造前一致（仍 == 2）。"""
    mod = _load("analysis_artifacts")
    run_dir = _build_run(tmp_path)  # Codex fixture: 2 apply_patch w/ refs + 1 empty
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.patch_event_count == 2


# --------------------------------------------------------------------------- #
# S1.5 / C — same-session-full 子区间窗口化计数
# --------------------------------------------------------------------------- #


def _windowed_records() -> list[dict[str, Any]]:
    """2 条触发前（2026-05-01）+ 3 条窗口内（2026-05-28T12:30+）。"""
    return [
        {
            "schema_version": 1,
            "ts": "2026-05-01T00:00:00Z",
            "source": "claude",
            "kind": "message",
            "role": "user",
            "raw_ref": {"file": "t.jsonl", "line": 0},
            "summary": "pre-trigger impl work",
            "artifact_refs": [],
        },
        _claude_write_op("2026-05-01T00:01:00Z", "Edit", "pre_edit", "old/x.py"),
        {
            "schema_version": 1,
            "ts": "2026-05-28T12:30:00Z",
            "source": "claude",
            "kind": "message",
            "role": "assistant",
            "raw_ref": {"file": "t.jsonl", "line": 0},
            "summary": "rvf subflow",
            "artifact_refs": [],
        },
        _claude_write_op("2026-05-28T12:31:00Z", "Edit", "rvf_edit_1", "fix/a.py"),
        _claude_write_op("2026-05-28T12:32:00Z", "Write", "rvf_write_1", "fix/b.py"),
    ]


def test_same_session_full_windows_to_rvf_subinterval(tmp_path: Path) -> None:
    """C: same-session-full 时只计 ts ≥ summary.timestamp 的 RVF 子区间。"""
    mod = _load("analysis_artifacts")
    run_dir = _build_claude_run(
        tmp_path,
        records=_windowed_records(),
        post_kind="same-session-full",
        timestamp="2026-05-28T12:00:00Z",
        write_index=True,  # index 给全量 5，证明窗口化时被忽略
    )
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.trajectory_window_start == "2026-05-28T12:00:00Z"
    # 只计窗口内 3 条（2 触发前被排除），不是 index 的 5。
    assert stats.trajectory_record_count == 3
    assert stats.trajectory_kind_counts == {"message": 1, "tool_call": 2}
    # 窗口内 2 个 write-op（pre_edit 被排除）。
    assert stats.patch_event_count == 2

    out_path = run_dir / "artifacts" / "analysis" / "causality.json"
    mod.scaffold_causality_json(inputs, stats, out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    call_ids = {p["call_id"] for p in payload["patches"]}
    assert call_ids == {"rvf_edit_1", "rvf_write_1"}
    assert "pre_edit" not in call_ids

    # summary.md 应注明窗口化。
    summary_md = run_dir / "artifacts" / "analysis" / "summary.md"
    mod.scaffold_summary_md(inputs, stats, summary_md)
    text = summary_md.read_text(encoding="utf-8")
    assert "窗口化" in text
    assert "2026-05-28T12:00:00Z" in text


def test_same_session_slice_not_windowed(tmp_path: Path) -> None:
    """控制组：非 same-session-full 不窗口化，全量计数。"""
    mod = _load("analysis_artifacts")
    run_dir = _build_claude_run(
        tmp_path,
        records=_windowed_records(),
        post_kind="same-session-slice",
        timestamp="2026-05-28T12:00:00Z",
    )
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.trajectory_window_start is None
    assert stats.trajectory_record_count == 5  # 全量
    assert stats.patch_event_count == 3  # 全部 write-op（含触发前 pre_edit）


def test_window_start_falls_back_to_run_id_timestamp(tmp_path: Path) -> None:
    """C 退化：summary 无 timestamp 时用 run_id 内嵌时间戳作窗口下界。"""
    mod = _load("analysis_artifacts")
    run_dir = _build_claude_run(
        tmp_path,
        records=_windowed_records(),
        post_kind="same-session-full",
        timestamp=None,
        run_id="rvf-20260528T120000Z-claude",
    )
    inputs = mod.discover_inputs(run_dir)
    stats = mod.gather_stats(inputs)
    assert stats.trajectory_window_start == "2026-05-28T12:00:00Z"
    assert stats.trajectory_record_count == 3
