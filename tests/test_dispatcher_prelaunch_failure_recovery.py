"""dispatcher 确定未尝试启动的持久负证明与有界恢复。"""

from __future__ import annotations

import json
import errno
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from _rvf_test_support.loader import load_script_module


def write_dispatcher_prelaunch_fixture_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch):
    monkeypatch.setattr(detached, "probe_dispatcher_tmux_session_before_new_execution", lambda *args: {"status": "absent"})


@pytest.fixture
def dispatcher_prelaunch_recovery_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    for name in ("RVF_DISPATCHER_WRITER_EXECUTION_ID", "RVF_DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH"):
        monkeypatch.delenv(name, raising=False)
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    detached = load_script_module("rvf_detached_thread")
    run = tmp_path / "rvf-review-start-0210210210210210"
    repo = tmp_path / "isolated-repository"
    repo.mkdir()
    identity = {"run_id": run.name, "run_dir": str(run), "repo": str(repo), "session_id": "rvf021-session"}
    write_dispatcher_prelaunch_fixture_json(run / "summary.json", identity)
    write_dispatcher_prelaunch_fixture_json(run / "artifacts/inputs/scope.contract.json", identity)
    status = run / "artifacts/reviewers/.dispatch-thread.status.json"
    script = tmp_path / "restorable-actual-dispatcher-runtime.py"
    source = (
        "import sys,time\nfrom pathlib import Path\n"
        "sys.path.insert(0," + repr(str(Path(evidence.__file__).parent)) + ")\n"
        "from dispatcher_writer_execution_evidence import run_actual_dispatcher_with_writer_execution_evidence\n"
        "root=Path(sys.argv[1])\n"
        "def actual_dispatcher_body():\n"
        "    with (root/'actual-writer-heartbeats.jsonl').open('a') as output:\n"
        "        for index in range(160):\n"
        "            if (root/'stop-actual-dispatcher').exists(): break\n"
        "            output.write(str(index)+'\\n'); output.flush(); time.sleep(.05)\n"
        "    return 0\n"
        "raise SystemExit(run_actual_dispatcher_with_writer_execution_evidence(actual_dispatcher_body,dispatcher_script_path=Path(__file__).resolve()))\n"
    )
    payload = {"schema_version": 1, "kind": "dispatch", **identity, "tmux_session": "rvf021-private-dispatch",
        "command": [sys.executable, str(script), str(tmp_path)], "returncode": None, "finished_at": None, "total_timeout_seconds": 15}
    parameters = {"session_name": payload["tmux_session"], "argv": payload["command"], "log_path": status.with_name("dispatcher.log"),
        "status_path": status, "lock_path": status.with_name(".dispatch-thread.launch.lock"), "status_payload": payload,
        "total_timeout_seconds": 15, "require_actual_dispatcher_writer_execution_evidence": True}
    return evidence, detached, run, status, script, source, parameters


@pytest.mark.parametrize("native_prelaunch_failure", ["missing_entrypoint", "missing_tmux", "tmux_execution_denied", "missing_selected_python", "selected_python_execution_denied", "missing_entrypoint_without_session"])
def test_native_prelaunch_failure_recovers_on_input_restoration(dispatcher_prelaunch_recovery_fixture, monkeypatch, native_prelaunch_failure):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("当前主机缺少原生 tmux；其余启动边界负例仍可执行")
    fixture_root = script.parent
    short_socket_alias = Path("/tmp") / ("rvf021-socket-" + secrets.token_hex(6))
    short_socket_alias.symlink_to(fixture_root, target_is_directory=True)
    socket = short_socket_alias / "native-control.sock"
    tmux_calls = fixture_root / "native-tmux-calls.jsonl"
    wrapper = fixture_root / "isolated-native-tmux"
    entrypoint_missing = native_prelaunch_failure.startswith("missing_entrypoint")
    if native_prelaunch_failure == "missing_entrypoint_without_session":
        parameters["status_payload"].pop("session_id")
    wrapper_source = "#!/bin/sh\n" + "printf '%s\\n' \"$*\" >> " + shlex.quote(str(tmux_calls)) + "\nexec " + shlex.quote(tmux) + " -S " + shlex.quote(str(socket)) + " \"$@\"\n"
    if native_prelaunch_failure != "missing_tmux":
        wrapper.write_text(wrapper_source)
        wrapper.chmod(0o600 if native_prelaunch_failure == "tmux_execution_denied" else 0o700)
    if not entrypoint_missing:
        script.write_text(source)
    selected_python_spawn_failure = native_prelaunch_failure in ("missing_selected_python", "selected_python_execution_denied")
    selected_python = fixture_root / "restorable-selected-python"
    selected_python_source = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " \"$@\"\n"
    if selected_python_spawn_failure:
        parameters["argv"][0] = str(selected_python)
        if native_prelaunch_failure == "selected_python_execution_denied":
            selected_python.write_text(selected_python_source)
            selected_python.chmod(0o600)
    monkeypatch.setenv("RVF_TMUX_BIN", str(wrapper))
    observed = {}
    def wait_for_monitor_creation_failure_and_shell_exit():
        monitor_failure_deadline = time.monotonic() + 8
        while time.monotonic() < monitor_failure_deadline:
            observation = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
            if observation["evidence_kind"] == "dispatcher_monitor_child_process_creation_failed" and not detached._tmux_session_alive(parameters["session_name"]):
                return
            time.sleep(.05)
        pytest.fail("原生monitor创建失败或外壳退出证据未在预算内可读")
    def launch_after_bounded_native_resource_recheck():
        deadline = time.monotonic() + 8
        attempts = []
        while time.monotonic() < deadline:
            outcome = detached.launch_detached(**parameters)
            attempts.append(outcome)
            if outcome["launch_status"] != "already_running" or not str(outcome.get("recovery_state", "")).startswith("waiting_for_"):
                observed.setdefault("bounded_resource_rechecks", []).extend(attempts)
                return outcome
            time.sleep(.05)
        pytest.fail("原生tmux资源未在预算内恢复：" + repr(attempts))
    try:
        first = detached.launch_detached(**parameters)
        if selected_python_spawn_failure:
            wait_for_monitor_creation_failure_and_shell_exit()
        first_status = json.loads(status.read_text())
        observed.update({"first_launch": first, "first_status": first_status,
            "first_attempt_native_tmux_calls": tmux_calls.read_text() if tmux_calls.exists() else "",
            "first_writer_observation": evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)})
        assert first["launch_status"] == ("launched" if selected_python_spawn_failure else "launch_failed")
        assert ("PermissionError" if native_prelaunch_failure.endswith("execution_denied") else "FileNotFoundError") in first_status["error"]
        assert first_status["session_id"] == "rvf021-session"
        requested_native_start = tmux_calls.exists() and any(line.startswith("new-session ") for line in tmux_calls.read_text().splitlines())
        assert requested_native_start is selected_python_spawn_failure
        assert observed["first_writer_observation"]["evidence_kind"] == ("dispatcher_monitor_child_process_creation_failed" if selected_python_spawn_failure
            else "dispatcher_launch_not_attempted" if entrypoint_missing else "dispatcher_tmux_process_creation_failed")
        assert not load_script_module("review_run_terminal_archive")._writers_still_active(status.parents[2])
        if not entrypoint_missing:
            for _ in range(2):
                assert launch_after_bounded_native_resource_recheck()["launch_status"] == ("launched" if selected_python_spawn_failure else "launch_failed")
                if selected_python_spawn_failure:
                    wait_for_monitor_creation_failure_and_shell_exit()
            assert detached.launch_detached(**parameters)["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change"
        script.write_text(source)
        wrapper.write_text(wrapper_source)
        wrapper.chmod(0o700)
        if selected_python_spawn_failure:
            selected_python.write_text(selected_python_source)
            selected_python.chmod(0o700)
        second = launch_after_bounded_native_resource_recheck()
        observed["second_launch"] = second
        observed["second_status"] = json.loads(status.read_text())
        write_dispatcher_prelaunch_fixture_json(fixture_root / "native-prelaunch-recovery-observations.json", observed)
        assert second["launch_status"] == "launched"
        deadline = time.monotonic() + 8
        heartbeats = fixture_root / "actual-writer-heartbeats.jsonl"
        while time.monotonic() < deadline and (not heartbeats.exists() or len(heartbeats.read_text().splitlines()) < 2):
            time.sleep(.05)
        assert heartbeats.exists() and len(heartbeats.read_text().splitlines()) >= 2
        assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
        live_dispatcher_binding = json.loads(status.read_text())
        parameters["lock_path"].unlink()
        repeat_while_actual_writer_alive = detached.launch_detached(**parameters)
        assert repeat_while_actual_writer_alive["launch_status"] == "already_running"
        assert json.loads(status.read_text())["dispatcher_writer_execution_id"] == live_dispatcher_binding["dispatcher_writer_execution_id"]
        observed["lock_loss_while_actual_writer_alive"] = {"repeat_launch": repeat_while_actual_writer_alive,
            "preserved_dispatcher_writer_execution_id": live_dispatcher_binding["dispatcher_writer_execution_id"]}
        (fixture_root / "stop-actual-dispatcher").write_text("stop\n")
        while time.monotonic() < deadline and evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]:
            time.sleep(.05)
        final_observation = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
        assert final_observation["evidence_kind"] == "actual_dispatcher_delivery"
        observed["actual_delivery_after_restoration"] = final_observation
        observed["native_tmux_calls"] = tmux_calls.read_text()
        write_dispatcher_prelaunch_fixture_json(fixture_root / "native-prelaunch-recovery-observations.json", observed)
    finally:
        (fixture_root / "stop-actual-dispatcher").write_text("stop\n")
        subprocess.run([tmux, "-S", str(socket), "kill-server"], capture_output=True, timeout=5)
        short_socket_alias.unlink(missing_ok=True)


def test_proven_prelaunch_failure_does_not_block_archive_writer_gate(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, run, status, _, _, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("入口不存在时不应调用 tmux"))
    result = detached.launch_detached(**parameters)
    assert result["launch_status"] == "launch_failed"
    observation = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
    write_dispatcher_prelaunch_fixture_json(status.parent / "prelaunch-archive-observation.json", observation)
    archive = load_script_module("review_run_terminal_archive")
    assert not archive._writers_still_active(run)
    assert observation["evidence_kind"] == "dispatcher_launch_not_attempted"


def test_explicit_session_conflict_cannot_replace_summary_bound_prelaunch_failure(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    parameters["status_payload"].pop("session_id")
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("尚未允许启动tmux"))
    assert detached.launch_detached(**parameters)["launch_status"] == "launch_failed"
    original_status_bytes = status.read_bytes()
    assert json.loads(original_status_bytes)["session_id"] == "rvf021-session"
    script.write_text(source)
    parameters["status_payload"]["session_id"] = "explicit-foreign-session"
    conflict = detached.launch_detached(**parameters)
    assert conflict["launch_status"] == "launch_failed"
    assert "归属" in conflict["error"]
    assert status.read_bytes() == original_status_bytes
    assert not evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]


def test_prelaunch_recovery_budget_is_bounded_per_observed_input_generation(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("缺入口的准备重试不应调用 tmux"))
    initial_failures = [detached.launch_detached(**parameters) for _ in range(5)]
    assert all(result["launch_status"] == "launch_failed" for result in initial_failures)
    assert all(result["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change" for result in initial_failures[3:])
    records = sorted((status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json"))
    assert len(records) == 3
    failures = [json.loads(path.read_text()) for path in records]
    assert sorted(item["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] for item in failures) == [1, 2, 3]
    assert len({item["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] for item in failures}) == 1
    assert all(item["dispatcher_writer_execution_prelaunch_failure_error"] for item in failures)
    old_generation = failures[0]["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"]
    script.write_text(source)
    monkeypatch.setattr(evidence, "run_dispatcher_tmux_launcher_with_native_process_creation_evidence",
        lambda status_path, execution_id, argv, environment, timeout: subprocess.CompletedProcess(argv, 1, "", "tmux 调用后的真实未知边界"))
    restored_attempt = detached.launch_detached(**parameters)
    assert restored_attempt["launch_status"] == "launch_failed"
    latest = json.loads(status.read_text())
    assert latest["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] == 1
    assert latest["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != old_generation
    assert latest["dispatcher_writer_execution_previous_prelaunch_failure_evidence_path"] in {str(path) for path in records}
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]


def test_concurrent_prelaunch_recoveries_share_the_input_generation_budget(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    _, detached, _, status, _, _, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("缺入口时不应派发"))
    with ThreadPoolExecutor(max_workers=5) as concurrent_launch_callers:
        outcomes = list(concurrent_launch_callers.map(lambda unused: detached.launch_detached(**parameters), range(8)))
    assert all(outcome["launch_status"] in {"launch_failed", "already_running"} for outcome in outcomes)
    # 并发者可能恰逢首个准备分支尚未封存失败；此时 unknown 不可重夺。
    for _ in range(3):
        detached.launch_detached(**parameters)
    records = [json.loads(path.read_text()) for path in (status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json")]
    assert len(records) == 3
    assert sorted(record["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] for record in records) == [1, 2, 3]
    write_dispatcher_prelaunch_fixture_json(status.parent / "concurrent-prelaunch-recovery-observations.json", {
        "concurrent_outcomes": outcomes, "execution_ids": [record["dispatcher_writer_execution_id"] for record in records],
        "attempt_numbers": sorted(record["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] for record in records)})


@pytest.mark.parametrize("tmux_outcome", ["duplicate_session", "nonzero", "timeout", "spawn_error"])
def test_attempted_tmux_failure_never_becomes_proof_of_no_writer(dispatcher_prelaunch_recovery_fixture, monkeypatch, tmux_outcome):
    evidence, detached, run, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    script.write_text(source)
    attempted_tmux_call_count = 0
    def attempted_tmux_boundary(status_path, execution_id, argv, environment, timeout):
        nonlocal attempted_tmux_call_count
        attempted_tmux_call_count += 1
        bound = json.loads(status.read_text())
        assert bound["dispatcher_writer_execution_tmux_attempted_at"]
        assert bound["dispatcher_writer_execution_launch_boundary_status"] == "tmux_launch_attempted"
        if tmux_outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, .01)
        if tmux_outcome == "spawn_error":
            raise FileNotFoundError("tmux executable 不可用，但已跨尝试边界")
        return subprocess.CompletedProcess(argv, 1, "", "duplicate session" if tmux_outcome == "duplicate_session" else "tmux 失败")
    monkeypatch.setattr(evidence, "run_dispatcher_tmux_launcher_with_native_process_creation_evidence", attempted_tmux_boundary)
    first = detached.launch_detached(**parameters)
    first_status = json.loads(status.read_text())
    assert first["launch_status"] == ("already_running" if tmux_outcome == "duplicate_session" else "launch_failed")
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
    archive = load_script_module("review_run_terminal_archive")
    assert archive._writers_still_active(run)
    second = detached.launch_detached(**parameters)
    assert second["launch_status"] == "already_running"
    assert json.loads(status.read_text())["dispatcher_writer_execution_id"] == first_status["dispatcher_writer_execution_id"]
    assert attempted_tmux_call_count == 1


@pytest.mark.parametrize("contradictory_observation_field", ["dispatcher_writer_execution_tmux_attempted_at", "dispatcher_writer_execution_monitor_pid", "actual_dispatcher_runtime_pid"])
def test_conflicting_attempt_or_runtime_fact_disqualifies_prelaunch_negative_proof(dispatcher_prelaunch_recovery_fixture, monkeypatch, contradictory_observation_field):
    evidence, detached, _, status, _, _, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("缺入口时不应调用 tmux"))
    detached.launch_detached(**parameters)
    bound = json.loads(status.read_text())
    receipt_path = Path(bound["dispatcher_writer_execution_evidence_path"])
    contradictory_receipt = json.loads(receipt_path.read_text())
    contradictory_receipt[contradictory_observation_field] = 12345
    write_dispatcher_prelaunch_fixture_json(receipt_path, contradictory_receipt)
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
    with pytest.raises(ValueError):
        evidence.record_dispatcher_writer_execution_failure_before_tmux_attempt(status, bound["dispatcher_writer_execution_id"], "不能覆盖已有启动事实")


def test_sealed_prelaunch_execution_cannot_later_start_runtime_or_monitor(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    script.write_text(source)
    def preparation_failure_before_tmux(**kwargs):
        raise ValueError("构造 shell 时尚未启动，已退出准备分支")
    monkeypatch.setattr(detached, "_build_wrapper_shell", preparation_failure_before_tmux)
    assert detached.launch_detached(**parameters)["launch_status"] == "launch_failed"
    bound = json.loads(status.read_text())
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE, bound["dispatcher_writer_execution_id"])
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE, bound["dispatcher_writer_execution_evidence_path"])
    monkeypatch.setattr(sys, "argv", [str(script), str(script.parent)])
    with pytest.raises(ValueError, match="未启动"):
        evidence.run_actual_dispatcher_with_writer_execution_evidence(lambda: pytest.fail("不能事后运行已封存执行"), dispatcher_script_path=script)
    with pytest.raises(ValueError, match="未启动"):
        evidence.monitor_bound_dispatcher_writer_execution_until_delivery(status, bound["dispatcher_writer_execution_id"], 10, bound["command"])


def test_native_monitor_child_exit_without_actual_runtime_registration_stays_unknown(dispatcher_prelaunch_recovery_fixture):
    evidence, _, run, status, script, _, parameters = dispatcher_prelaunch_recovery_fixture
    script.write_text("import time\ntime.sleep(.02)\n")
    bound = evidence.prepare_dispatcher_writer_execution_evidence_binding(status, parameters["status_payload"])
    evidence.record_dispatcher_writer_execution_tmux_attempt_boundary(status, bound["dispatcher_writer_execution_id"])
    native_monitor_argv = evidence.build_dispatcher_writer_execution_monitor_argv(status, bound, .25)
    native_monitor = subprocess.run(native_monitor_argv, capture_output=True, text=True, timeout=5)
    assert native_monitor.returncode == 124
    receipt = json.loads(Path(bound["dispatcher_writer_execution_evidence_path"]).read_text())
    assert receipt["dispatcher_writer_execution_monitor_child_returncode"] == 0
    assert receipt["dispatcher_writer_execution_monitor_started_at"]
    assert "actual_dispatcher_runtime_pid" not in receipt
    observation = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
    assert observation["dispatcher_writers_may_still_write"]
    assert load_script_module("review_run_terminal_archive")._writers_still_active(run)
    write_dispatcher_prelaunch_fixture_json(script.parent / "native-monitor-without-runtime-observations.json", {
        "monitor_argv": native_monitor_argv, "returncode": native_monitor.returncode,
        "stdout": native_monitor.stdout, "stderr": native_monitor.stderr, "bound_execution": receipt, "writer_gate": observation})


def test_post_creation_io_error_cannot_become_native_exec_failure_proof(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    script.write_text(source)
    class CreatedTmuxProcessWithLaterIoFailure:
        pid = 23456
        returncode = None
        def __enter__(self):
            return self
        def __exit__(self, *exception):
            return False
        def communicate(self, **kwargs):
            raise FileNotFoundError(errno.ENOENT, "进程创建后的普通I/O异常", parameters["status_payload"].get("tmux_executable", "tmux"))
    monkeypatch.setattr(evidence.subprocess, "Popen", lambda *args, **kwargs: CreatedTmuxProcessWithLaterIoFailure())
    assert detached.launch_detached(**parameters)["launch_status"] == "launch_failed"
    bound = json.loads(status.read_text())
    receipt = json.loads(Path(bound["dispatcher_writer_execution_evidence_path"]).read_text())
    assert receipt["dispatcher_writer_execution_tmux_launcher_process_started_at"]
    assert "dispatcher_writer_execution_native_tmux_process_creation_failure" not in receipt
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]


def test_exhausted_prelaunch_budget_ignores_touch_and_identical_rewrite_but_tracks_meaningful_inputs(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, _, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    script.write_text(source)
    observed_source_directory = script.parent / "observed-recovery-implementation-sources"
    observed_source_directory.mkdir()
    actual_source_directory = Path(evidence.__file__).parent
    for source_name in ("dispatcher_writer_execution_evidence.py", "rvf_detached_thread.py"):
        (observed_source_directory / source_name).write_bytes((actual_source_directory / source_name).read_bytes())
    monkeypatch.setattr(evidence, "__file__", str(observed_source_directory / "dispatcher_writer_execution_evidence.py"))
    tmux_executable = script.parent / "permission-denied-tmux-executable"
    tmux_executable_source = '#!/bin/sh\nif [ "$1" = "has-session" ]; then echo "no server running" >&2; exit 1; fi\nexit 0\n'
    tmux_executable.write_text(tmux_executable_source)
    tmux_executable.chmod(0o600)
    monkeypatch.setenv("RVF_TMUX_BIN", str(tmux_executable))
    def exhaust_current_dispatcher_prelaunch_input_generation():
        for _ in range(3):
            assert detached.launch_detached(**parameters)["launch_status"] == "launch_failed"
        assert detached.launch_detached(**parameters)["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change"
        return json.loads(status.read_text())
    exhausted = exhaust_current_dispatcher_prelaunch_input_generation()
    original_execution_id = exhausted["dispatcher_writer_execution_id"]
    for unchanged_content_path in (tmux_executable, script, observed_source_directory / "dispatcher_writer_execution_evidence.py",
                                   observed_source_directory / "rvf_detached_thread.py"):
        unchanged_content_path.touch()
        assert detached.launch_detached(**parameters)["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change"
        same_content = unchanged_content_path.read_bytes()
        unchanged_content_path.write_bytes(same_content)
        assert detached.launch_detached(**parameters)["recovery_state"] == "waiting_for_dispatcher_prelaunch_input_change"
        assert json.loads(status.read_text())["dispatcher_writer_execution_id"] == original_execution_id
    meaningful_generations = []
    tmux_executable.write_text(tmux_executable_source + "# tmux实际内容变化\n")
    changed_content = exhaust_current_dispatcher_prelaunch_input_generation()
    assert changed_content["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != exhausted["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"]
    meaningful_generations.append(changed_content["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"])
    tmux_executable.chmod(0o000)
    unreadable = exhaust_current_dispatcher_prelaunch_input_generation()
    assert unreadable["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != meaningful_generations[-1]
    meaningful_generations.append(unreadable["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"])
    tmux_executable.chmod(0o600)
    readable_again = exhaust_current_dispatcher_prelaunch_input_generation()
    assert readable_again["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != meaningful_generations[-1]
    meaningful_generations.append(readable_again["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"])
    helper_source = observed_source_directory / "dispatcher_writer_execution_evidence.py"
    helper_source.write_bytes(helper_source.read_bytes() + "\n# 恢复实现内容发生有意义变化\n".encode())
    changed_recovery_version = exhaust_current_dispatcher_prelaunch_input_generation()
    assert changed_recovery_version["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != meaningful_generations[-1]
    meaningful_generations.append(changed_recovery_version["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"])
    tmux_executable.chmod(0o700)
    assert detached.launch_detached(**parameters)["launch_status"] == "launched"
    executable_again = json.loads(status.read_text())
    assert executable_again["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] == 1
    assert executable_again["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] != meaningful_generations[-1]
    write_dispatcher_prelaunch_fixture_json(script.parent / "meaningful-input-generation-observations.json", {
        "exhausted_execution_id_before_touch_and_same_content_rewrite": original_execution_id,
        "meaningfully_changed_generation_sha256": meaningful_generations,
        "executable_restored_generation_sha256": executable_again["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"],
        "execution_receipt_count": len(list((status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json")))})


@pytest.mark.parametrize("invalid_binding", ["run_id", "dispatcher_writer_execution_id", "dispatcher_writer_execution_command_sha256", "receipt_symlink", "status_symlink"])
def test_prelaunch_negative_proof_retains_identity_and_symlink_gates(dispatcher_prelaunch_recovery_fixture, monkeypatch, invalid_binding):
    evidence, detached, run, status, _, _, parameters = dispatcher_prelaunch_recovery_fixture
    assume_absent_tmux_resource_for_preparation_only_fixture(detached, monkeypatch)
    monkeypatch.setattr(detached.subprocess, "run", lambda *args, **kwargs: pytest.fail("缺入口时不应调用 tmux"))
    detached.launch_detached(**parameters)
    receipt_path = Path(json.loads(status.read_text())["dispatcher_writer_execution_evidence_path"])
    if invalid_binding.endswith("symlink"):
        changed_path = receipt_path if invalid_binding == "receipt_symlink" else status
        renamed_path = changed_path.with_name("foreign-prelaunch-evidence.json")
        changed_path.rename(renamed_path)
        changed_path.symlink_to(renamed_path)
    else:
        changed_receipt = json.loads(receipt_path.read_text())
        changed_receipt[invalid_binding] = "foreign"
        write_dispatcher_prelaunch_fixture_json(receipt_path, changed_receipt)
    original_status_bytes = status.read_bytes()
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
    expected_rejection = "launch_failed" if invalid_binding == "status_symlink" else "already_running"
    assert detached.launch_detached(**parameters)["launch_status"] == expected_rejection
    assert status.read_bytes() == original_status_bytes
    assert load_script_module("review_run_terminal_archive")._writers_still_active(run)


def test_native_preexisting_session_waits_without_creating_execution(dispatcher_prelaunch_recovery_fixture, monkeypatch):
    evidence, detached, run, status, script, source, parameters = dispatcher_prelaunch_recovery_fixture
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("当前主机缺少原生 tmux；非0与duplicate边界仍有隔离回归")
    fixture_root = script.parent
    script.write_text(source)
    short_socket_alias = Path("/tmp") / ("rvf021-duplicate-" + secrets.token_hex(5))
    short_socket_alias.symlink_to(fixture_root, target_is_directory=True)
    socket = short_socket_alias / "native-control.sock"
    wrapper = fixture_root / "isolated-native-tmux"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(tmux) + " -S " + shlex.quote(str(socket)) + " \"$@\"\n")
    wrapper.chmod(0o700)
    monkeypatch.setenv("RVF_TMUX_BIN", str(wrapper))
    old_writer = fixture_root / "preexisting-session-writer.py"
    old_writer.write_text("import json,os,sys,time\nfrom pathlib import Path\nroot=Path(sys.argv[1])\n"
        "with (root/'old-writer-heartbeats.jsonl').open('a') as output:\n"
        "    for index in range(120):\n"
        "        if (root/'stop-preexisting-writer').exists(): break\n"
        "        output.write(json.dumps({'pid':os.getpid(),'sid':os.getsid(0),'index':index})+'\\n'); output.flush(); time.sleep(.05)\n")
    start_argv = [tmux, "-S", str(socket), "new-session", "-d", "-s", parameters["session_name"],
        shlex.join([sys.executable, str(old_writer), str(fixture_root)])]
    try:
        old_launch = subprocess.run(start_argv, capture_output=True, text=True, timeout=5)
        assert old_launch.returncode == 0, old_launch.stderr
        heartbeat_path = fixture_root / "old-writer-heartbeats.jsonl"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (not heartbeat_path.exists() or len(heartbeat_path.read_text().splitlines()) < 2):
            time.sleep(.05)
        assert heartbeat_path.exists()
        before = len(heartbeat_path.read_text().splitlines())
        collision = detached.launch_detached(**parameters)
        assert collision["launch_status"] == "already_running"
        assert collision["recovery_state"] == "waiting_for_tmux_session_release"
        assert collision["tmux_command"] is None
        while time.monotonic() < deadline and len(heartbeat_path.read_text().splitlines()) <= before:
            time.sleep(.05)
        after = len(heartbeat_path.read_text().splitlines())
        assert after > before
        assert not status.exists()
        write_dispatcher_prelaunch_fixture_json(fixture_root / "native-duplicate-live-writer-observations.json", {
            "preexisting_session_argv": start_argv, "preexisting_session_returncode": old_launch.returncode,
            "collision_launch": collision, "heartbeat_count_before": before, "heartbeat_count_after": after,
            "first_writer_identity": json.loads(heartbeat_path.read_text().splitlines()[0]), "new_execution_created": status.exists()})
    finally:
        (fixture_root / "stop-preexisting-writer").write_text("stop\n")
        subprocess.run([tmux, "-S", str(socket), "kill-server"], capture_output=True, timeout=5)
        short_socket_alias.unlink(missing_ok=True)
