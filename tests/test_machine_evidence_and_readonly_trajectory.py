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
    assert result["status"] == "review_and_validation_completed"
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


def test_cleanup_recovery_observation_detects_boot_change_without_writing_or_repeating_model(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import review_recovery_evidence_observation as recovery
    from core.task_snapshot_review.role_owned_process_cleanup import reconcile_registered_role_owned_process_cleanup
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, '-c', 'pass']])
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    attempt = run / 'reviewer-1/attempt-001'
    original_notice_id = read_json(run / 'business-result.json')['agentContinuationNotice']['noticeId']
    cleanup_file = attempt / 'role-owned-process-cleanup.json'
    write_json(cleanup_file, {**read_json(cleanup_file), 'status': 'unknown', 'owned_processes': [],
        'retry_not_before': 2**40, 'unchanged_recovery_attempt_count': 3})
    before = {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}
    first = recovery.observe_task_review_recovery_evidence(run)
    assert first['status'] == 'known'
    monkeypatch.setattr(recovery, 'native_host_boot_identity', lambda: 'different-boot-generation')
    second = recovery.observe_task_review_recovery_evidence(run)
    assert second['status'] == 'known'
    assert first['evidenceSha256'] != second['evidenceSha256']
    assert before == {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}
    from core.task_snapshot_review import role_owned_process_cleanup as cleanup
    monkeypatch.setattr(cleanup, 'native_host_boot_identity', lambda: 'different-boot-generation')
    reconcile_registered_role_owned_process_cleanup(run)
    assert read_json(cleanup_file)['status'] == 'exited'
    assert len(list(run.glob('reviewer-*/attempt-*/model-invocation.json'))) == 2
    assert resume_review(run)['agentContinuationNotice']['noticeId'] == original_notice_id
    assert len(list(run.glob('reviewer-*/attempt-*/model-invocation.json'))) == 2


def test_owner_exit_receipt_is_readonly_recovery_input_without_model_redispatch(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import review_recovery_evidence_observation as recovery
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, '-c', 'pass']])
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    supervision_file = next(run.glob('final-validation/attempt-*/command-*/command-supervision.json'))
    supervision = read_json(supervision_file)
    write_json(supervision_file, {**supervision, 'status': 'descendant_exit_unproven',
        'ownership_observation_coverage_status': 'unknown'})
    receipt = [{'status': 'unknown', 'source': 'registered_owner_exit_receipt',
        'reason': 'coverage_unproven', 'evidence_sha256': 'weak-receipt'}]
    monkeypatch.setattr(recovery, 'native_host_boot_identity', lambda: supervision['host_boot_identity'])
    monkeypatch.setattr(recovery, 'observe_registered_command_owner_exit_receipt',
        lambda *_args: receipt[0])
    model_invocations = sorted(run.glob('reviewer-*/attempt-*/model-invocation.json'))

    first = recovery.observe_task_review_recovery_evidence(run)
    before = {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}
    assert first['status'] == 'known'
    assert recovery.observe_task_review_recovery_evidence(run) == first
    receipt[0] = {'status': 'exited', 'source': 'linux_kernel_child_subreaper_exit_receipt',
        'evidence_sha256': 'closed-native-receipt'}
    changed = recovery.observe_task_review_recovery_evidence(run)

    assert changed['status'] == 'known'
    assert changed['evidenceSha256'] != first['evidenceSha256']
    assert recovery.observe_task_review_recovery_evidence(run) == changed
    assert {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()} == before
    assert sorted(run.glob('reviewer-*/attempt-*/model-invocation.json')) == model_invocations

    from core.task_snapshot_review import registered_command_owner_exit_receipt as owner_receipt
    monkeypatch.setattr(owner_receipt, 'observe_registered_command_owner_exit_receipt', lambda *_args: receipt[0])
    record = read_json(run / 'run.json')
    write_json(run / 'run.json', {**record, 'status': 'ready_for_validation'})
    validation = read_json(run / 'validation.json')
    write_json(run / 'validation.json', {**validation, 'status': 'machine_evidence_pending'})

    resumed = resume_review(run)

    assert resumed['status'] == 'review_and_validation_completed'
    assert read_json(supervision_file.parent / 'descendant-exit-reconciliation.json')['status'] == 'exited'
    assert sorted(run.glob('reviewer-*/attempt-*/model-invocation.json')) == model_invocations
