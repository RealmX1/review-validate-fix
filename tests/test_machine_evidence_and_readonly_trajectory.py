"""共享事实隔离、准备环境、只读状态投影及多 run 量测。"""
import hashlib
import sys

from test_task_snapshot_review_workflow import repository, fake_config
from test_automatic_task_review_progression import automatic_snapshot
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.workflow import resume_review


def test_workspace_preparation_applies_to_roles_and_final_validation(repository, tmp_path):
    from core.task_snapshot_review.task_snapshot import create_snapshot
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "prepared-review"
    prepare = [sys.executable, "-c", "from pathlib import Path; Path('.prepared-environment').write_text('ready')"]
    verify = [sys.executable, "-c", "from pathlib import Path; assert Path('.prepared-environment').read_text() == 'ready'"]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"prepare isolated environments",
        run_directory=run, execution_plan={"fix": True, "preparation_commands": [prepare],
            "validation_commands": [verify], "machine_evidence_commands": [verify], "machine_context": {"dependency_identity": "fixture-v1"}})
    config = fake_config(tmp_path)
    script = tmp_path / "reviewer.py"
    script.write_text("from pathlib import Path\nassert Path('.prepared-environment').is_file()\n" + script.read_text())
    result = resume_review(run, reviewer_configs=[config, config])
    assert result["status"] == "ready_to_commit"
    assert not (repository / ".prepared-environment").exists()
    assert len(list(run.glob("machine-evidence/attempt-*/evidence.json"))) == 1
    for request_file in run.glob("reviewer-*/attempt-*/request.json"):
        request = read_json(request_file)
        assert "reviewer-" not in str(request.get("shared_machine_evidence"))


def test_analyze_multiple_runs_reads_raw_and_effective_without_mutating(repository, tmp_path):
    from core.task_snapshot_review.trajectory_analysis import analyze_runs
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    attempt = run / "reviewer-1/attempt-001"
    original = read_json(attempt / "outcome.json")
    write_json(attempt / "outcome.json", {**original, "status": "failed"})
    write_json(attempt / "reconciled-native-completion.json", {"status": "completed", "source": "original_stdout"})
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in run.rglob("*") if path.is_file()}
    result = analyze_runs([run, run])
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in run.rglob("*") if path.is_file()}
    assert before == after
    assert len(result["runs"]) == 1
    first = next(item for item in result["runs"][0]["attempts"] if item["role"] == "reviewer-1")
    assert first["raw_status"] == "failed"
    assert first["effective_status"] == "completed"
    assert first["effective_status_evidence"]
    assert result["metrics"]["role_invocations"] == 2
    assert result["metrics"]["main_agent_model_round_trips"] is None


def test_historical_missing_usage_is_unknown(tmp_path):
    from core.task_snapshot_review.trajectory_analysis import analyze_runs
    run = tmp_path / "historical-run"
    write_json(run / "run.json", {"status": "failed", "base_commit": "historical-base", "candidate_commit": "historical-tree"})
    result = analyze_runs([run])
    assert result["metrics"]["native_usage"] is None
    assert result["runs"][0]["source_version"] is None


def test_historical_native_usage_can_be_proven_from_original_stream_without_writes(tmp_path):
    from core.task_snapshot_review.trajectory_analysis import analyze_runs
    run = tmp_path / "historical-native-usage"
    write_json(run / "run.json", {"status": "completed"})
    attempt = run / "reviewer-1/attempt-001"
    write_json(attempt / "request.json", {"config": {"output_format": "codex_json"}})
    write_json(attempt / "model-invocation.json", {"command": ["codex", "exec", "--json"]})
    (attempt / "stdout.txt").write_text('{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n')
    before = {str(path): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    result = analyze_runs([run])
    assert result["runs"][0]["attempts"][0]["native_usage"] == [{"input_tokens": 12, "output_tokens": 3}]
    assert result["runs"][0]["attempts"][0]["native_facts_evidence"].endswith("stdout.txt")
    assert before == {str(path): path.read_bytes() for path in run.rglob("*") if path.is_file()}
