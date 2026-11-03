"""缺失席机器请求由宿主执行，心跳不能形成恢复输入。"""
import hashlib
import json
import sys
import os
import subprocess
import time
from pathlib import Path

import pytest

from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review
from test_task_snapshot_review_workflow import repository, canary


def requested_machine_snapshot(repository, tmp_path, command=None, dependency_source_paths=None):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "reviewer-machine-evidence-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"verify host recovery",
        run_directory=run, execution_plan={"fix": False, "validation_commands": [command or [sys.executable, "-c", "print('machine proof')"]],
            "machine_context": {"dependency_source_paths": dependency_source_paths or []}})
    return run


def machine_request_reviewer(tmp_path, request_kind="structured"):
    script = tmp_path / "machine-request-reviewer.py"
    script.write_text(
        "import json,os\nfrom pathlib import Path\n"
        "r=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "pending='reviewer-2' in r['input_file'] and not r.get('reviewer_machine_evidence')\n"
        "result={'status':'incomplete' if pending else 'completed','findings':[],'audit_summary':'保留独立审阅与机器事实'}\n"
        f"if pending: result['requests']=['需要已登记机器验证'] if {request_kind!r}=='text' else [{{'kind':'machine_evidence','binding':r['machine_evidence_request_binding'],'registered_command_references':[{{'command_collection':'validation_commands','command_index':0}}]}}]\n"
        "if r.get('reviewer_machine_evidence'):\n"
        " proof=json.loads(Path(r['reviewer_machine_evidence'][0]).read_text())\n"
        " assert proof['status']=='passed'\n"
        " assert proof['source_tree_before']==proof['source_tree_after']==r['machine_evidence_request_binding']['candidate_tree']\n"
        "print(json.dumps(result,ensure_ascii=False))\n")
    return {"command": [sys.executable, str(script)], "output_format": "text"}


def test_registered_reviewer_request_collects_host_evidence_and_only_resumes_missing_seat(repository, tmp_path):
    run = requested_machine_snapshot(repository, tmp_path)
    before = canary(repository)
    config = machine_request_reviewer(tmp_path)
    result = resume_review(run, reviewer_configs=[config, config])
    assert result["status"] == "ready_to_commit"
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 2
    assert read_json(run / "reviewer-2/attempt-001/result.json")["status"] == "incomplete"
    assert canary(repository) == before
    evidence = list(run.glob("reviewer-machine-evidence-requests/*/attempt-*/evidence.json"))
    assert len(evidence) == 1
    assert read_json(evidence[0])["commands"][0]["stdout_sha256"] == hashlib.sha256(b"machine proof\n").hexdigest()
    assert resume_review(run)["status"] == "ready_to_commit"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 3


def registered_request(run):
    from core.task_snapshot_review.review_recovery_evidence_observation import machine_evidence_request_binding
    return {"kind": "machine_evidence", "binding": machine_evidence_request_binding(run, read_json(run / "run.json")),
        "registered_command_references": [{"command_collection": "validation_commands", "command_index": 0}]}


@pytest.mark.parametrize("mismatch", ["tree", "environment", "dependency", "command", "logs"])
def test_mismatched_original_is_not_reused_and_missing_proof_is_collected(repository, tmp_path, monkeypatch, mismatch):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    dependency = tmp_path / "frozen-dependency-content"
    dependency.write_text("dependency-v1")
    run = requested_machine_snapshot(repository, tmp_path, dependency_source_paths=[str(dependency)])
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    request = registered_request(run)
    first = collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
    original = first.read_bytes()
    proof = read_json(first)
    assert collect_registered_reviewer_machine_evidence_request(run, record, plan, request, []) == first
    if mismatch == "tree":
        proof["binding"]["candidate_tree"] = "0" * 40
        write_json(first, proof)
    elif mismatch == "command":
        proof["commands"][0]["argv"] = ["unregistered-command"]
        write_json(first, proof)
    elif mismatch == "logs":
        (Path(proof["commands"][0]["original_execution_directory"]) / "stdout.txt").write_text("unbound historical output")
    elif mismatch == "environment":
        monkeypatch.setenv("REGISTERED_MACHINE_SERVICE_VERSION", "changed")
    else:
        dependency.write_text("dependency-v2")
    second = collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
    assert second != first
    assert read_json(second)["status"] == "passed"
    if mismatch not in {"tree", "command"}:
        assert first.read_bytes() == original
    assert len(list(first.parent.parent.glob("attempt-*"))) == 2


@pytest.mark.parametrize("invalid", ["free_text", "foreign_run", "extra_argv", "boolean_index"])
def test_machine_request_cannot_extend_command_authorization(repository, tmp_path, invalid):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    run = requested_machine_snapshot(repository, tmp_path)
    request = registered_request(run)
    if invalid == "free_text":
        request = "execute arbitrary command"
    elif invalid == "foreign_run":
        request["binding"]["run_id"] = "foreign-run"
    elif invalid == "extra_argv":
        request["registered_command_references"][0]["argv"] = ["unregistered-command"]
    else:
        request["registered_command_references"][0]["command_index"] = True
    with pytest.raises(ValueError):
        collect_registered_reviewer_machine_evidence_request(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"), request, [])
    assert not list(run.glob("reviewer-machine-evidence-requests/*/attempt-*"))


def test_legacy_request_is_appended_idempotently_and_cannot_revive_revocation(repository, tmp_path):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import register_reviewer_machine_evidence_request
    from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked
    run = requested_machine_snapshot(repository, tmp_path)
    config = machine_request_reviewer(tmp_path, "text")
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    notices = read_json(run / "agent-continuation-notices.json")["notices"]
    assert [notice["stage"] for notice in notices] == ["machine_evidence_waiting"]
    source = run / "reviewer-2/attempt-001/result.json"
    original = source.read_bytes()
    args = {"source_role_result_file": source, "source_role_result_sha256": hashlib.sha256(original).hexdigest(),
        "source_request_index": 0, "request": registered_request(run)}
    registration = register_reviewer_machine_evidence_request(run, **args)
    assert register_reviewer_machine_evidence_request(run, **args) == registration
    assert resume_review(run)["status"] == "ready_to_commit"
    assert source.read_bytes() == original
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    write_json(run / "execution-revocation.json", {"reason": "用户 Stop"})
    with pytest.raises(ExecutionRevoked):
        register_reviewer_machine_evidence_request(run, **args)


@pytest.mark.parametrize("failure_kind", ["deterministic", "transient"])
def test_host_attempt_budget_survives_controllers_and_heartbeat_changes(repository, tmp_path, monkeypatch, failure_kind):
    import time
    failure = "Operation not permitted" if failure_kind == "deterministic" else "temporary host failure"
    command = [sys.executable, "-c", f"import sys; print({failure!r},file=sys.stderr); sys.exit(1)"]
    run = requested_machine_snapshot(repository, tmp_path, command)
    config = machine_request_reviewer(tmp_path)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    for heartbeat in range(6):
        monkeypatch.setenv("KANBAN_CONTROLLER_NONCE", str(heartbeat))
        now += 6
        result = resume_review(run, reviewer_configs=[config, config] if heartbeat == 0 else None)
        assert result["disposition"] == "machine_evidence_pending"
    assert len(list(run.glob("reviewer-machine-evidence-requests/*/attempt-*"))) == (1 if failure_kind == "deterministic" else 3)
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2
    assert len(read_json(run / "agent-continuation-notices.json")["notices"]) == 1


def test_host_evidence_environment_isolates_control_plane_provider_and_codex_storage(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    command = [sys.executable, "-c", "import os; from pathlib import Path; "
        "assert not any(k in os.environ for k in ['KANBAN_CONTROL_ENDPOINT','OPENAI_API_KEY','RVF_ROLE_INPUT']); "
        "assert all(Path(os.environ[k]).is_dir() for k in ['HOME','CODEX_HOME','CODEX_SQLITE_HOME']); "
        "assert len(set(os.environ[k] for k in ['HOME','CODEX_HOME','CODEX_SQLITE_HOME']))==3; print('isolated host proof')"]
    run = requested_machine_snapshot(repository, tmp_path, command)
    for key in ("KANBAN_CONTROL_ENDPOINT", "OPENAI_API_KEY", "RVF_ROLE_INPUT"):
        monkeypatch.setenv(key, "must-not-be-inherited")
    evidence = collect_registered_reviewer_machine_evidence_request(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"), registered_request(run), [])
    assert read_json(evidence)["status"] == "passed"
    assert "must-not-be-inherited" not in evidence.read_text()


@pytest.mark.parametrize("collection_kind", ["reviewer_request", "shared_machine_evidence"])
@pytest.mark.parametrize("crash_during", ["machine_command", "workspace_preparation"])
def test_controller_crash_reconciles_native_result_without_reexecuting_command(repository, tmp_path, collection_kind, crash_during):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    invocation_log = tmp_path / "actual-host-command-invocations"
    release = tmp_path / "host-command-may-exit"
    command = [sys.executable, "-c", "from pathlib import Path; import time\n"
        f"with Path({str(invocation_log)!r}).open('a') as log: log.write('executed\\n')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.02)\nprint('native host fact')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "interrupted-host-collection-run"
    collection = "validation_commands" if collection_kind == "reviewer_request" else "machine_evidence_commands"
    machine_command = command if crash_during == "machine_command" else [sys.executable, "-c", "print('machine proof')"]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"reconcile interrupted command",
        run_directory=run, execution_plan={"fix": False, collection: [machine_command],
            "preparation_commands": [command] if crash_during == "workspace_preparation" else []})
    source_directory = Path(__file__).resolve().parents[1]
    program = f"import sys; sys.path.insert(0,{str(source_directory)!r}); from pathlib import Path; "
    program += "from core.task_snapshot_review.persistent_artifacts import read_json; "
    program += f"run=Path({str(run)!r}); "
    if collection_kind == "reviewer_request":
        program += "from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request; "
        program += f"collect_registered_reviewer_machine_evidence_request(run,read_json(run/'run.json'),read_json(run/'execution-plan.json'),{registered_request(run)!r},[])"
    else:
        program += "from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence; "
        program += "collect_shared_machine_evidence(run,read_json(run/'run.json'),read_json(run/'execution-plan.json'))"
    controller = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while not invocation_log.exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        controller.kill()
        controller.wait(timeout=5)
        release.touch()
        record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
        if collection_kind == "reviewer_request":
            original = collect_registered_reviewer_machine_evidence_request(run, record, plan, registered_request(run), [])
            proof = read_json(original)
            attempts = list(run.glob("reviewer-machine-evidence-requests/*/attempt-*"))
        else:
            proof = collect_shared_machine_evidence(run, record, plan)
            original = Path(proof["original"])
            attempts = list(run.glob("machine-evidence/attempt-*"))
        assert proof["status"] == "passed"
        if crash_during == "machine_command":
            assert "reconciled_from" in proof["commands"][0]
            if collection_kind == "reviewer_request":
                execution = Path(proof["commands"][0]["original_execution_directory"]) / "execution.json"
            else:
                execution = original.parent / "commands/command-001/execution.json"
        else:
            assert read_json(original.parent / "workspace-preparation-reconciliation.json")["status"] == "passed"
            execution = original.parent / "preparation/command-001/execution.json"
        assert read_json(execution)["status"] == "running"
        assert invocation_log.read_text().splitlines() == ["executed"]
        assert len(attempts) == 1
    finally:
        release.touch()
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


def test_retry_executes_only_missing_registered_command_when_dependencies_are_proven(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    first_log, second_log, dependency = (tmp_path / name for name in ("first-command-invocations", "second-command-invocations", "bound-dependency"))
    dependency.write_text("frozen dependency")
    commands = [
        [sys.executable, "-c", f"from pathlib import Path; p=Path({str(first_log)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1')"],
        [sys.executable, "-c", f"from pathlib import Path; import sys; p=Path({str(second_log)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); sys.exit(1 if len(p.read_text())==1 else 0)"],
    ]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "partial-host-evidence-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"only execute missing proof",
        run_directory=run, execution_plan={"fix": False, "validation_commands": commands,
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    request = registered_request(run)
    request["registered_command_references"].append({"command_collection": "validation_commands", "command_index": 1})
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    assert collect_registered_reviewer_machine_evidence_request(run, record, plan, request, []) is None
    now += 6
    evidence = collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
    assert read_json(evidence)["status"] == "passed"
    assert first_log.read_text() == "1"
    assert second_log.read_text() == "11"


def test_portable_historical_fixtures_keep_source_version_revocation_and_frozen_indexes():
    fixture = read_json(Path(__file__).parent / "fixtures/reviewer-machine-evidence-recovery/three-incomplete-runs-and-same-version-successful-control.json")
    expected = {"dd6f5": [0, 1, 2, 3, 4], "82e07": [3], "11d06": [3, 4]}
    for case in fixture["cases"]:
        assert case["original_incomplete_result"]["status"] == "incomplete"
        assert [ref["command_index"] for ref in case["registered_command_references"]] == expected[case["source_task_id"]]
        assert case["decision_facts_sha256_was_missing"]
        assert case["historical_revoked"] == (case["source_task_id"] != "11d06")
        for ref in case["registered_command_references"]:
            assert case["frozen_execution_plan"][ref["command_collection"]][ref["command_index"]]
        assert all(len(digest) == 64 for digest in case["originals_sha256"].values())
        assert case["source_version"]["plugin_version"] == fixture["successful_control"]["source_version"]["plugin_version"]
    assert fixture["successful_control"]["last_reviewer_result_status"] == "completed"


def test_shared_machine_recollection_deduplicates_facts_and_controller_nonce_cannot_reset_budget(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    from core.task_snapshot_review.review_recovery_evidence_observation import observe_task_review_recovery_evidence
    invocation_log = tmp_path / "shared-host-command-invocations"
    command = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(invocation_log)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); print('same proof')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "shared-machine-fact-budget-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"shared host facts do not reset budget",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": [command]})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    results = []
    first = None
    for nonce in range(6):
        monkeypatch.setenv("KANBAN_CONTROLLER_NONCE", str(nonce))
        results.append(collect_shared_machine_evidence(run, record, plan))
        observation = observe_task_review_recovery_evidence(run)
        assert observation["status"] == "known"
        if first is None:
            first = observation["evidenceSha256"]
        assert observation["evidenceSha256"] == first
    assert [item["status"] if item else None for item in results] == ["passed", "passed", "passed", None, None, None]
    assert all(item["dependency_identity_proven"] is False for item in results[:3])
    assert invocation_log.read_text() == "111"
    assert len(list((run / "machine-evidence").glob("attempt-*"))) == 3


def test_unproven_shared_pass_is_withheld_after_budget_exhaustion(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    for variable in ("HOME", "CODEX_HOME", "CODEX_SQLITE_HOME", "RVF_USER_LOCAL_DATA_ROOT"):
        isolated_directory = tmp_path / ("isolated-" + variable.lower().replace("_", "-"))
        isolated_directory.mkdir()
        monkeypatch.setenv(variable, str(isolated_directory))
    for variable in ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID"):
        monkeypatch.delenv(variable, raising=False)
    readiness = tmp_path / "undeclared-service-readiness"
    readiness.write_text("ready")
    invocations = tmp_path / "host-command-invocations"
    command = [sys.executable, "-c", (
        "from pathlib import Path; "
        f"p=Path({str(invocations)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); "
        f"assert Path({str(readiness)!r}).read_text() == 'ready'; print('live service passed')"
    )]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "unproven-dependency-exhausted-host-budget-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"],
        requirements=b"unknown dependency identity cannot qualify historical passes",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": [command]})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    for _ in range(3):
        proof = collect_shared_machine_evidence(run, record, plan)
        assert proof["status"] == "passed" and proof["dependency_identity_proven"] is False
    originals = {path: path.read_bytes() for path in (run / "machine-evidence").glob("attempt-*/evidence.json")}
    readiness.write_text("unavailable")
    current = collect_shared_machine_evidence(run, record, plan)
    assert invocations.read_text() == "111"
    assert len(originals) == 3 and all(path.read_bytes() == content for path, content in originals.items())
    assert current is None


def test_qualified_shared_original_satisfies_registered_final_command_without_new_execution(repository, tmp_path):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    dependency = tmp_path / "declared-exact-dependency"
    dependency.write_text("fixed dependency")
    marker = tmp_path / "same-command-invocations"
    command = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); print('qualified shared proof')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "qualified-shared-original-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"reuse qualified shared command",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": [command], "validation_commands": [command],
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    shared = collect_shared_machine_evidence(run, record, plan)
    assert shared["dependency_identity_proven"]
    evidence = collect_registered_reviewer_machine_evidence_request(run, record, plan, registered_request(run), [])
    proof = read_json(evidence)
    assert proof["status"] == "passed" and proof["execution_started"] is False
    assert marker.read_text() == "1"
    assert not (evidence.parent / "workspace").exists()


def test_stop_prevents_new_collection_and_source_mutation_is_not_published_as_pass(repository, tmp_path):
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked
    run = requested_machine_snapshot(repository, tmp_path, [sys.executable, "-c", "from pathlib import Path; Path('task.py').write_text('mutated')"])
    before = canary(repository)
    assert collect_registered_reviewer_machine_evidence_request(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"), registered_request(run), []) is None
    assert read_json(next(run.glob("reviewer-machine-evidence-requests/*/attempt-*/evidence.json")))["status"] == "candidate_changed"
    assert canary(repository) == before
    write_json(run / "execution-revocation.json", {"reason": "用户 Stop"})
    originals = {path: path.read_bytes() for path in run.glob("reviewer-machine-evidence-requests/*/*.json")}
    with pytest.raises(ExecutionRevoked):
        collect_registered_reviewer_machine_evidence_request(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"), registered_request(run), [])
    assert all(path.read_bytes() == value for path, value in originals.items())


def test_recovery_observation_ignores_thousand_heartbeats_and_retains_readonly_state(repository, tmp_path):
    from core.task_snapshot_review.review_recovery_evidence_observation import observe_task_review_recovery_evidence
    run = requested_machine_snapshot(repository, tmp_path)
    before = canary(repository)
    first = observe_task_review_recovery_evidence(run)
    assert first["status"] == "known"
    for heartbeat in range(1000):
        write_json(run / "reviewer-2/progress.json", {"last_observed_at": heartbeat, "controller_nonce": str(heartbeat)})
        assert observe_task_review_recovery_evidence(run)["evidenceSha256"] == first["evidenceSha256"]
    paths_before = {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    assert observe_task_review_recovery_evidence(run) == first
    assert {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()} == paths_before
    write_json(run / "context-response-originals-by-sha256/real-evidence.json", {"new": "machine proof"})
    assert observe_task_review_recovery_evidence(run)["evidenceSha256"] != first["evidenceSha256"]
    assert canary(repository) == before


def test_proven_shared_pass_is_not_reexecuted_or_replaced(repository, tmp_path):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    dependency = tmp_path / "stable-dependency"
    dependency.write_text("stable")
    marker = tmp_path / "shared-command-count"
    command = [sys.executable, "-c", "from pathlib import Path; import sys; "
        f"p=Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '')+'1'); sys.exit(0 if p.read_text()=='1' else 1)"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "proven-shared-pass-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"reuse proven shared pass",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": [command],
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    first = collect_shared_machine_evidence(run, record, plan)
    assert first["status"] == "passed" and first["dependency_identity_proven"] is True
    second = collect_shared_machine_evidence(run, record, plan)
    assert second["original"] == first["original"] and second["status"] == "passed"
    assert marker.read_text() == "1"
    assert len(list((run / "machine-evidence").glob("attempt-*"))) == 1


@pytest.mark.parametrize("mismatch", ["tree", "environment", "prepared_dependency"])
def test_unqualified_shared_pass_is_recollected_until_budget_then_withheld(repository, tmp_path, mismatch):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence, prepared_workspace_dependency_identity
    from core.task_snapshot_review.review_recovery_evidence_observation import registered_host_recovery_inputs
    dependency = tmp_path / "declared-dependency-source"
    dependency.write_text("verified dependency")
    marker = tmp_path / "shared-qualification-count"
    (repository / ".gitignore").write_text("installed-dependency.py\n")
    (repository / "task.py").write_text("value = 2\n")
    preparation = [sys.executable, "-c",
        f"from pathlib import Path; Path('installed-dependency.py').write_text(Path({str(dependency)!r}).read_text())"]
    command = [sys.executable, "-c", "from pathlib import Path; "
        f"p=Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '')+'1')"]
    run = tmp_path / "shared-cache-qualification-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py", ".gitignore"],
        requirements=b"qualify shared cache before reuse", run_directory=run,
        execution_plan={"fix": False, "preparation_commands": [preparation], "machine_evidence_commands": [command],
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")

    def tamper(evidence):
        original = Path(evidence["original"])
        before = original.read_bytes()
        if mismatch == "tree":
            evidence["tested_tree"] = "0" * 40
            write_json(original, evidence)
        elif mismatch == "environment":
            evidence["runtime_environment"]["environment_sha256"] = "0" * 64
            write_json(original, evidence)
        else:
            (Path(evidence["cwd"]) / "installed-dependency.py").write_text("changed after evidence")
        return before, original.read_bytes()

    current = collect_shared_machine_evidence(run, record, plan)
    assert current["status"] == "passed" and current["dependency_identity_proven"] is True
    expected_tree = current["tested_tree"]
    _, damaged = tamper(current)
    recollected = collect_shared_machine_evidence(run, record, plan)
    assert recollected["status"] == "passed" and recollected["original"] != current["original"]
    assert recollected["tested_tree"] == expected_tree
    assert recollected["runtime_environment"] == registered_host_recovery_inputs(run, plan)
    for result in recollected["commands"]:
        captured = result["prepared_workspace_dependency_identity"]
        assert prepared_workspace_dependency_identity(Path(result["cwd"]), captured["preparation_created_paths"]) == captured
    assert marker.read_text() == "11"
    assert Path(current["original"]).read_bytes() == damaged
    if mismatch == "prepared_dependency":
        assert (Path(current["cwd"]) / "installed-dependency.py").read_text() == "changed after evidence"
    assert len(list((run / "machine-evidence").glob("attempt-*"))) == 2
    current = recollected
    _, damaged = tamper(current)
    third = collect_shared_machine_evidence(run, record, plan)
    assert third["status"] == "passed" and third["original"] != current["original"]
    assert marker.read_text() == "111"
    assert Path(current["original"]).read_bytes() == damaged
    _, damaged = tamper(third)
    withheld = collect_shared_machine_evidence(run, record, plan)
    assert withheld is None or withheld.get("status") != "passed"
    assert marker.read_text() == "111"
    assert len(list((run / "machine-evidence").glob("attempt-*"))) == 3
    assert Path(third["original"]).read_bytes() == damaged


def test_reconciled_shared_evidence_keeps_dependency_proof(repository, tmp_path):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    dependency = tmp_path / "reconciled-dependency"
    dependency.write_text("stable")
    marker = tmp_path / "reconciled-command-count"
    command = [sys.executable, "-c", "from pathlib import Path; "
        f"p=Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '')+'1')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "reconciled-dependency-proof-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"keep reconciled dependency proof",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": [command], "validation_commands": [command],
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    shared = collect_shared_machine_evidence(run, record, plan)
    assert shared["dependency_identity_proven"] is True
    Path(shared["original"]).unlink()
    reconciled = collect_shared_machine_evidence(run, record, plan)
    assert marker.read_text() == "1"
    assert reconciled["status"] == "passed" and reconciled["dependency_identity_proven"] is True
    assert reconciled["commands"][0]["prepared_workspace_dependency_identity"]["identity_proven"] is True
    evidence = collect_registered_reviewer_machine_evidence_request(run, record, plan, registered_request(run), [])
    assert read_json(evidence)["execution_started"] is False
    assert marker.read_text() == "1"


def test_symlink_dependency_sources_and_ignored_symlink_files_stay_unproven(repository, tmp_path):
    from core.task_snapshot_review.machine_evidence import prepared_workspace_dependency_identity
    from core.task_snapshot_review.review_recovery_evidence_observation import registered_dependency_sources_identity
    target = tmp_path / "real-dependency"
    target.mkdir()
    (target / "package.json").write_text("{}\n")
    root_link = tmp_path / "dependency-link"
    root_link.symlink_to(target, target_is_directory=True)
    parent = tmp_path / "dependency-parent"
    parent.mkdir()
    (parent / "dependency-link").symlink_to(target, target_is_directory=True)
    assert registered_dependency_sources_identity({"machine_context": {"dependency_source_paths": [str(root_link)]}})["identity_proven"] is False
    assert registered_dependency_sources_identity({"machine_context": {"dependency_source_paths": [str(parent)]}})["identity_proven"] is False
    assert registered_dependency_sources_identity({"machine_context": {"dependency_source_paths": [str(target / "package.json")]}})["identity_proven"] is True
    tool = tmp_path / "tool"
    tool.write_text("echo tool\n")
    ignored = repository / "node_modules" / ".bin"
    ignored.mkdir(parents=True)
    (ignored / "tool").symlink_to(tool)
    with (repository / ".git/info/exclude").open("a") as excluded:
        excluded.write("node_modules/\n")
    assert prepared_workspace_dependency_identity(repository)["identity_proven"] is False


def test_unexecuted_later_command_keeps_its_attempt_budget(repository, tmp_path):
    from core.task_snapshot_review.machine_evidence import collect_shared_machine_evidence
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    dependency = tmp_path / "budget-dependency"
    dependency.write_text("stable")
    second_marker = tmp_path / "second-command"
    commands = [
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        [sys.executable, "-c", f"from pathlib import Path; Path({str(second_marker)!r}).write_text('ran')"],
    ]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "unexecuted-command-budget-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"do not charge unexecuted commands",
        run_directory=run, execution_plan={"fix": False, "machine_evidence_commands": commands, "validation_commands": [commands[1]],
            "machine_context": {"dependency_source_paths": [str(dependency)]}})
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    for _ in range(3):
        assert collect_shared_machine_evidence(run, record, plan)["status"] == "failed"
    assert not second_marker.exists()
    evidence = collect_registered_reviewer_machine_evidence_request(run, record, plan, registered_request(run), [])
    assert read_json(evidence)["status"] == "passed"
    assert second_marker.read_text() == "ran"


def test_unknown_command_is_not_reexecuted_for_another_request_group(repository, tmp_path):
    import signal
    from core.task_snapshot_review.machine_evidence import WorkspacePreparationFailed
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    invocation = tmp_path / "unknown-command-invocations"
    release = tmp_path / "unknown-command-may-exit"
    command = [sys.executable, "-c", "from pathlib import Path; import time\n"
        f"with Path({str(invocation)!r}).open('a') as log: log.write('executed\\n')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.02)\nprint('unknown command')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "unknown-across-request-groups-run"
    commands = [command, [sys.executable, "-c", "print('second fact')"]]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"do not reexecute unknown commands",
        run_directory=run, execution_plan={"fix": False, "validation_commands": commands})
    request = registered_request(run)
    source_directory = Path(__file__).resolve().parents[1]
    program = f"import sys; sys.path.insert(0,{str(source_directory)!r}); from pathlib import Path; "
    program += "from core.task_snapshot_review.persistent_artifacts import read_json; "
    program += "from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request; "
    program += f"run=Path({str(run)!r}); collect_registered_reviewer_machine_evidence_request(run,read_json(run/'run.json'),read_json(run/'execution-plan.json'),{request!r},[])"
    controller = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not invocation.exists():
            if controller.poll() is not None:
                raise AssertionError(controller.stderr.read().decode())
            assert time.monotonic() < deadline
            time.sleep(.02)
        supervision = next(run.glob("reviewer-machine-evidence-requests/*/attempt-*/commands/**/command-supervision.json"))
        supervisor_pid = read_json(supervision)["supervisor_pid"]
        controller.kill()
        controller.wait(timeout=5)
        os.kill(supervisor_pid, signal.SIGKILL)
        release.touch()
        time.sleep(.3)
        record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
        with pytest.raises(WorkspacePreparationFailed):
            collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
        changed = json.loads(json.dumps(request))
        changed["registered_command_references"].append({"command_collection": "validation_commands", "command_index": 1})
        with pytest.raises(WorkspacePreparationFailed):
            collect_registered_reviewer_machine_evidence_request(run, record, plan, changed, [])
        assert invocation.read_text().splitlines() == ["executed"]
        assert read_json(supervision)["status"] != "completed"
    finally:
        release.touch()
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


@pytest.mark.parametrize("launch_site", ["machine_command", "preparation"])
def test_confirmed_not_started_launch_retries_after_transient_error(repository, tmp_path, launch_site):
    import errno
    from unittest.mock import patch
    from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
    marker = tmp_path / f"started-after-eagain-{launch_site}"
    command = [sys.executable, "-c", "from pathlib import Path; "
        f"p=Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '')+'1')"]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / f"not-started-{launch_site}-run"
    execution_plan = {"fix": False, "validation_commands": [command if launch_site == "machine_command" else [sys.executable, "-c", "print('idle')"]]}
    if launch_site == "preparation":
        execution_plan["preparation_commands"] = [command]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"retry a launch that never started",
        run_directory=run, execution_plan=execution_plan)
    record, plan = read_json(run / "run.json"), read_json(run / "execution-plan.json")
    request = registered_request(run)
    real_popen = subprocess.Popen
    state = {"failed": False}

    def transient_supervisor_launch(argv, *arguments, **keywords):
        if not state["failed"] and len(argv) > 1 and str(argv[1]).endswith("retained_command_supervisor.py"):
            state["failed"] = True
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return real_popen(argv, *arguments, **keywords)

    with patch.object(subprocess, "Popen", transient_supervisor_launch):
        with pytest.raises(BlockingIOError):
            collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
    evidence = collect_registered_reviewer_machine_evidence_request(run, record, plan, request, [])
    assert read_json(evidence)["status"] == "passed"
    assert marker.read_text() == "1"


def test_incomplete_machine_request_is_consumed_while_another_reviewer_runs(repository, tmp_path):
    import concurrent.futures
    marker = tmp_path / "parallel-host-command"
    release = tmp_path / "slow-reviewer-may-finish"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('done')"]
    run = requested_machine_snapshot(repository, tmp_path, command)
    reviewer = tmp_path / "parallel-reviewer.py"
    reviewer.write_text(
        "import json,os,time\nfrom pathlib import Path\n"
        "r=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        f"if 'reviewer-1' in r['input_file']:\n    while not Path({str(release)!r}).exists(): time.sleep(.02)\n"
        "pending='reviewer-2' in r['input_file'] and not r.get('reviewer_machine_evidence')\n"
        "result={'status':'incomplete' if pending else 'completed','findings':[],'audit_summary':'隔离并行审阅'}\n"
        "if pending: result['requests']=[{'kind':'machine_evidence','binding':r['machine_evidence_request_binding'],"
        "'registered_command_references':[{'command_collection':'validation_commands','command_index':0}]}]\n"
        "print(json.dumps(result,ensure_ascii=False))\n")
    config = {"command": [sys.executable, str(reviewer)], "output_format": "text"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resume_review, run, reviewer_configs=[config, config])
        try:
            incomplete = run / "reviewer-2/attempt-001/result.json"
            deadline = time.monotonic() + 20
            while not incomplete.exists():
                if future.done():
                    raise AssertionError(future.result())
                assert time.monotonic() < deadline
                time.sleep(.02)
            deadline = time.monotonic() + 20
            while not marker.exists():
                if future.done():
                    raise AssertionError(future.result())
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert not release.exists()
            assert list(run.glob("reviewer-machine-evidence-requests/*/attempt-*/collection-intent.json"))
            assert future.running()
        finally:
            release.touch()
        result = future.result(timeout=30)
    assert result["status"] == "ready_to_commit"
    assert marker.read_text() == "done"


def test_other_reviewer_finishes_while_machine_collection_is_still_running(repository, tmp_path):
    import concurrent.futures
    started = tmp_path / "host-command-started"
    gate = tmp_path / "host-command-may-finish"
    done = tmp_path / "host-proof-collected"
    release = tmp_path / "remaining-reviewer-may-exit"
    command = [sys.executable, "-c",
        "import time\nfrom pathlib import Path\n"
        f"started,gate,done=Path({str(started)!r}),Path({str(gate)!r}),Path({str(done)!r})\n"
        "started.write_text('1')\n"
        "while not gate.exists(): time.sleep(.02)\n"
        "done.write_text('done')\n"]
    run = requested_machine_snapshot(repository, tmp_path, command)
    reviewer = tmp_path / "reviewer-during-open-collection.py"
    reviewer.write_text(
        "import json,os,time\nfrom pathlib import Path\n"
        "r=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        f"if 'reviewer-1' in r['input_file']:\n    while not Path({str(release)!r}).exists(): time.sleep(.02)\n"
        "pending='reviewer-2' in r['input_file'] and not r.get('reviewer_machine_evidence')\n"
        "result={'status':'incomplete' if pending else 'completed','findings':[],'audit_summary':'采集未完成时其它席继续'}\n"
        "if pending: result['requests']=[{'kind':'machine_evidence','binding':r['machine_evidence_request_binding'],"
        "'registered_command_references':[{'command_collection':'validation_commands','command_index':0}]}]\n"
        "print(json.dumps(result,ensure_ascii=False))\n")
    config = {"command": [sys.executable, str(reviewer)], "output_format": "text"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resume_review, run, reviewer_configs=[config, config])
        try:
            incomplete = run / "reviewer-2/attempt-001/result.json"
            deadline = time.monotonic() + 30
            while not incomplete.exists():
                if future.done():
                    raise AssertionError(future.result())
                assert time.monotonic() < deadline
                time.sleep(.02)
            deadline = time.monotonic() + 30
            while not started.exists():
                if future.done():
                    raise AssertionError(future.result())
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert not done.exists() and not gate.exists()
            release.touch()
            finished = run / "reviewer-1/attempt-001/result.json"
            deadline = time.monotonic() + 30
            while not finished.exists():
                if future.done():
                    raise AssertionError(future.result())
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert read_json(finished)["status"] == "completed"
            assert not gate.exists() and not done.exists() and future.running()
            assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
        finally:
            gate.touch()
            release.touch()
        result = future.result(timeout=40)
    assert result["status"] == "ready_to_commit"
    assert done.read_text() == "done"
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    assert len(list(run.glob("reviewer-machine-evidence-requests/*/attempt-*/evidence.json"))) == 1


def test_eligibility_recheck_time_does_not_renew_recovery_evidence(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import reviewer_account_eligibility as eligibility
    from core.task_snapshot_review.review_recovery_evidence_observation import observe_task_review_recovery_evidence
    run = requested_machine_snapshot(repository, tmp_path)
    (run / "reviewer-1").mkdir()
    config = {"command": [sys.executable, "-c", "pass"], "output_format": "text"}
    base = time.time()
    hashes = []
    for offset in (0, 31, 62):
        monkeypatch.setattr(time, "time", lambda offset=offset: base + offset)
        state = eligibility.reviewer_eligibility(config, run_directory=run)
        write_json(run / "reviewer-1/reviewer-eligibility.json", {"observations": [state]})
        observation = observe_task_review_recovery_evidence(run)
        assert observation["status"] == "known"
        hashes.append(observation["evidenceSha256"])
        assert state["status"] == "unknown" and state["can_dispatch"] is True
    assert len(set(hashes)) == 1
