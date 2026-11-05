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
    from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility

    run = shared_snapshot(repository, tmp_path, [[sys.executable, "-c", "pass"]])
    command_execution_log = tmp_path / "credential-failure-command-executions.log"
    command = ("import os,sys\nfrom pathlib import Path\n"
               f"with Path({str(command_execution_log)!r}).open('a') as executions:\n"
               "    executions.write(os.environ['RVF_ROLE_INPUT']+'\\n')\n"
               "print('Invalid API key', file=sys.stderr)\nsys.exit(1)\n")
    config = {"command": [sys.executable, "-c", command], "output_format": "text"}

    def actual_invocation_count():
        executed_role_inputs = command_execution_log.read_text().splitlines()
        invocation_role_inputs = [read_json(path.parent / "request.json")["input_file"]
                                  for path in run.glob("reviewer-*/attempt-*/model-invocation.json")]
        assert sorted(executed_role_inputs) == sorted(invocation_role_inputs)
        return len(executed_role_inputs)

    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    original_invocation_count = actual_invocation_count()
    # 两个 reviewer 并发时，首次认证失败前可能已有一个或两个调用启动。
    assert original_invocation_count in (1, 2)
    evidence = Path(reviewer_eligibility(config)["evidence_path"])
    original_failure = read_json(evidence)["negative_evidence"]["account"]
    assert original_failure["failure_count"] == original_invocation_count

    monkeypatch.setenv("UNRELATED_MACHINE_OBSERVATION", "changed")
    assert resume_review(run)["status"] == "review_incomplete"
    assert actual_invocation_count() == original_invocation_count
    assert read_json(evidence)["negative_evidence"]["account"] == original_failure

    def assert_recovery_budget_exhausted(result):
        assert result["status"] == "review_incomplete"
        assert actual_invocation_count() == 2
        assert read_json(evidence)["negative_evidence"]["account"]["failure_count"] == 2
        eligibility = reviewer_eligibility(config)
        assert eligibility["status"] == "unavailable"
        assert not eligibility["can_dispatch"]
        assert eligibility["reason"] == "persistent_authentication"
        assert eligibility["recovery"]["maximum_unchanged_retries"] == 0
        assert result["recovery"]["maximum_unchanged_retries"] == 0

    availability = read_json(evidence)
    availability["negative_evidence"]["account"]["retry_not_before"] = time.time() - 1
    write_json(evidence, availability)
    remaining_invocation_budget = 2 - original_invocation_count
    assert reviewer_eligibility(config)["recovery"]["maximum_unchanged_retries"] == remaining_invocation_budget
    assert_recovery_budget_exhausted(resume_review(run))

    availability = read_json(evidence)
    availability["negative_evidence"]["account"]["retry_not_before"] = time.time() - 1
    write_json(evidence, availability)
    assert_recovery_budget_exhausted(resume_review(run))
    assert read_json(evidence)["negative_evidence"]["account"] == availability["negative_evidence"]["account"]
    monkeypatch.setenv("UNRELATED_MACHINE_OBSERVATION", "changed-again")
    assert_recovery_budget_exhausted(resume_review(run))
    assert read_json(evidence)["negative_evidence"]["account"] == availability["negative_evidence"]["account"]


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


def test_historical_timeout_does_not_override_current_success(repository, tmp_path):
    from core.task_snapshot_review.workflow import configuration_digest
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    config = fake_config(tmp_path)
    history = {}
    for role in ("reviewer-1", "reviewer-2"):
        attempt = run / role / "attempt-001"
        write_json(attempt / "request.json", {"configuration_digest": configuration_digest(config),
            "candidate_commit": read_json(run / "run.json")["candidate_commit"], "config": config, "findings": []})
        outcome = {"status": "failed", "returncode": 124, "retryable": False, "recovery": {"category": "deadline_exhausted"}}
        write_json(attempt / "outcome.json", outcome)
        write_json(attempt.parent / "deadline-exhausted.json", outcome)
        for path in (attempt / "outcome.json", attempt.parent / "deadline-exhausted.json"):
            history[path] = path.read_bytes()
    result = resume_review(run, reviewer_configs=[config, config])
    assert result["status"] == "ready_to_commit"
    assert result["disposition"] == "agent_review_ready"
    assert result["closeout"]["automatic_commit_allowed"]
    assert all(path.read_bytes() == content for path, content in history.items())
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 4
    assert resume_review(run)["status"] == "ready_to_commit"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 4


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
