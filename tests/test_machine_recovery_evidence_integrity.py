"""机器事实、故障预算及跨 controller 接续的真实边界回归。"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from test_automatic_task_review_progression import automatic_snapshot
from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


def shared_snapshot(repository, tmp_path, commands, *, preparation_commands=None):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "shared-machine-recovery"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"verify recovery boundaries",
                    run_directory=run, execution_plan={"fix": True, "machine_evidence_commands": commands,
                        "preparation_commands": preparation_commands or [], "validation_commands": [[sys.executable, "-c", "pass"]]})
    return run


def test_successful_machine_output_change_resumes_incomplete_role(repository, tmp_path):
    marker = tmp_path / "service-state"
    marker.write_text("pending")
    run = shared_snapshot(repository, tmp_path, [[sys.executable, "-c", f"from pathlib import Path; print(Path({str(marker)!r}).read_text())"]])
    reviewer = tmp_path / "machine-output-reviewer.py"
    reviewer.write_text("import json,os\nfrom pathlib import Path\nr=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\ne=Path(r['shared_machine_evidence']).parent/'commands/command-001/stdout.txt'\nready=e.read_text().strip()=='ready'\nprint(json.dumps({'status':'completed' if ready else 'incomplete','audit_summary':'核对机器事实','findings':[], **({} if ready else {'requests':['等待服务就绪']})}))\n")
    config = {"command": [sys.executable, str(reviewer)], "output_format": "text"}
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    original_digest = read_json(run / "run.json")["shared_machine_evidence_recovery_sha256"]
    marker.write_text("ready")
    assert resume_review(run)["status"] == "ready_to_commit"
    assert read_json(run / "run.json")["shared_machine_evidence_recovery_sha256"] != original_digest
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 4


def test_unrelated_environment_does_not_reset_credential_cooldown(repository, tmp_path, monkeypatch):
    run = shared_snapshot(repository, tmp_path, [[sys.executable, "-c", "pass"]])
    config = {"command": [sys.executable, "-c", "import sys; print('Invalid API key', file=sys.stderr); sys.exit(1)"], "output_format": "text"}
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    monkeypatch.setenv("UNRELATED_MACHINE_OBSERVATION", "changed")
    assert resume_review(run)["status"] == "review_incomplete"
    original_attempt_count = len(list(run.glob("reviewer-*/attempt-*/request.json")))
    from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility
    evidence = Path(reviewer_eligibility(config)['evidence_path'])
    availability = read_json(evidence)
    failure = availability['negative_evidence']['account']
    expected_additional_attempts = int(failure['failure_count'] < 2)
    failure['retry_not_before'] = time.time() - 1
    write_json(evidence, availability)
    resume_review(run)
    final_attempt_count = original_attempt_count + expected_additional_attempts
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == final_attempt_count
    monkeypatch.setenv("UNRELATED_MACHINE_OBSERVATION", "changed-again")
    resume_review(run)
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == final_attempt_count


@pytest.mark.parametrize("drift_kind", ["environment", "dependency"])
def test_passed_validation_requires_current_environment_evidence(repository, tmp_path, monkeypatch, drift_kind):
    from core.task_snapshot_review.workflow import _validate
    from core.task_snapshot_review.persistent_artifacts import exclusive_lock
    marker = tmp_path / "required-dependency"
    marker.write_text("ready")
    monkeypatch.setenv("REQUIRED_SERVICE_STATE", "ready")
    command = [sys.executable, "-c", f"import os; from pathlib import Path; assert os.environ['REQUIRED_SERVICE_STATE']=='ready'; assert Path({str(marker)!r}).read_text()=='ready'"]
    run = automatic_snapshot(repository, tmp_path, commands=[command])
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "ready_to_commit"
    if drift_kind == "environment":
        monkeypatch.setenv("REQUIRED_SERVICE_STATE", "unavailable")
    else:
        marker.write_text("unavailable")
    with exclusive_lock(run / "workflow.lock") as lock_handle:
        assert _validate(run, read_json(run / "run.json"), [command], lock_handle)["status"] == "failed"
    assert len(list(run.glob("final-validation/attempt-*"))) == 2
    assert read_json(run / "final-validation/attempt-001/validation.json")["status"] == "passed"


def test_changed_environment_revokes_commit_eligibility_and_enters_validation_repair(repository, tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRED_SERVICE_STATE", "ready")
    command = [sys.executable, "-c", "import os; assert os.environ['REQUIRED_SERVICE_STATE']=='ready'"]
    run = automatic_snapshot(repository, tmp_path, commands=[command])
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["closeout"]["automatic_commit_allowed"]
    fixer = tmp_path / "pending-validation-repair.py"
    fixer.write_text("import json,os\nfrom pathlib import Path\nr=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\nassert r['candidate_commit']==r['validation_failure']['tested_commit']\nprint(json.dumps({'status':'incomplete','dispositions':[]}))\n")
    monkeypatch.setenv("REQUIRED_SERVICE_STATE", "unavailable")
    result = resume_review(run, fixer_config={"command": [sys.executable, str(fixer)], "output_format": "text"})
    assert result["status"] == "fix_incomplete"
    assert not result["closeout"]["automatic_commit_allowed"]
    assert read_json(run / "validation.json")["status"] == "failed"


def test_expired_role_without_outcome_is_a_durable_judgment_result(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    for role in ("reviewer-1", "reviewer-2"):
        write_json(run / role / "role-deadline.json", {"deadline_at": time.time() - 1})
    config = fake_config(tmp_path)
    result = resume_review(run, reviewer_configs=[config, config])
    assert result["status"] == "review_incomplete"
    assert result["disposition"] == "agent_judgment_required"
    assert not result["closeout"]["automatic_commit_allowed"]
    assert list(run.glob("reviewer-*/deadline-exhausted.json"))
    assert resume_review(run)["disposition"] == "agent_judgment_required"


def test_shared_command_mutation_cannot_publish_candidate_pass(repository, tmp_path):
    command = [sys.executable, "-c", "from pathlib import Path; Path('task.py').write_text('value = 999\\n')"]
    later_marker = tmp_path / "later-command-ran"
    run = shared_snapshot(repository, tmp_path, [command, [sys.executable, "-c", f"from pathlib import Path; Path({str(later_marker)!r}).touch()"]])
    evidence = collect_shared_machine_evidence(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"))
    assert evidence["status"] == "candidate_changed"
    assert evidence["commands"][0]["source_tree_before"] != evidence["commands"][0]["source_tree_after"]
    assert not later_marker.exists()
    assert (repository / "task.py").read_text() == "value = 2\n"


def test_unchanged_preparation_failure_has_stable_recovery_identity(repository, tmp_path):
    preparation = [sys.executable, "-c", "import sys; print('dependency unavailable', file=sys.stderr); sys.exit(1)"]
    run = shared_snapshot(repository, tmp_path, [[sys.executable, "-c", "pass"]], preparation_commands=[preparation])
    config = fake_config(tmp_path)
    first = resume_review(run, reviewer_configs=[config, config])
    second = resume_review(run)
    assert first["disposition"] == second["disposition"] == "machine_evidence_pending"
    assert first["recovery"]["missing_evidence"] == second["recovery"]["missing_evidence"]
    assert "attempt-" not in second["recovery"]["missing_evidence"]
    assert first["recovery"]["evidence_paths"] == second["recovery"]["evidence_paths"]


def test_live_machine_command_keeps_single_flight_after_controller_crash(repository, tmp_path):
    executions = tmp_path / "machine-command-executions"
    release = tmp_path / "release-machine-command"
    command = [sys.executable, "-c", f"import time,os; from pathlib import Path; p=Path({str(executions)!r});\nwith p.open('a') as f: f.write(str(os.getpid())+'\\n')\nwhile not Path({str(release)!r}).exists(): time.sleep(0.02)"]
    run = shared_snapshot(repository, tmp_path, [command])
    config = fake_config(tmp_path)
    source = Path(__file__).resolve().parents[1]
    controller_program = f"import sys; sys.path.insert(0,{str(source)!r}); from pathlib import Path; from core.task_snapshot_review.workflow import resume_review; resume_review(Path({str(run)!r}), reviewer_configs={([config, config])!r})"
    first = subprocess.Popen([sys.executable, "-c", controller_program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    second = None
    try:
        deadline = time.monotonic() + 5
        while not executions.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        first.kill()
        first.wait(timeout=5)
        second = subprocess.Popen([sys.executable, "-c", controller_program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        assert len(executions.read_text().splitlines()) == 1
        release.touch()
        assert second.wait(timeout=10) == 0
    finally:
        release.touch()
        for controller in (first, second):
            if controller and controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)
        if executions.exists():
            for pid in executions.read_text().splitlines():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
