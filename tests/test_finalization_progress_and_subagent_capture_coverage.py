"""真实进程崩溃的阶段恢复，以及子代理 spawn 未知时的统计覆盖。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from _rvf_test_support.loader import load_script_module


START = "2026-10-25T13:00:00.100Z"
END = "2026-10-25T13:00:00.200Z"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))


CRASHING_FINALIZER_PROGRAM = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import rvf_run_finalize as finalizer
import review_run_terminal_archive as archive
run, repo, mode, crash_point = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
archive._surface_terminal_archive_analysis = lambda *args, **kwargs: None
counter = run / "actual-stage-calls.jsonl"
def counted(name):
    with counter.open("a") as handle:
        handle.write(json.dumps({"stage": name, "mode": mode}) + "\n")
original_capture_after = finalizer.capture_after
def capture_after(*args, **kwargs):
    counted("capture_after")
    result = original_capture_after(*args, **kwargs)
    if mode == "crash" and crash_point == "after_snapshot_before_stage_receipt":
        os._exit(73)
    return result
finalizer.capture_after = capture_after
original_capture = finalizer.capture_run
def capture(**kwargs):
    counted("trajectory_capture")
    return original_capture(**kwargs)
finalizer.capture_run = capture
original_release = finalizer._release_tracker_lease
def release(*args, **kwargs):
    if mode == "crash" and crash_point == "after_workspace_stage":
        os._exit(73)
    return original_release(*args, **kwargs)
finalizer._release_tracker_lease = release
original_summary_merge = finalizer.merge_review_run_summary
def summary_merge(*args, **kwargs):
    result = original_summary_merge(*args, **kwargs)
    if mode == "crash" and crash_point in {"after_final_summary_before_cache", "mutable_analysis_projection"} and "finalize_completed_at" in args[1]:
        os._exit(73)
    return result
finalizer.merge_review_run_summary = summary_merge
if crash_point != "mutable_analysis_projection":
    finalizer._scaffold_analysis = lambda *args: {}
if mode == "crash":
    archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted", reason="真实隔离进程的未完成归档",
        event={"session_id": "parent-session", "transcript_path": str(run / "source-rollout.jsonl")})
else:
    result = archive.resume_pending_review_run_terminal_archives(repo=repo, session_id="parent-session")
    print(json.dumps(result, ensure_ascii=False))
'''


def crash_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    finalizer = load_script_module("rvf_run_finalize")
    snapshot = load_script_module("workspace_snapshot")
    repo = tmp_path / "r"
    repo.mkdir()
    for arguments in (["init", "-q", "-b", "main"], ["config", "user.name", "崩溃恢复测试"],
                      ["config", "user.email", "crash-recovery@example.test"]):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)
    (repo / "feature.txt").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "初始化崩溃恢复夹具"], check=True, capture_output=True)
    run = tmp_path / "run"
    write_json(run / "summary.json", {"run_id": run.name, "run_dir": str(run), "repo": str(repo),
        "session_id": "parent-session", "review_run_started_at": START})
    write_json(run / "artifacts/inputs/scope.contract.json", {"run_id": run.name, "repo": str(repo)})
    write_json(run / "artifacts/before-workspace-snapshot.json", snapshot.capture(repo))
    write_jsonl(run / "source-rollout.jsonl", [
        {"type": "session_meta", "payload": {"id": "parent-session", "originator": "Codex CLI"}},
        {"timestamp": "2026-10-25T13:00:00.150Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "终态之前的真实轨迹夹具"}},
    ])
    (repo / "feature.txt").write_text("terminal-workspace\n")
    return Path(finalizer.__file__).parent, repo, run


@pytest.mark.parametrize("crash_point", ["after_workspace_stage", "after_final_summary_before_cache", "after_snapshot_before_stage_receipt"])
def test_actual_process_crash_never_recaptures_later_live_workspace(tmp_path, crash_point):
    scripts, repo, run = crash_fixture(tmp_path)
    environment = dict(os.environ)
    environment["RVF_USER_LOCAL_DATA_ROOT"] = str(tmp_path / "state")
    command = [sys.executable, "-B", "-c", CRASHING_FINALIZER_PROGRAM, str(scripts), str(run), str(repo)]
    crashed = subprocess.run([*command, "crash", crash_point], env=environment, capture_output=True, text=True)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    assert not (run / "artifacts/.finalize.lock").exists()
    after_path = run / "artifacts/after-workspace-snapshot.json"
    original_after = after_path.read_bytes()
    (tmp_path / "original-after-workspace-snapshot.json").write_bytes(original_after)
    original_patch_path = run / "artifacts/workspace-diff.patch"
    original_patch = original_patch_path.read_bytes() if original_patch_path.exists() else None
    if original_patch is not None:
        (tmp_path / "original-workspace-diff.patch").write_bytes(original_patch)
    (repo / "feature.txt").write_text("later-unrelated-workspace\n")
    resumed = subprocess.run([*command, "resume", crash_point], env=environment, capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    summary = json.loads((run / "summary.json").read_text())
    counters = [json.loads(line) for line in (run / "actual-stage-calls.jsonl").read_text().splitlines()]
    observation = {"crash_point": crash_point, "crash_returncode": crashed.returncode,
        "resume_returncode": resumed.returncode, "resume_stdout": resumed.stdout,
        "original_after_sha256": hashlib.sha256(original_after).hexdigest(),
        "resumed_after_sha256": hashlib.sha256(after_path.read_bytes()).hexdigest(),
        "stage_calls": counters, "workspace_diff": summary["finalize"]["workspace_diff"]}
    write_json(tmp_path / "actual-crash-recovery-observations.json", observation)
    assert after_path.read_bytes() == original_after
    assert sum(item["stage"] == "capture_after" for item in counters) == 1
    if crash_point == "after_snapshot_before_stage_receipt":
        assert summary["finalize"]["workspace_diff"]["status"] == "unavailable"
    else:
        assert summary["finalize"]["workspace_diff"]["status"] == "complete"
        assert original_patch_path.read_bytes() == original_patch
        assert sum(item["stage"] == "trajectory_capture" for item in counters) == 1


def subagent_fixture(tmp_path: Path, spawned_at: object) -> Path:
    write_json(tmp_path / "summary.json", {"run_id": tmp_path.name, "review_run_started_at": START,
        "review_run_terminal_at": END, "finalize": {"trajectory": {"post_rvf_source_kind": "same-session-full"}}})
    root = tmp_path / "artifacts/trajectory/rvf"
    write_jsonl(root / "rollout.jsonl", [{"type": "session_meta", "payload": {"id": "parent-session"}}])
    child = root / "subagents/child-session"
    write_json(child / "manifest.json", {"spawn": {"agent_id": "child-session", "spawn_call_id": "spawn-child", "spawned_at": spawned_at}})
    write_jsonl(child / "trajectory.jsonl", [{"ts": "2026-10-25T13:00:00.160Z", "kind": "tool_call", "tool": "apply_patch",
        "call_id": "actual-child-patch", "artifact_refs": [{"path": "feature.py", "op": "edit"}]}])
    return root


@pytest.mark.parametrize("spawned_at", [None, "", "invalid", "2026-10-25T13:00:00.150"])
def test_unknown_spawn_does_not_report_complete_zero_subagent_work(tmp_path, spawned_at):
    analysis = load_script_module("analysis_artifacts")
    subagent_fixture(tmp_path, spawned_at)
    result = analysis.scaffold_run(tmp_path)
    stats = result["stats_dict"]
    payload = json.loads(result["causality_json_path"].read_text())
    write_json(tmp_path / "actual-unknown-spawn-observations.json", {"spawned_at": spawned_at,
        "subagent_capture_status": stats["subagent_capture_status"], "subagent_patch_event_count": stats["subagent_patch_event_count"],
        "patch_call_attribution_status": payload["patch_call_attribution_status"]})
    assert stats["subagent_capture_status"] in {"partial", "unavailable"}
    assert stats["subagent_patch_event_count"] is None
    assert stats["observed_subagent_write_op_event_count"] == 1
    assert payload["patch_call_attribution_status"] != "complete"
    assert payload["unattributed_subagent_patch_observations"][0]["call_id"] == "actual-child-patch"


@pytest.mark.parametrize("changed_evidence", ["artifact", "identity"])
def test_changed_checkpoint_evidence_waits_and_recovers_without_live_snapshot(tmp_path, changed_evidence):
    scripts, repo, run = crash_fixture(tmp_path)
    finalizer = load_script_module("rvf_run_finalize")
    environment = dict(os.environ)
    environment["RVF_USER_LOCAL_DATA_ROOT"] = str(tmp_path / "state")
    command = [sys.executable, "-B", "-c", CRASHING_FINALIZER_PROGRAM, str(scripts), str(run), str(repo)]
    crashed = subprocess.run([*command, "crash", "after_workspace_stage"], env=environment, capture_output=True, text=True)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    proof_path = (run / "artifacts/after-workspace-snapshot.json" if changed_evidence == "artifact"
                  else run / "artifacts" / finalizer.TERMINAL_ARCHIVE_FINALIZATION_PROGRESS_FILENAME)
    original_proof = proof_path.read_bytes()
    changed = json.loads(original_proof)
    if changed_evidence == "artifact":
        changed["repo"] = str(tmp_path / "foreign-repository")
    else:
        changed["identity"]["run_id"] = "foreign-run"
    write_json(proof_path, changed)
    (repo / "feature.txt").write_text("later-unrelated-workspace\n")
    blocked = subprocess.run([*command, "resume", "after_workspace_stage"], env=environment, capture_output=True, text=True)
    assert blocked.returncode == 0, blocked.stderr
    assert json.loads(blocked.stdout)[0]["status"] == "waiting_for_evidence"
    assert not (run / "artifacts/.finalize.lock").exists()
    for _ in range(3):
        waiting = subprocess.run([*command, "resume", "after_workspace_stage"], env=environment, capture_output=True, text=True)
        assert waiting.returncode == 0, waiting.stderr
        assert json.loads(waiting.stdout)[0]["status"] == "waiting_for_evidence"
    request_path = run / "artifacts/review-run-terminal-archive-request.json"
    assert json.loads(request_path.read_text())["attempt_count"] <= 3
    proof_path.write_bytes(original_proof)
    recovered = subprocess.run([*command, "resume", "after_workspace_stage"], env=environment, capture_output=True, text=True)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)[0]["status"] == "archived"
    counters = [json.loads(line) for line in (run / "actual-stage-calls.jsonl").read_text().splitlines()]
    assert sum(item["stage"] == "capture_after" for item in counters) == 1
    assert sum(item["stage"] == "trajectory_capture" for item in counters) == 1


def test_invalid_manifest_spawn_recovers_from_correlated_native_spawn(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    root = subagent_fixture(tmp_path, "invalid")
    manifest = root / "subagents/child-session/manifest.json"
    original_manifest = manifest.read_bytes()
    write_jsonl(root / "rollout.jsonl", [
        {"type": "session_meta", "payload": {"id": "parent-session"}},
        {"timestamp": "2026-10-25T13:00:00.150Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent", "call_id": "spawn-child", "arguments": "{}"}},
        {"timestamp": "2026-10-25T13:00:00.151Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "spawn-child", "output": json.dumps({"agent_id": "child-session"})}},
    ])
    inputs = analysis.discover_inputs(tmp_path)
    boundary = analysis._subagent_spawn_boundary_evidence(inputs, inputs.subagents[0])
    assert boundary["source"] == "correlated_native_spawn"
    stats = analysis.gather_stats(inputs)
    assert stats.subagent_capture_status == "complete"
    assert stats.subagent_patch_event_count == 1
    assert manifest.read_bytes() == original_manifest


def test_conflicting_child_identity_stays_partial_and_preserves_raw_patch(tmp_path):
    analysis = load_script_module("analysis_artifacts")
    root = subagent_fixture(tmp_path, "2026-10-25T13:00:00.150Z")
    manifest = root / "subagents/child-session/manifest.json"
    payload = json.loads(manifest.read_text())
    payload["spawn"]["agent_id"] = "foreign-child"
    write_json(manifest, payload)
    result = analysis.scaffold_run(tmp_path)
    assert result["stats_dict"]["subagent_patch_event_count"] is None
    assert result["stats_dict"]["subagent_capture_status"] == "partial"
    causality = json.loads(result["causality_json_path"].read_text())
    assert causality["unattributed_subagent_patch_observations"][0]["call_id"] == "actual-child-patch"
    assert causality["patches"] == []


def test_later_analysis_projection_does_not_invalidate_completed_capture_stage(tmp_path):
    scripts, repo, run = crash_fixture(tmp_path)
    environment = dict(os.environ)
    environment["RVF_USER_LOCAL_DATA_ROOT"] = str(tmp_path / "state")
    command = [sys.executable, "-B", "-c", CRASHING_FINALIZER_PROGRAM, str(scripts), str(run), str(repo)]
    crashed = subprocess.run([*command, "crash", "mutable_analysis_projection"], env=environment, capture_output=True, text=True)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    analysis_summary = run / "artifacts/analysis/summary.md"
    assert analysis_summary.is_file()
    revised_narrative = "合成夹具：后续分析更新叙事，已经完成的捕获事实保持有效。\n"
    analysis_summary.write_text(revised_narrative)
    resumed = subprocess.run([*command, "resume", "mutable_analysis_projection"], env=environment, capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)[0]["status"] == "archived"
    assert analysis_summary.read_text() == revised_narrative
