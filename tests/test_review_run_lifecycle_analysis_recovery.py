"""固定候选、准备终态和分析证据边界的行为回归。"""

from __future__ import annotations

import json
from pathlib import Path

from _rvf_test_support.loader import load_script_module


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_review_run_start_survives_recreated_ledger_and_late_phase(tmp_path, monkeypatch):
    from core.run_ledger.run_ledger import RunLedger, rvf_state_fields

    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local"))
    run_directory = tmp_path / "review-run"
    first = RunLedger(component="prepare-run", run_id="review-run", run_dir=run_directory)
    event = first.event(phase="prepare", event="started", status="started", reason_code="prepare_started")
    first.summary(status="completed", reason_code="prepare_completed", **rvf_state_fields(phase="prepare"))
    reviewer = RunLedger(component="reviewer", run_id="review-run", run_dir=run_directory)
    reviewer.event(phase="review", event="dispatch_started", status="started")
    reviewer.summary(status="completed", reason_code="reviewer_completed")
    first.summary(status="completed", reason_code="late_prepare", **rvf_state_fields(phase="prepare"))
    summary = json.loads(first.summary_path.read_text())
    assert summary["review_run_started_at"] == event["timestamp"]
    assert summary["rvf_state_phase"] == "review"


def test_analysis_window_uses_immutable_review_start_instead_of_latest_writer():
    analysis = load_script_module("analysis_artifacts")
    assert analysis._rvf_window_start({
        "timestamp": "2026-10-25T13:20:00Z",
        "review_run_started_at": "2026-10-25T12:59:00Z",
    }, "rvf-review-start-0d2b17176837f3e4") == "2026-10-25T12:59:00Z"


def test_usage_without_cumulative_baseline_is_unknown(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    rollout = tmp_path / "artifacts/trajectory/rvf/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({
        "timestamp": "2026-10-25T13:20:00Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 10,
            "reasoning_output_tokens": 2, "total_tokens": 110,
        }}},
    }) + "\n")
    usage = finalize._usage_summary(tmp_path)
    assert usage["input_tokens"] is None
    assert usage["token_usage_coverage_status"] == "unavailable"


def test_intake_reads_bound_run_identity_before_historical_mentions(tmp_path, monkeypatch):
    intake = load_script_module("rvf_handoff_intake")
    run_directory = tmp_path / "rvf-review-start-26c8cb1f8628fb51"
    write_json(run_directory / "summary.json", {"run_id": run_directory.name})
    monkeypatch.setattr(intake, "repo_snapshot", lambda repo: {"is_git_repo": True, "dirty_paths": []})
    monkeypatch.setattr(intake, "load_candidate_evidence", lambda *args, **kwargs: ("missing", None, None, []))
    result = intake.build_payload(
        "历史参考 rvf-20261024T100000Z-stop-hook-old\n"
        f"- run dir: {run_directory}\n", tmp_path, None,
    )
    assert result["run_id"] == run_directory.name
    assert result["run_identity_status"] == "verified"


def test_intake_rejects_conflicting_declared_and_bound_run_identity(tmp_path, monkeypatch):
    intake = load_script_module("rvf_handoff_intake")
    run_directory = tmp_path / "rvf-review-start-26c8cb1f8628fb51"
    write_json(run_directory / "summary.json", {"run_id": run_directory.name})
    monkeypatch.setattr(intake, "repo_snapshot", lambda repo: {"is_git_repo": True, "dirty_paths": []})
    monkeypatch.setattr(intake, "load_candidate_evidence", lambda *args, **kwargs: ("missing", None, None, []))
    result = intake.build_payload(
        f"- run id: rvf-review-start-0d2b17176837f3e4\n- run dir: {run_directory}\n",
        tmp_path, None,
    )
    assert result["run_identity_status"] == "invalid"
    assert result["run_identity_errors"]


def test_current_codex_spawn_result_is_correlated_and_deduplicated(tmp_path):
    load_script_module("subagent_capture")
    from adapters.codex.subagent import discover_spawned_agents

    rollout = tmp_path / "rollout.jsonl"
    records = [
        {"timestamp": "2026-10-25T12:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "collaboration.spawn_agent", "call_id": "spawn-call",
            "arguments": json.dumps({"task_name": "validate_rvf_001", "message": "验证 RVF-001"}),
        }},
        {"timestamp": "2026-10-25T12:00:01Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "spawn-call",
            "output": json.dumps({"agent_id": "child-session", "task_name": "validate_rvf_001"}),
        }},
        {"timestamp": "2026-10-25T12:00:01Z", "type": "event_msg", "payload": {
            "type": "collab_agent_spawn_end", "call_id": "spawn-call", "new_thread_id": "child-session",
        }},
    ]
    rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    spawns = discover_spawned_agents(rollout)
    assert len(spawns) == 1
    assert spawns[0].call_id == "spawn-call"
    assert spawns[0].prompt == "验证 RVF-001"


def test_start_receipt_declares_final_integration_only():
    decision = load_script_module("rvf_agent_decision_cli")
    handoff = decision.build_execution_handoff(
        repo="/repo", run_id="run", run_dir="/run", prepare_artifacts={},
        prep_file_path="/prep", kanban_task_id="53034",
    )
    assert handoff["base_absorption_policy"] == "final_integration_only"
    assert "base-branch-sync" in handoff["execution_instruction"]


def test_native_subagent_activity_binds_thread_without_guessing_task_name(tmp_path):
    load_script_module("subagent_capture")
    from adapters.codex.subagent import discover_spawned_agents

    records = [
        {"type": "session_meta", "payload": {"id": "parent-session"}},
        {"timestamp": "2026-10-25T12:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent", "namespace": "collaboration",
            "call_id": "native-spawn", "arguments": json.dumps({"task_name": "fix_issue", "message": "修复意见"}),
        }},
        {"timestamp": "2026-10-25T12:00:01Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "parent-session", "item": {
                "type": "SubAgentActivity", "kind": "started", "id": "native-spawn",
                "agent_thread_id": "native-child-session", "agent_path": "/root/fix_issue",
            },
        }},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "native-spawn",
            "output": json.dumps({"task_name": "/root/fix_issue"})}},
    ]
    rollout = tmp_path / "native-rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    spawns = discover_spawned_agents(rollout)
    assert len(spawns) == 1
    assert spawns[0].agent_id == "native-child-session"
    assert spawns[0].call_id == "native-spawn"


def test_child_rollout_filename_cannot_replace_native_session_identity(tmp_path):
    load_script_module("subagent_capture")
    from adapters.codex.subagent import find_subagent_rollout

    rollout = tmp_path / "rollout-2026-10-25-expected-child.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "foreign-child"}}) + "\n")
    assert find_subagent_rollout("expected-child", sessions_root=tmp_path) is None


def test_analysis_keeps_attempt_evidence_when_native_patch_calls_are_missing(tmp_path, monkeypatch):
    analysis = load_script_module("analysis_artifacts")
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    run_directory = tmp_path / "rvf-review-start-26c8cb1f8628fb51"
    patch = run_directory / "artifacts/fix-attempts/fix-rvf-001/fix.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff --git a/feature.py b/feature.py\n")
    write_json(run_directory / "summary.json", {"run_id": run_directory.name, "repo": str(tmp_path)})
    monkeypatch.setattr(reviewable_unit_diff_tracker, "rvf_causality_for_run", lambda **kwargs: {
        "issues": [{"issue_id": "RVF-001", "candidate_patch_call_ids": []}],
        "fix_attempts": [{"attempt_id": "fix-rvf-001", "run_id": run_directory.name,
                          "issue_id": "RVF-001", "fix_patch_path": str(patch), "status": "applied"}],
        "patch_events": [{"issue_id": "RVF-001", "attempt_id": "fix-rvf-001",
                          "path": "feature.py", "call_id": None}],
    })
    result = analysis.scaffold_run(run_directory)
    causality = json.loads(result["causality_json_path"].read_text())
    assert causality["patch_call_attribution_status"] == "partial"
    assert causality["issues"][0]["candidate_patch_call_ids"] == []
    assert causality["attempt_patch_evidence"][0]["fix_patch_sha256"]
    assert causality["attempt_patch_evidence"][0]["evidence_level"] == "attempt_patch"


def test_unresolved_spawn_is_partial_capture_instead_of_zero_work(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    write_json(tmp_path / "summary.json", {"run_id": "review-run"})
    rollout = tmp_path / "artifacts/trajectory/rvf/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "name": "spawn_agent", "call_id": "unresolved-spawn",
    }}) + "\n")
    stats = analysis.gather_stats(analysis.discover_inputs(tmp_path))
    assert stats.subagent_capture_status == "partial"


def test_full_session_usage_has_fixed_bounds_and_deduplicated_phase_responses(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    write_json(tmp_path / "summary.json", {"run_id": "review-run", "review_run_started_at": "2026-10-25T12:59:00Z",
        "review_run_terminal_at": "2026-10-25T13:20:00Z"})
    write_json(tmp_path / "artifacts/review-run-lifecycle.json", {"run_id": "review-run",
        "phase_started_at": {"prepare": "2026-10-25T12:59:00Z", "review": "2026-10-25T13:00:00Z"}})
    records = [{"type": "session_meta", "payload": {"id": "parent-session"}}]
    for timestamp, value in [("2026-10-25T12:58:00Z", 100), ("2026-10-25T13:15:00Z", 150),
                             ("2026-10-25T13:30:00Z", 900)]:
        records.append({"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"input_tokens": value, "cached_input_tokens": 0,
                "output_tokens": value // 10, "reasoning_output_tokens": 0, "total_tokens": value + value // 10}}}})
    response = {"timestamp": "2026-10-25T13:01:00Z", "type": "token_usage_record", "payload": {
        "thread_id": "parent-session", "response_id": "native-response", "usage": {
            "input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 2,
            "reasoning_output_tokens": 1, "total_tokens": 22,
        }}}
    records.extend([response, response])
    rollout = tmp_path / "artifacts/trajectory/rvf/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    usage = finalize._usage_summary(tmp_path)
    assert usage["input_tokens"] == 50
    assert usage["phase_wall_seconds"]["prepare"] == 60
    assert usage["phase_response_token_usage"]["review"]["input_tokens"] == 20
    assert usage["observed_native_response_count"] == 1


def test_foreign_session_cumulative_usage_is_not_a_review_baseline(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    for kind, session, total in [("pre-rvf", "parent-session", 100), ("rvf", "forked-child", 150)]:
        rollout = tmp_path / f"artifacts/trajectory/{kind}/rollout.jsonl"
        rollout.parent.mkdir(parents=True)
        records = [{"type": "session_meta", "payload": {"id": session}},
            {"timestamp": "2026-10-25T13:01:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"input_tokens": total}}}}]
        rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    usage = finalize._usage_summary(tmp_path)
    assert usage["input_tokens"] is None
    assert usage["token_usage_coverage_status"] == "unavailable"


def test_unbound_session_counters_do_not_claim_complete_usage(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    for kind, total in [("pre-rvf", 100), ("rvf", 150)]:
        rollout = tmp_path / f"artifacts/trajectory/{kind}/rollout.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({"timestamp": "2026-10-25T13:01:00Z", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"input_tokens": total}}}}) + "\n")
    usage = finalize._usage_summary(tmp_path)
    assert usage["input_tokens"] is None
    assert usage["token_usage_coverage_status"] == "unavailable"


def test_terminal_and_spawn_boundaries_exclude_later_work_and_fork_history(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name,
        "review_run_started_at": "2026-10-25T13:00:00Z",
        "review_run_terminal_at": "2026-10-25T13:10:00Z",
        "finalize": {"trajectory": {"post_rvf_source_kind": "same-session-full"}}})
    root = tmp_path / "artifacts/trajectory/rvf"
    root.mkdir(parents=True)
    def patch(timestamp, call_id):
        return {"ts": timestamp, "kind": "tool_call", "tool": "apply_patch", "call_id": call_id,
                "artifact_refs": [{"path": "file.py", "op": "edit"}]}
    records = [patch("2026-10-25T13:01:00Z", "main-patch"), patch("2026-10-25T13:11:00Z", "later-run-patch")]
    (root / "trajectory.jsonl").write_text("\n".join(json.dumps(item) for item in records) + "\n")
    child = root / "subagents/child-session"
    child.mkdir(parents=True)
    write_json(child / "manifest.json", {"agent_id": "child-session", "spawn": {
        "spawned_at": "2026-10-25T13:05:00Z", "spawn_call_id": "spawn-child"}})
    child_records = [patch("2026-10-25T13:01:00Z", "inherited-main-patch"),
                     patch("2026-10-25T13:06:00Z", "child-patch"),
                     patch("2026-10-25T13:11:00Z", "later-child-patch")]
    (child / "trajectory.jsonl").write_text("\n".join(json.dumps(item) for item in child_records) + "\n")
    inputs = analysis.discover_inputs(tmp_path)
    stats = analysis.gather_stats(inputs)
    assert stats.patch_event_count == 1
    assert stats.subagent_patch_event_count == 1
    calls = analysis._collect_patches(inputs, window_start=stats.trajectory_window_start,
                                     window_end=stats.trajectory_window_end)
    assert {patch["call_id"] for patch in calls} == {"main-patch", "child-patch"}


def test_native_file_change_recovers_nested_patch_without_reading_encrypted_tool_input(tmp_path):
    distill = load_script_module("trajectory_distill")
    analysis = load_script_module("analysis_artifacts")
    root = tmp_path / "artifacts/trajectory/rvf"
    root.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "parent-session"}},
        {"timestamp": "2026-10-25T13:01:00Z", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-code-call", "input": "encrypted-input"}},
        {"timestamp": "2026-10-25T13:01:01Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "parent-session", "item": {
                "type": "FileChange", "id": "exec-native-file-change", "status": "completed",
                "changes": {"src/file.py": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"}}}}},
        {"timestamp": "2026-10-25T13:01:01Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "foreign-session", "item": {
                "type": "FileChange", "id": "foreign-file-change", "status": "completed",
                "changes": {"src/foreign.py": {"type": "add", "content": "value = 1\n"}}}}},
    ]
    rollout = root / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    distilled, _index = distill.distill_codex_jsonl(rollout_path=rollout, rollout_filename="rollout.jsonl", repo=None)
    (root / "trajectory.jsonl").write_text("\n".join(json.dumps(record) for record in distilled) + "\n")
    patches = analysis._collect_patches(analysis.discover_inputs(tmp_path))
    assert [patch["call_id"] for patch in patches] == ["exec-native-file-change"]
    assert patches[0]["artifact_refs"][0]["path"] == "src/file.py"


def test_merge_command_advances_real_lifecycle_without_waiting_for_stop_hook(tmp_path, monkeypatch):
    from core.run_ledger.review_run_lifecycle import record_review_run_phase
    merge = load_script_module("rvf_merge_dispositions")
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name})
    record_review_run_phase(tmp_path, run_id=tmp_path.name, phase="prepare",
                            observed_at="2026-10-25T13:00:00Z", starts_review_run=True)
    monkeypatch.setattr(merge, "build_merge_dispositions_payload", lambda *args: {"schema_version": 1})
    merge.write_merge_dispositions(tmp_path, {})
    lifecycle = json.loads((tmp_path / "artifacts/review-run-lifecycle.json").read_text())
    assert lifecycle["phase"] == "merge"
    assert lifecycle["phase_started_at"]["merge"]


def test_component_status_and_reviewer_counts_use_independent_artifacts(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name, "review_run_terminal_status": "declined"})
    write_json(tmp_path / "artifacts/reviewers/reviewer-plan.json", {"reviewers": [
        {"reviewer_id": "one"}, {"reviewer_id": "two"}, {"reviewer_id": "three"}]})
    write_json(tmp_path / "artifacts/reviewers/one/review-result.json", {"issues": "损坏的结果"})
    result = analysis.scaffold_run(tmp_path)
    states = result["stats_dict"]["component_states"]
    assert states["rvf_terminal_status"] == "declined"
    assert states["analyze_completion_status"] == "not_observed"
    assert states["land_status"] == "not_observed"
    assert states["base_integration_status"] == "unknown"
    assert states["planned_reviewer_count"] == 3
    assert states["observed_reviewer_result_count"] == 1
    assert states["valid_reviewer_result_count"] == 0
