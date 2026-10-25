"""审查时间窗口、真实 prepare 起点和原生响应归属的隔离回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _rvf_test_support.loader import load_script_module


START = "2026-10-25T13:00:00.100Z"
END = "2026-10-25T13:00:00.200Z"
PRE = "2026-10-25T12:59:59.999Z"
TOKEN_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")


def token_usage(value: int) -> dict[str, int]:
    return {key: value if key in {"input_tokens", "total_tokens"} else 0 for key in TOKEN_USAGE_KEYS}


def session_meta(session_id: str) -> dict:
    return {"type": "session_meta", "payload": {"id": session_id}}


def cumulative_sample(timestamp: object, value: int) -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": token_usage(value)},
    }}


def response_sample(session_id: str, response_id: str, timestamp: object, value: int) -> dict:
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": {
        "thread_id": session_id, "response_id": response_id, "usage": token_usage(value),
    }}


def patch_sample(timestamp: object, call_id: str) -> dict:
    return {"ts": timestamp, "kind": "tool_call", "tool": "apply_patch", "call_id": call_id,
            "artifact_refs": [{"path": "sample.py", "op": "edit"}]}


def cumulative_usage_fixture(run_directory: Path, samples: list[dict], *, started_at=START,
                             ended_at=END, baseline_samples: list[dict] | None = None) -> None:
    write_json(run_directory / "summary.json", {"run_id": run_directory.name,
        "review_run_started_at": started_at, "review_run_terminal_at": ended_at})
    write_jsonl(run_directory / "artifacts/trajectory/pre-rvf/rollout.jsonl", [session_meta("parent"),
        *(baseline_samples if baseline_samples is not None else [cumulative_sample(PRE, 100)])])
    write_jsonl(run_directory / "artifacts/trajectory/rvf/rollout.jsonl", [session_meta("parent"), *samples])


@pytest.mark.parametrize(("started_at", "inside"), [
    ("2026-10-25T13:00:00Z", "2026-10-25T13:00:00.000100Z"),
    (START, "2026-10-25T13:00:00.100100Z"),
    (START, "2026-10-25T13:00:00.100000001Z"),
    ("2026-10-25T13:00:00.100000001Z", "2026-10-25T13:00:00.100000002Z"),
    ("2026-10-25T13:00:00.1Z", "2026-10-25T15:00:00.100100+02:00"),
])
def test_cumulative_baseline_and_window_use_actual_utc_moments(tmp_path, started_at, inside):
    finalize = load_script_module("rvf_run_finalize")
    cumulative_usage_fixture(tmp_path, [cumulative_sample(inside, 150), cumulative_sample(END, 200)], started_at=started_at)
    source = tmp_path / "artifacts/trajectory/rvf/rollout.jsonl"
    original_bytes = source.read_bytes()
    result = finalize._usage_summary(tmp_path)
    assert result["input_tokens"] == 100
    assert result["baseline_source"] == "artifacts/trajectory/pre-rvf/rollout.jsonl"
    assert result["token_count_event_count"] == 2
    assert result["token_usage_coverage_status"] == "complete"
    assert result["started_at"] == started_at
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("unknown_timestamp", [None, "invalid", "2026-10-25T13:00:00.150"])
def test_unknown_sample_timestamps_cannot_replace_baseline_or_final(tmp_path, unknown_timestamp):
    finalize = load_script_module("rvf_run_finalize")
    cumulative_usage_fixture(tmp_path, [cumulative_sample(unknown_timestamp, 150), cumulative_sample(END, 200),
                                      cumulative_sample(unknown_timestamp, 999)])
    result = finalize._usage_summary(tmp_path)
    assert result["input_tokens"] == 100
    assert result["token_count_event_count"] == 1


@pytest.mark.parametrize("unknown_boundary", [None, "invalid", "2026-10-25T13:00:00.200"])
@pytest.mark.parametrize("boundary_name", ["started_at", "ended_at"])
def test_missing_or_invalid_required_boundary_keeps_cumulative_usage_unknown(tmp_path, unknown_boundary, boundary_name):
    finalize = load_script_module("rvf_run_finalize")
    cumulative_usage_fixture(tmp_path, [cumulative_sample(END, 200)], **{boundary_name: unknown_boundary})
    result = finalize._usage_summary(tmp_path)
    assert result["input_tokens"] is None
    assert result["token_usage_coverage_status"] == "unavailable"


@pytest.mark.parametrize("sample_timestamp", [START, "2026-10-25T13:00:00.100000Z", "2026-10-25T13:00:00.100000000Z"])
def test_only_counter_at_start_does_not_prove_terminal_usage(tmp_path, sample_timestamp):
    finalize = load_script_module("rvf_run_finalize")
    cumulative_usage_fixture(tmp_path, [cumulative_sample(sample_timestamp, 100)], baseline_samples=[])
    result = finalize._usage_summary(tmp_path)
    assert result["input_tokens"] is None
    assert result["token_usage_coverage_status"] == "unavailable"


def test_post_start_sample_cannot_invent_a_missing_cumulative_baseline(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    cumulative_usage_fixture(tmp_path, [cumulative_sample("2026-10-25T13:00:00.100100Z", 150),
                                      cumulative_sample(END, 200)], baseline_samples=[])
    assert finalize._usage_summary(tmp_path)["token_usage_coverage_status"] == "unavailable"


@pytest.mark.parametrize("host", ["codex", "claude", "kimi"])
def test_trigger_since_filter_accepts_later_mixed_precision_timestamp(tmp_path, host):
    capture = load_script_module("trajectory_capture")
    timestamp = "2026-10-25T13:00:00.100100Z"
    records = {
        "codex": {"type": "event_msg", "timestamp": timestamp,
                  "payload": {"type": "user_message", "message": "$review-validate-fix"}},
        "claude": {"type": "user", "timestamp": timestamp,
                   "message": {"role": "user", "content": "$review-validate-fix"}},
        "kimi": {"type": "turn.prompt", "time": timestamp,
                 "origin": {"kind": "user"}, "input": [{"type": "text", "text": "$review-validate-fix"}]},
    }
    finders = {"codex": capture.find_rvf_start_in_jsonl, "claude": capture.find_rvf_start_in_claude_jsonl,
               "kimi": capture.find_rvf_start_in_kimi_wire_jsonl}
    transcript = tmp_path / "trigger.jsonl"
    write_jsonl(transcript, [records[host]])
    cut = finders[host](transcript, since_timestamp=START)
    assert cut is not None
    assert cut.timestamp == timestamp


def test_review_start_cut_includes_first_later_mixed_precision_event(tmp_path):
    capture = load_script_module("trajectory_capture")
    transcript = tmp_path / "cut.jsonl"
    write_jsonl(transcript, [{"timestamp": PRE}, {"timestamp": "2026-10-25T13:00:00.100100Z"}, {"timestamp": END}])
    assert capture._cut_at_review_run_start(transcript, START).line_index == 1


def test_terminal_capture_keeps_raw_boundary_and_excludes_later_event(tmp_path, monkeypatch):
    capture = load_script_module("trajectory_capture")
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name, "review_run_started_at": START})
    transcript = tmp_path / "source.jsonl"
    records = [session_meta("parent")]
    for timestamp in (START, "2026-10-25T13:00:00.100100Z", END,
                      "2026-10-25T13:00:00.200000001Z", "2026-10-25T13:00:00.200100Z"):
        records.append({"timestamp": timestamp, "type": "event_msg", "payload": {"type": "agent_message", "message": "样本"}})
    write_jsonl(transcript, records)
    original_bytes = transcript.read_bytes()
    monkeypatch.setattr(capture, "capture_all_subagents", lambda **kwargs: [])
    capture.capture_run(run_dir=tmp_path, event={"session_id": "parent", "transcript_path": str(transcript),
                                               "review_run_terminal_observed_at": END})
    output = tmp_path / "artifacts/trajectory/rvf/rollout.jsonl"
    observed = [json.loads(line)["timestamp"] for line in output.read_text().splitlines()]
    assert observed == [START, "2026-10-25T13:00:00.100100Z", END]
    assert transcript.read_bytes() == original_bytes


def test_patch_counts_membership_and_order_share_real_timestamp_bounds(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    trajectory = tmp_path / "artifacts/trajectory/rvf/trajectory.jsonl"
    write_jsonl(trajectory, [patch_sample(START, "at-start"),
        patch_sample("2026-10-25T13:00:00.100100Z", "inside"),
        patch_sample("2026-10-25T13:00:00.200100Z", "after-terminal"),
        patch_sample(None, "missing-time"), patch_sample("2026-10-25T13:00:00.150", "naive-time")])
    inputs = analysis.discover_inputs(tmp_path)
    assert analysis._count_trajectory(inputs, window_start=START, window_end=END) == (2, {"tool_call": 2}, 2)
    assert [item["call_id"] for item in analysis._collect_patches(inputs, window_start=START, window_end=END)] == ["at-start", "inside"]


def test_spawn_lower_bound_uses_later_utc_moment_and_missing_capture_is_partial(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name, "review_run_started_at": START,
        "review_run_terminal_at": END, "finalize": {"trajectory": {"post_rvf_source_kind": "same-session-full"}}})
    root = tmp_path / "artifacts/trajectory/rvf"
    write_jsonl(root / "rollout.jsonl", [{"timestamp": "2026-10-25T13:00:00.100100Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "uncaptured-child"}}])
    child = root / "subagents/child"
    write_json(child / "manifest.json", {"spawn": {"agent_id": "child", "spawned_at": "2026-10-25T13:00:00.100100Z"}})
    write_jsonl(child / "trajectory.jsonl", [patch_sample("2026-10-25T13:00:00.100050Z", "inherited"),
        patch_sample("2026-10-25T13:00:00.100200Z", "child-patch")])
    inputs = analysis.discover_inputs(tmp_path)
    stats = analysis.gather_stats(inputs)
    assert stats.subagent_patch_event_count == 1
    assert stats.subagent_capture_status == "partial"
    assert [item["call_id"] for item in analysis._collect_patches(inputs, window_start=START, window_end=END)] == ["child-patch"]


@pytest.mark.parametrize(("window_start", "window_end"), [(None, END), (START, None), (END, START)])
def test_capture_completeness_requires_known_ordered_window(tmp_path, window_start, window_end):
    analysis = load_script_module("analysis_artifacts")
    write_jsonl(tmp_path / "artifacts/trajectory/rvf/rollout.jsonl", [session_meta("parent")])
    assert analysis._subagent_capture_status(analysis.discover_inputs(tmp_path), window_start, window_end) == "unavailable"


def test_native_response_phase_uses_parsed_phase_order(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    write_jsonl(tmp_path / "artifacts/trajectory/rvf/rollout.jsonl", [session_meta("parent"),
        response_sample("parent", "phase-response", "2026-10-25T13:00:00.101Z", 20)])
    lifecycle = {"phase_started_at": {"prepare": START, "review": "2026-10-25T13:00:00.100100Z"}}
    result = finalize._phase_and_component_resource_observations(tmp_path, START, END, lifecycle)
    assert list(result["phase_response_token_usage"]) == ["review"]
    assert result["phase_wall_seconds"] == {"prepare": 0.0, "review": 0.1}


def test_invalid_phase_timestamp_keeps_phase_allocation_unknown(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    write_jsonl(tmp_path / "artifacts/trajectory/rvf/rollout.jsonl", [session_meta("parent"),
        response_sample("parent", "phase-response", "2026-10-25T13:00:00.150Z", 20)])
    result = finalize._phase_and_component_resource_observations(tmp_path, START, END,
        {"phase_started_at": {"prepare": START, "review": "invalid"}})
    assert list(result["phase_response_token_usage"]) == ["unallocated"]
    assert result["phase_wall_seconds"] == {"prepare": None, "review": None}


def test_actual_prepare_event_establishes_immutable_start(tmp_path):
    load_script_module("rvf_run_finalize")
    from core.run_ledger.run_ledger import RunLedger
    from core.run_ledger.review_run_lifecycle import read_review_run_lifecycle

    first = RunLedger(component="prepare-run", run_id="bound-run", run_dir=tmp_path)
    event = first.event(phase="prepare", event="started", status="started", reason_code="prepare_started")
    assert read_review_run_lifecycle(tmp_path)["review_run_started_at"] == event["timestamp"]
    reviewer = RunLedger(component="reviewer", run_id="bound-run", run_dir=tmp_path)
    reviewer.event(phase="review", event="dispatch_started", status="started")
    first.event(phase="prepare", event="started", status="started", reason_code="prepare_started")
    assert read_review_run_lifecycle(tmp_path)["review_run_started_at"] == event["timestamp"]
    assert read_review_run_lifecycle(tmp_path)["phase"] == "review"


def test_archived_actual_prepare_event_recovers_start_and_rejects_foreign_run(tmp_path):
    load_script_module("rvf_run_finalize")
    from core.run_ledger.review_run_lifecycle import review_run_start_evidence

    actual = {"run_id": "bound-run", "component": "prepare-run", "phase": "prepare", "event": "started",
              "status": "started", "reason_code": "prepare_started", "timestamp": START}
    write_jsonl(tmp_path / "events.jsonl", [{**actual, "run_id": "foreign-run", "timestamp": PRE}, actual])
    summary = {"run_id": "bound-run", "timestamp": END, "review_run_started_at": "invalid"}
    assert review_run_start_evidence(tmp_path, summary) == (START, "archived_prepare_started_event")


@pytest.mark.parametrize(("field", "value"), [
    ("component", "reviewer"), ("phase", "review"), ("event", "prepare_started"),
    ("reason_code", "prepare_finished"), ("status", "completed"), ("timestamp", "invalid"),
])
def test_archived_prepare_fallback_requires_actual_contract_and_valid_time(tmp_path, field, value):
    load_script_module("rvf_run_finalize")
    from core.run_ledger.review_run_lifecycle import review_run_start_evidence

    event = {"run_id": "bound-run", "component": "prepare-run", "phase": "prepare", "event": "started",
             "status": "started", "reason_code": "prepare_started", "timestamp": START, field: value}
    write_jsonl(tmp_path / "events.jsonl", [event])
    assert review_run_start_evidence(tmp_path, {"run_id": "bound-run", "timestamp": END}) == (None, "unavailable")


def child_response_fixture(run_directory: Path, *, spawned_at="2026-10-25T13:00:00.150Z", spawn_identity="child") -> None:
    root = run_directory / "artifacts/trajectory/rvf"
    write_jsonl(root / "rollout.jsonl", [session_meta("parent"),
        response_sample("parent", "shared-response-id", "2026-10-25T13:00:00.110Z", 20)])
    child = root / "subagents/child"
    write_json(child / "manifest.json", {"spawn": {"agent_id": spawn_identity, "spawned_at": spawned_at}})
    write_jsonl(child / "rollout.jsonl", [session_meta("child"),
        response_sample("child", "inherited-response-id", "2026-10-25T13:00:00.120Z", 100),
        response_sample("child", "shared-response-id", "2026-10-25T13:00:00.160Z", 30)])


def test_response_identity_includes_session_and_child_window_starts_at_spawn(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    child_response_fixture(tmp_path)
    result = finalize._phase_and_component_resource_observations(tmp_path, START, END, {})
    components = result["component_response_token_usage"]
    assert components["main_session"]["observed_token_usage"]["input_tokens"] == 20
    assert components["subagent:child"]["observed_token_usage"]["input_tokens"] == 30
    assert result["observed_native_response_count"] == 2
    assert result["conflicting_native_response_identities"] == []


@pytest.mark.parametrize("unknown_spawn", [None, "invalid", "2026-10-25T13:00:00.150"])
def test_unknown_child_spawn_cannot_count_inherited_responses(tmp_path, unknown_spawn):
    finalize = load_script_module("rvf_run_finalize")
    child_response_fixture(tmp_path, spawned_at=unknown_spawn)
    component = finalize._phase_and_component_resource_observations(tmp_path, START, END, {})["component_response_token_usage"]["subagent:child"]
    assert component["observed_token_usage"] is None
    assert component["coverage_status"] == "unavailable"
    assert component["coverage_reason"] == "subagent_spawn_timestamp_unavailable"


def test_child_spawn_identity_must_match_native_rollout_identity(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    child_response_fixture(tmp_path, spawn_identity="foreign-child")
    component = finalize._phase_and_component_resource_observations(tmp_path, START, END, {})["component_response_token_usage"]["subagent:child"]
    assert component["observed_token_usage"] is None
    assert component["coverage_reason"] == "subagent_spawn_identity_mismatch"


def test_only_same_session_response_duplicates_deduplicate_or_conflict(tmp_path):
    finalize = load_script_module("rvf_run_finalize")
    child_response_fixture(tmp_path)
    child_rollout = tmp_path / "artifacts/trajectory/rvf/subagents/child/rollout.jsonl"
    child_records = [json.loads(line) for line in child_rollout.read_text().splitlines()]
    child_records.extend([child_records[-1], response_sample("child", "shared-response-id", "2026-10-25T13:00:00.170Z", 40)])
    write_jsonl(child_rollout, child_records)
    result = finalize._phase_and_component_resource_observations(tmp_path, START, END, {})
    assert result["observed_native_response_count"] == 2
    assert result["component_response_token_usage"]["subagent:child"]["observed_token_usage"]["input_tokens"] == 30
    assert result["conflicting_native_response_identities"] == [{"native_session_id": "child", "response_id": "shared-response-id"}]
