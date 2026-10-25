"""真实双进程验证终态归档意图与 dispatcher 启动许可的持久互斥。"""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from _rvf_test_support.loader import load_script_module


DISPATCHER_ARCHIVE_SERIALIZATION_NATIVE_WORKER = r'''
import json, os, sys, time
from pathlib import Path
scripts, root, role, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, str(scripts))
import dispatcher_writer_execution_evidence as evidence
import rvf_detached_thread as detached
import review_run_terminal_archive as archive
import rvf_run_finalize as finalizer
configuration = json.loads((root / "native-worker-configuration.json").read_text())
run, repo = Path(configuration["run_dir"]), Path(configuration["repo"])
status = run / "artifacts/reviewers/.dispatch-thread.status.json"
def checkpoint(name):
    (root / (name + ".entered")).write_text(str(os.getpid()))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (root / (name + ".release")).exists(): return
        time.sleep(.02)
    raise RuntimeError("隔离进程竞争未在预算内释放：" + name)
def side_effect(kind):
    with (root / "native-finalize-side-effects.jsonl").open("a") as output:
        output.write(json.dumps({"kind": kind, "pid": os.getpid()}) + "\n")
    return {}
finalizer.capture_run = lambda **kwargs: side_effect("capture")
finalizer._release_tracker_lease = lambda *args, **kwargs: side_effect("release")
finalizer._write_usage_summary = lambda *args: {}
finalizer._scaffold_analysis = lambda *args: {}
if role == "launch":
    if mode == "exit_during_recovery_fence_status":
        original_fence_commit = evidence.atomic_write_json
        def interrupt_after_fence_receipt(path, payload):
            original_fence_commit(path, payload)
            if Path(path).name == "dispatcher-writer-execution-evidence.json" and payload.get("dispatcher_writer_execution_unattempted_recovery_fence"):
                os._exit(79)
        evidence.atomic_write_json = interrupt_after_fence_receipt
    if mode == "pause_after_absent_tmux_probe":
        original_probe = detached.probe_dispatcher_tmux_session_before_new_execution
        def pause_after_absent_probe(*args, **kwargs):
            observation = original_probe(*args, **kwargs)
            if observation["status"] == "absent": checkpoint("absent_tmux_before_external_claim")
            return observation
        detached.probe_dispatcher_tmux_session_before_new_execution = pause_after_absent_probe
    if mode in ("exit_after_preparing_status", "pause_after_preparing_status"):
        original_initialize = evidence.initialize_dispatcher_writer_execution_binding_before_entrypoint_read
        def interrupt_after_preparing_status(*args, **kwargs):
            bound = original_initialize(*args, **kwargs)
            if mode == "exit_after_preparing_status": os._exit(76)
            checkpoint("paired_preparing_before_content")
            return bound
        evidence.initialize_dispatcher_writer_execution_binding_before_entrypoint_read = interrupt_after_preparing_status
    if mode in ("exit_after_receipt_sha", "exit_after_unpointed_receipt"):
        original_commit = evidence.atomic_write_json
        def interrupt_receipt_binding_commit(path, payload):
            original_commit(path, payload)
            if Path(path).name == "dispatcher-writer-execution-evidence.json" and payload.get("dispatcher_writer_execution_launch_boundary_status") == "preparing_before_tmux_attempt":
                if mode == "exit_after_unpointed_receipt": os._exit(78)
                if payload.get("dispatcher_writer_execution_entrypoint_sha256"): os._exit(77)
        evidence.atomic_write_json = interrupt_receipt_binding_commit
    if mode == "evidence_before_status":
        original_write = evidence.atomic_write_json
        paused = False
        def pause_new_execution_receipt(path, payload):
            global paused
            original_write(path, payload)
            if not paused and Path(path).name == "dispatcher-writer-execution-evidence.json" and payload.get("dispatcher_writer_execution_launch_boundary_status") == "preparing_before_tmux_attempt":
                paused = True
                checkpoint("new_execution_receipt_before_status")
        evidence.atomic_write_json = pause_new_execution_receipt
    if mode == "attempted_before_spawn":
        original_spawn = evidence.run_dispatcher_tmux_launcher_with_native_process_creation_evidence
        def pause_after_attempted(*args, **kwargs):
            checkpoint("attempted_before_native_spawn")
            return original_spawn(*args, **kwargs)
        evidence.run_dispatcher_tmux_launcher_with_native_process_creation_evidence = pause_after_attempted
    (root / ("launch_" + mode + ".started")).write_text(str(os.getpid()))
    outcome = detached.launch_detached(session_name=configuration["session_name"], argv=configuration["command"],
        log_path=status.with_name("native-dispatch.log"), status_path=status,
        lock_path=status.with_name(".dispatch-thread.launch.lock"), status_payload=configuration["payload"],
        total_timeout_seconds=20, require_actual_dispatcher_writer_execution_evidence=True)
else:
    if mode in ("after_writer_check", "crash_after_intent"):
        original_writer_check = archive._writers_still_active
        def intercept_writer_check(*args, **kwargs):
            observed = original_writer_check(*args, **kwargs)
            if mode == "crash_after_intent": os._exit(73)
            if not observed: checkpoint("archive_accepted_no_writer")
            return observed
        archive._writers_still_active = intercept_writer_check
    if mode in ("pause_before_finalize", "crash_after_completed_finalize", "forbid_finalize"):
        original_finalize = archive.finalize_run
        def intercept_finalize(**kwargs):
            if mode == "forbid_finalize": raise AssertionError("完成后恢复不能再次调用finalize")
            if mode == "pause_before_finalize": checkpoint("archive_before_finalize")
            result = original_finalize(**kwargs)
            assert result["errors"] == [], result
            if mode == "crash_after_completed_finalize": os._exit(74)
            return result
        archive.finalize_run = intercept_finalize
    (root / ("archive_" + mode + ".started")).write_text(str(os.getpid()))
    if role == "archive":
        outcome = archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted",
            reason="隔离双进程验证终态与启动互斥", event={"session_id": configuration["session_id"], "suppress_review_validate_fix": True})
    else:
        outcome = archive.resume_pending_review_run_terminal_archives(repo=repo, session_id=configuration["session_id"])[0]
(root / (role + "-" + mode + "-result.json")).write_text(json.dumps(outcome, ensure_ascii=False, indent=2))
'''


def write_dispatcher_archive_serialization_fixture_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def wait_for_dispatcher_archive_serialization_condition(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("隔离dispatcher/归档竞争未在预算内达到预期条件")


@pytest.fixture
def native_dispatcher_archive_serialization_fixture(tmp_path, monkeypatch):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("当前主机缺少原生tmux")
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    for name in ("RVF_DISPATCHER_WRITER_EXECUTION_ID", "RVF_DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH"):
        monkeypatch.delenv(name, raising=False)
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    detached = load_script_module("rvf_detached_thread")
    scripts = Path(evidence.__file__).parent
    run, repo = tmp_path / "rvf-review-start-0230230230230230", tmp_path / "isolated-repository"
    repo.mkdir()
    identity = {"run_id": run.name, "run_dir": str(run), "repo": str(repo), "session_id": "rvf023-session"}
    write_dispatcher_archive_serialization_fixture_json(run / "summary.json", identity)
    write_dispatcher_archive_serialization_fixture_json(run / "artifacts/inputs/scope.contract.json", identity)
    script = tmp_path / "restorable-actual-dispatcher.py"
    source = "import json,os,sys,time\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(scripts)) + ")\n"
    source += "from dispatcher_writer_execution_evidence import run_actual_dispatcher_with_writer_execution_evidence\nroot=Path(sys.argv[1])\n"
    source += "def actual_dispatcher_body():\n    with (root/'native-writer-heartbeats.jsonl').open('a') as output:\n"
    source += "        for index in range(400):\n            if (root/'stop-actual-dispatcher').exists(): break\n            output.write(json.dumps({'pid':os.getpid(),'sid':os.getsid(0),'index':index})+'\\n'); output.flush(); time.sleep(.05)\n    return 0\n"
    source += "raise SystemExit(run_actual_dispatcher_with_writer_execution_evidence(actual_dispatcher_body,dispatcher_script_path=Path(__file__).resolve()))\n"
    status = run / "artifacts/reviewers/.dispatch-thread.status.json"
    alias = Path("/tmp") / ("rvf023-socket-" + secrets.token_hex(6))
    alias.symlink_to(tmp_path, target_is_directory=True)
    socket = alias / "private-control.sock"
    wrapper = tmp_path / "isolated-native-tmux"
    wrapper.write_text("#!" + sys.executable + "\nimport os,sys\nos.execv(" + repr(tmux) + ",[" + repr(tmux) + ",'-S'," + repr(str(socket)) + ",*sys.argv[1:]])\n")
    wrapper.chmod(0o700)
    monkeypatch.setenv("RVF_TMUX_BIN", str(wrapper))
    payload = {"schema_version": 1, "kind": "dispatch", **identity, "tmux_session": "rvf023-private-dispatch",
        "command": [sys.executable, str(script), str(tmp_path)], "returncode": None, "finished_at": None, "total_timeout_seconds": 20}
    initial = detached.launch_detached(session_name=payload["tmux_session"], argv=payload["command"], log_path=status.with_name("native-dispatch.log"),
        status_path=status, lock_path=status.with_name(".dispatch-thread.launch.lock"), status_payload=payload,
        total_timeout_seconds=20, require_actual_dispatcher_writer_execution_evidence=True)
    assert initial["launch_status"] == "launch_failed"
    assert not evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
    original_execution_id = json.loads(status.read_text())["dispatcher_writer_execution_id"]
    script.write_text(source)
    configuration = {"run_dir": str(run), "repo": str(repo), "session_id": identity["session_id"],
        "session_name": payload["tmux_session"], "command": payload["command"], "payload": payload}
    write_dispatcher_archive_serialization_fixture_json(tmp_path / "native-worker-configuration.json", configuration)
    worker = tmp_path / "dispatcher-archive-native-worker.py"
    worker.write_text(DISPATCHER_ARCHIVE_SERIALIZATION_NATIVE_WORKER)
    owned_processes, command_receipts = [], []
    def start_native_worker(role, mode):
        argv = [sys.executable, "-B", str(worker), str(scripts), str(tmp_path), role, mode]
        output_path = tmp_path / (role + "-" + mode + "-stdout.log")
        error_path = tmp_path / (role + "-" + mode + "-stderr.log")
        with output_path.open("w") as output, error_path.open("w") as errors:
            process = subprocess.Popen(argv, cwd=repo, stdout=output, stderr=errors)
        receipt = {"argv": argv, "cwd": str(repo), "pid": process.pid, "stdout_path": str(output_path), "stderr_path": str(error_path)}
        owned_processes.append((process, receipt))
        return process
    def finish_native_worker(process, expected=0):
        process.wait(timeout=15)
        receipt = next(receipt for candidate, receipt in owned_processes if candidate is process)
        receipt.update({"exit_code": process.returncode, "stdout": Path(receipt["stdout_path"]).read_text(), "stderr": Path(receipt["stderr_path"]).read_text()})
        if receipt not in command_receipts:
            command_receipts.append(receipt)
        write_dispatcher_archive_serialization_fixture_json(tmp_path / "native-process-command-receipts.json", command_receipts)
        assert process.returncode == expected, receipt
    def result_for(role, mode):
        return json.loads((tmp_path / (role + "-" + mode + "-result.json")).read_text())
    def claim_external_tmux_resource():
        argv = [str(wrapper), "new-session", "-d", "-s", payload["tmux_session"], shlex.join(payload["command"])]
        claimed = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=5)
        command_receipts.append({"argv": argv, "cwd": str(repo), "exit_code": claimed.returncode, "stdout": claimed.stdout, "stderr": claimed.stderr})
        assert claimed.returncode == 0, claimed.stderr
        wait_for_dispatcher_archive_serialization_condition(lambda: (tmp_path / "native-writer-heartbeats.jsonl").exists())
    def release_external_tmux_resource():
        (tmp_path / "stop-actual-dispatcher").write_text("stop\n")
        wait_for_dispatcher_archive_serialization_condition(lambda: not detached._tmux_session_alive(payload["tmux_session"]))
        (tmp_path / "stop-actual-dispatcher").unlink()
        (tmp_path / "native-writer-heartbeats.jsonl").unlink(missing_ok=True)
    fixture = {"root": tmp_path, "run": run, "repo": repo, "status": status, "evidence": evidence,
        "claim_external": claim_external_tmux_resource, "release_external": release_external_tmux_resource, "tmux_wrapper": wrapper,
        "start": start_native_worker, "finish": finish_native_worker, "result": result_for, "original_execution_id": original_execution_id}
    try:
        yield fixture
    finally:
        (tmp_path / "stop-actual-dispatcher").write_text("stop\n")
        for name in ("new_execution_receipt_before_status", "attempted_before_native_spawn", "archive_accepted_no_writer", "archive_before_finalize", "absent_tmux_before_external_claim", "paired_preparing_before_content"):
            (tmp_path / (name + ".release")).write_text("release\n")
        for process, receipt in owned_processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if "exit_code" not in receipt:
                receipt.update({"exit_code": process.returncode, "stdout": Path(receipt["stdout_path"]).read_text(), "stderr": Path(receipt["stderr_path"]).read_text()})
                command_receipts.append(receipt)
        cleanup_argv = [tmux, "-S", str(socket), "kill-server"]
        cleanup = subprocess.run(cleanup_argv, capture_output=True, text=True, timeout=5)
        command_receipts.append({"argv": cleanup_argv, "cwd": str(repo), "exit_code": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr})
        write_dispatcher_archive_serialization_fixture_json(tmp_path / "native-process-command-receipts.json", command_receipts)
        alias.unlink(missing_ok=True)


def finish_writer_and_archive_once(fixture):
    root, evidence, status = fixture["root"], fixture["evidence"], fixture["status"]
    (root / "stop-actual-dispatcher").write_text("stop\n")
    wait_for_dispatcher_archive_serialization_condition(lambda: not evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"])
    operation = "resume" if (fixture["run"] / "artifacts/review-run-terminal-archive-request.json").exists() else "archive"
    resumed = fixture["start"](operation, "normal")
    fixture["finish"](resumed)
    assert fixture["result"](operation, "normal")["status"] == "archived"
    repeated = fixture["start"]("resume", "forbid_finalize")
    fixture["finish"](repeated)
    assert fixture["result"]("resume", "forbid_finalize")["status"] == "archived"
    effects = [json.loads(line)["kind"] for line in (root / "native-finalize-side-effects.jsonl").read_text().splitlines()]
    assert effects == ["capture", "release"]


@pytest.mark.parametrize("interruption_mode,expected_exit", [("exit_after_preparing_status", 76), ("exit_after_receipt_sha", 77), ("pause_after_preparing_status", -9)])
def test_abandoned_preparation_recovers_without_guessing_launcher_death(native_dispatcher_archive_serialization_fixture, interruption_mode, expected_exit):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    interrupted = fixture["start"]("launch", interruption_mode)
    if expected_exit == -9:
        wait_for_dispatcher_archive_serialization_condition(lambda: (root / "paired_preparing_before_content.entered").exists())
        interrupted.kill()
    fixture["finish"](interrupted, expected=expected_exit)
    interrupted_status = json.loads(fixture["status"].read_text())
    interrupted_receipt_path = Path(interrupted_status["dispatcher_writer_execution_evidence_path"])
    interrupted_receipt_bytes = interrupted_receipt_path.read_bytes()
    if interruption_mode == "exit_after_receipt_sha":
        entrypoint = Path(interrupted_status["command"][1])
        entrypoint.write_bytes(entrypoint.read_bytes() + "\n# 中断后live入口内容变化，只用于后续新执行\n".encode())
    recovering = fixture["start"]("launch", "normal")
    fixture["finish"](recovering)
    write_dispatcher_archive_serialization_fixture_json(root / "native-abandoned-preparation-observation.json", {
        "interruption_mode": interruption_mode, "native_exit_code": expected_exit,
        "interrupted_status": interrupted_status, "interrupted_receipt": json.loads(interrupted_receipt_bytes),
        "recovery_result": fixture["result"]("launch", "normal")})
    assert fixture["result"]("launch", "normal")["launch_status"] == "launched"
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "native-writer-heartbeats.jsonl").exists())
    sealed = json.loads(interrupted_receipt_path.read_text())
    assert sealed["dispatcher_writer_execution_launch_boundary_status"] == "failed_before_tmux_attempt"
    assert sealed["dispatcher_writer_execution_entrypoint_sha256"] == json.loads(interrupted_receipt_bytes)["dispatcher_writer_execution_entrypoint_sha256"]
    finish_writer_and_archive_once(fixture)


def test_orphan_preparation_receipts_share_the_same_generation_budget(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    for _ in range(3):
        interrupted = fixture["start"]("launch", "exit_after_unpointed_receipt")
        fixture["finish"](interrupted, expected=78)
    # 原status未指向新执行时，也直接从全部精确receipt恢复预算，不维护第二份索引。
    assert not (fixture["status"].parent / "dispatcher-writer-prelaunch-recovery-input-generation.json").exists()
    recovering = fixture["start"]("launch", "normal")
    fixture["finish"](recovering)
    outcome = fixture["result"]("launch", "normal")
    receipts = [json.loads(path.read_text()) for path in (fixture["status"].parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json")]
    write_dispatcher_archive_serialization_fixture_json(root / "native-orphan-generation-budget-observation.json", {"recovery_result": outcome, "receipts": receipts})
    assert outcome["launch_status"] == "launch_failed"
    assert outcome["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change"
    assert not (root / "native-writer-heartbeats.jsonl").exists()
    generations = {}
    for receipt in receipts:
        generations.setdefault(receipt["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"], []).append(receipt)
    assert sorted(len(group) for group in generations.values()) == [1, 3]
    archive = fixture["start"]("archive", "normal")
    fixture["finish"](archive)
    assert fixture["result"]("archive", "normal")["status"] == "archived"


def test_recovery_fence_half_commit_survives_another_actual_process_exit(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    interrupted = fixture["start"]("launch", "exit_after_receipt_sha")
    fixture["finish"](interrupted, expected=77)
    fenced = fixture["start"]("launch", "exit_during_recovery_fence_status")
    fixture["finish"](fenced, expected=79)
    recovered = fixture["start"]("launch", "normal")
    fixture["finish"](recovered)
    assert fixture["result"]("launch", "normal")["launch_status"] == "launched"
    finish_writer_and_archive_once(fixture)


def test_prior_actual_boot_exit_remains_valid_after_another_execution_is_prepared(native_dispatcher_archive_serialization_fixture, monkeypatch):
    fixture = native_dispatcher_archive_serialization_fixture
    evidence = fixture["evidence"]
    detached = load_script_module("rvf_detached_thread")
    configuration = json.loads((fixture["root"] / "native-worker-configuration.json").read_text())
    binding = evidence.prepare_dispatcher_writer_execution_evidence_binding(fixture["status"], configuration["payload"])
    binding = evidence.record_dispatcher_writer_execution_tmux_attempt_boundary(fixture["status"], binding["dispatcher_writer_execution_id"])
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE, binding["dispatcher_writer_execution_id"])
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE, binding["dispatcher_writer_execution_evidence_path"])
    monkeypatch.setattr(sys, "argv", configuration["command"][1:])
    monkeypatch.setattr(evidence, "current_local_host_boot_identity_for_dispatcher_writer_execution", lambda: {"host_identity_sha256": "same-fixture-host", "boot_session_id": "prior-fixture-boot"})
    def interrupted_actual_runtime():
        raise KeyboardInterrupt("真实adapter出生登记后中断；boot换代来源使用受控OS身份fixture")
    with pytest.raises(KeyboardInterrupt):
        evidence.run_actual_dispatcher_with_writer_execution_evidence(interrupted_actual_runtime, dispatcher_script_path=Path(configuration["command"][1]))
    monkeypatch.setattr(evidence, "current_local_host_boot_identity_for_dispatcher_writer_execution", lambda: {"host_identity_sha256": "same-fixture-host", "boot_session_id": "next-fixture-boot"})
    def bounded_preparation_failure(**kwargs):
        raise ValueError("有界fixture准备失败")
    monkeypatch.setattr(detached, "_build_wrapper_shell", bounded_preparation_failure)
    for _ in range(2):
        outcome = detached.launch_detached(session_name=configuration["session_name"], argv=configuration["command"],
            log_path=fixture["status"].with_name("native-dispatch.log"), status_path=fixture["status"],
            lock_path=fixture["status"].with_name(".dispatch-thread.launch.lock"), status_payload=configuration["payload"],
            total_timeout_seconds=20, require_actual_dispatcher_writer_execution_evidence=True)
        assert outcome["launch_status"] == "launch_failed" and "有界fixture准备失败" in outcome["error"]


@pytest.mark.parametrize("remove_launch_lock", [False, True])
def test_existing_tmux_resource_preserves_old_negative_proof_and_recovers_after_release(native_dispatcher_archive_serialization_fixture, remove_launch_lock):
    fixture = native_dispatcher_archive_serialization_fixture
    status = fixture["status"]
    original_status = status.read_bytes()
    receipt_path = Path(json.loads(original_status)["dispatcher_writer_execution_evidence_path"])
    original_receipt = receipt_path.read_bytes()
    original_receipt_count = len(list((status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json")))
    fixture["claim_external"]()
    if remove_launch_lock:
        status.with_name(".dispatch-thread.launch.lock").unlink()
    blocked = fixture["start"]("launch", "normal")
    fixture["finish"](blocked)
    blocked_outcome = fixture["result"]("launch", "normal")
    assert blocked_outcome["recovery_state"] == "waiting_for_tmux_session_release"
    assert status.read_bytes() == original_status
    assert receipt_path.read_bytes() == original_receipt
    assert len(list((status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json"))) == original_receipt_count
    fixture["release_external"]()
    recovered = fixture["start"]("launch", "normal")
    fixture["finish"](recovered)
    assert fixture["result"]("launch", "normal")["launch_status"] == "launched"
    write_dispatcher_archive_serialization_fixture_json(fixture["root"] / "native-tmux-preflight-release-observation.json", {
        "launch_lock_removed": remove_launch_lock, "blocked_outcome": blocked_outcome,
        "old_status_and_receipt_preserved": True, "recovered_outcome": fixture["result"]("launch", "normal")})
    finish_writer_and_archive_once(fixture)


def test_external_claim_after_tmux_probe_keeps_new_attempt_unknown(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    launched = fixture["start"]("launch", "pause_after_absent_tmux_probe")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "absent_tmux_before_external_claim.entered").exists())
    fixture["claim_external"]()
    (root / "absent_tmux_before_external_claim.release").write_text("release\n")
    fixture["finish"](launched)
    outcome = fixture["result"]("launch", "pause_after_absent_tmux_probe")
    current_status = json.loads(fixture["status"].read_text())
    observed = fixture["evidence"].observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(fixture["status"])
    assert outcome["launch_status"] == "already_running" and outcome["tmux_command"]
    assert current_status["dispatcher_writer_execution_id"] != fixture["original_execution_id"]
    assert current_status["dispatcher_writer_execution_launch_boundary_status"] == "tmux_launch_attempted"
    assert observed["dispatcher_writers_may_still_write"]
    archive = fixture["start"]("archive", "normal")
    fixture["finish"](archive)
    assert fixture["result"]("archive", "normal")["status"] == "waiting_for_evidence"
    assert not (root / "native-finalize-side-effects.jsonl").exists()
    write_dispatcher_archive_serialization_fixture_json(root / "native-post-probe-external-claim-observation.json", {
        "outcome": outcome, "new_attempted_execution_id": current_status["dispatcher_writer_execution_id"], "writer_observation": observed})


def test_tmux_query_failure_waits_without_new_receipt_then_recovers(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    wrapper = fixture["tmux_wrapper"]
    original_wrapper = wrapper.read_bytes()
    original_status = fixture["status"].read_bytes()
    wrapper.write_text("#!" + sys.executable + "\nimport sys\nprint('tmux server query denied',file=sys.stderr)\nraise SystemExit(2)\n")
    blocked = fixture["start"]("launch", "normal")
    fixture["finish"](blocked)
    blocked_outcome = fixture["result"]("launch", "normal")
    assert blocked_outcome["recovery_state"] == "waiting_for_tmux_query_recovery"
    assert fixture["status"].read_bytes() == original_status
    wrapper.write_bytes(original_wrapper)
    recovered = fixture["start"]("launch", "normal")
    fixture["finish"](recovered)
    assert fixture["result"]("launch", "normal")["launch_status"] == "launched"
    write_dispatcher_archive_serialization_fixture_json(fixture["root"] / "native-tmux-query-recovery-observation.json", {"blocked_outcome": blocked_outcome,
        "recovered_outcome": fixture["result"]("launch", "normal")})
    finish_writer_and_archive_once(fixture)


def test_new_execution_receipt_before_status_is_serialized_with_archive(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    launcher = fixture["start"]("launch", "evidence_before_status")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "new_execution_receipt_before_status.entered").exists())
    status_while_paused = json.loads(fixture["status"].read_text())
    receipts_while_paused = len(list((fixture["status"].parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json")))
    archive = fixture["start"]("archive", "normal")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "archive_normal.started").exists())
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and archive.poll() is None:
        time.sleep(.02)
    archive_finished_before_launch_release = archive.poll() is not None
    captured_before_launch_release = (root / "native-finalize-side-effects.jsonl").exists()
    (root / "new_execution_receipt_before_status.release").write_text("release\n")
    fixture["finish"](launcher)
    fixture["finish"](archive)
    observation = {"status_execution_id_while_new_receipt_prepared": status_while_paused["dispatcher_writer_execution_id"],
        "execution_receipt_count_while_paused": receipts_while_paused,
        "archive_finished_before_launch_release": archive_finished_before_launch_release,
        "captured_before_launch_release": captured_before_launch_release,
        "launch_result": fixture["result"]("launch", "evidence_before_status"), "archive_result": fixture["result"]("archive", "normal")}
    write_dispatcher_archive_serialization_fixture_json(root / "native-evidence-before-status-race-observation.json", observation)
    assert not archive_finished_before_launch_release
    assert not captured_before_launch_release
    assert fixture["result"]("archive", "normal")["status"] == "waiting_for_evidence"
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "native-writer-heartbeats.jsonl").exists())
    finish_writer_and_archive_once(fixture)


def test_archive_check_then_new_attempt_uses_same_durable_admission(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    archive = fixture["start"]("archive", "after_writer_check")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "archive_accepted_no_writer.entered").exists())
    launcher = fixture["start"]("launch", "normal")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "launch_normal.started").exists())
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and launcher.poll() is None:
        time.sleep(.02)
    launch_finished_before_archive_release = launcher.poll() is not None
    writer_started_before_archive_release = (root / "native-writer-heartbeats.jsonl").exists()
    (root / "archive_accepted_no_writer.release").write_text("release\n")
    fixture["finish"](archive)
    fixture["finish"](launcher)
    write_dispatcher_archive_serialization_fixture_json(root / "native-check-before-attempt-race-observation.json", {
        "launch_finished_before_archive_release": launch_finished_before_archive_release,
        "writer_started_before_archive_release": writer_started_before_archive_release,
        "archive_result": fixture["result"]("archive", "after_writer_check"), "launch_result": fixture["result"]("launch", "normal")})
    assert not launch_finished_before_archive_release
    assert not writer_started_before_archive_release
    assert fixture["result"]("archive", "after_writer_check")["status"] == "archived"
    assert fixture["result"]("launch", "normal")["launch_status"] == "launch_failed"
    assert "终态归档" in fixture["result"]("launch", "normal")["error"]


def test_already_attempted_launch_makes_archive_wait_for_actual_delivery(native_dispatcher_archive_serialization_fixture):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    launcher = fixture["start"]("launch", "attempted_before_spawn")
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "attempted_before_native_spawn.entered").exists())
    archive = fixture["start"]("archive", "normal")
    fixture["finish"](archive)
    assert fixture["result"]("archive", "normal")["status"] == "waiting_for_evidence"
    assert not (root / "native-finalize-side-effects.jsonl").exists()
    (root / "attempted_before_native_spawn.release").write_text("release\n")
    fixture["finish"](launcher)
    wait_for_dispatcher_archive_serialization_condition(lambda: (root / "native-writer-heartbeats.jsonl").exists())
    finish_writer_and_archive_once(fixture)


@pytest.mark.parametrize("crash_mode,exit_code", [("crash_after_intent", 73), ("crash_after_completed_finalize", 74)])
def test_terminal_archive_intent_survives_process_exit_without_duplicate_finalize(native_dispatcher_archive_serialization_fixture, crash_mode, exit_code):
    fixture = native_dispatcher_archive_serialization_fixture
    root = fixture["root"]
    crashed = fixture["start"]("archive", crash_mode)
    fixture["finish"](crashed, expected=exit_code)
    launcher = fixture["start"]("launch", "normal")
    fixture["finish"](launcher)
    assert fixture["result"]("launch", "normal")["launch_status"] == "launch_failed"
    assert not (root / "native-writer-heartbeats.jsonl").exists()
    assert json.loads(fixture["status"].read_text())["dispatcher_writer_execution_id"] == fixture["original_execution_id"]
    mode = "forbid_finalize" if crash_mode == "crash_after_completed_finalize" else "normal"
    resumed = fixture["start"]("resume", mode)
    fixture["finish"](resumed)
    assert fixture["result"]("resume", mode)["status"] == "archived"
    repeated = fixture["start"]("resume", "forbid_finalize")
    fixture["finish"](repeated)
    effects = [json.loads(line)["kind"] for line in (root / "native-finalize-side-effects.jsonl").read_text().splitlines()]
    assert effects == ["capture", "release"]
