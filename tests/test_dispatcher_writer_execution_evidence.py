"""实际 dispatcher runtime 身份、原生 tmux/monitor 丢失及自动交付补证。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from _rvf_test_support.loader import load_script_module


def write_dispatcher_writer_execution_fixture_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def wait_until_dispatcher_writer_execution_fixture_condition(predicate, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("实际 dispatcher fixture 未在有界时间内到达预期状态")


@pytest.fixture
def bound_dispatcher_writer_execution_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    for name in ("RVF_SUPPRESS", "RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD",
                 "RVF_DISPATCHER_WRITER_EXECUTION_ID", "RVF_DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH"):
        monkeypatch.delenv(name, raising=False)
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    run = tmp_path / "rvf-review-start-0200200200200200"
    repo = tmp_path / "isolated-repository"
    repo.mkdir()
    write_dispatcher_writer_execution_fixture_json(run / "summary.json", {"run_id": run.name, "run_dir": str(run), "repo": str(repo), "session_id": "rvf020-session"})
    write_dispatcher_writer_execution_fixture_json(run / "artifacts/inputs/scope.contract.json", {"run_id": run.name, "repo": str(repo), "session_id": "rvf020-session"})
    script = tmp_path / "actual-dispatcher-runtime.py"
    script.write_text("import json,os,sys,time\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(Path(evidence.__file__).parent)) + ")\n"
        "from dispatcher_writer_execution_evidence import run_actual_dispatcher_with_writer_execution_evidence\n"
        "root=Path(sys.argv[1])\n"
        "def actual_dispatcher_body():\n"
        "    with (root/'writer-heartbeats.jsonl').open('a') as stream:\n"
        "        for index in range(400):\n"
        "            if (root/'stop-actual-dispatcher').exists(): break\n"
        "            stream.write(json.dumps({'pid':os.getpid(),'sid':os.getsid(0),'index':index})+'\\n'); stream.flush(); time.sleep(.05)\n"
        "    return 0\n"
        "raise SystemExit(run_actual_dispatcher_with_writer_execution_evidence(actual_dispatcher_body,dispatcher_script_path=Path(__file__).resolve()))\n",
        encoding="utf-8")
    status = run / "artifacts/reviewers/.dispatch-thread.status.json"
    payload = {"schema_version": 1, "kind": "dispatch", "run_id": run.name, "run_dir": str(run), "repo": str(repo),
        "session_id": "rvf020-session", "tmux_session": "rvf020-private-dispatch", "launch_status": "launched",
        "returncode": None, "command": [sys.executable, str(script), str(tmp_path)]}
    return evidence, run, repo, status, payload, script


def prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload):
    bound = evidence.prepare_dispatcher_writer_execution_evidence_binding(status, payload)
    bound = evidence.record_dispatcher_writer_execution_tmux_attempt_boundary(status, bound["dispatcher_writer_execution_id"])
    write_dispatcher_writer_execution_fixture_json(status, bound)
    return bound, Path(bound["dispatcher_writer_execution_evidence_path"])


def test_tmux_absence_and_launcher_exit_without_actual_runtime_remain_unknown(bound_dispatcher_writer_execution_fixture, monkeypatch):
    evidence, run, _, status, payload, _ = bound_dispatcher_writer_execution_fixture
    bound, path = prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload)
    record = json.loads(path.read_text())
    record.update({"dispatcher_writer_execution_monitor_pid": 1234, "dispatcher_writer_execution_monitor_child_pid": 5678, "dispatcher_writer_execution_monitor_child_returncode": 0,
                   "preparation_boot": {"host_identity_sha256": "same", "boot_session_id": "old"}})
    write_dispatcher_writer_execution_fixture_json(path, record)
    monkeypatch.setattr(evidence, "current_local_host_boot_identity_for_dispatcher_writer_execution", lambda: {"host_identity_sha256": "same", "boot_session_id": "new"})
    detached = load_script_module("rvf_detached_thread")
    monkeypatch.setattr(detached, "_tmux_session_alive", lambda session: False)
    archive = load_script_module("review_run_terminal_archive")
    observed = []
    assert archive._writers_still_active(run, observed)
    assert observed[0]["status"] == "unknown"
    assert observed[0]["recovery_condition"]
    lock = status.with_name(".dispatch-thread.launch.lock")
    lock.write_text("owned\n")
    assert detached._reclaim_stale_detached_lock(lock_path=lock, session_name=bound["tmux_session"],
        status_path=status, require_actual_dispatcher_writer_execution_evidence=True) is None
    assert lock.exists()


@pytest.mark.parametrize("new_host,new_boot,quiet", [("same-host", "old-boot", False), ("foreign-host", "new-boot", False), ("same-host", "new-boot", True)])
def test_boot_alternative_requires_actual_runtime_and_same_host(bound_dispatcher_writer_execution_fixture, monkeypatch, new_host, new_boot, quiet):
    evidence, _, _, status, payload, script = bound_dispatcher_writer_execution_fixture
    bound, _ = prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload)
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE, bound["dispatcher_writer_execution_id"])
    monkeypatch.setenv(evidence.DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE, bound["dispatcher_writer_execution_evidence_path"])
    monkeypatch.setattr(sys, "argv", [str(script), str(script.parent)])
    monkeypatch.setattr(evidence, "current_local_host_boot_identity_for_dispatcher_writer_execution", lambda: {"host_identity_sha256": "same-host", "boot_session_id": "old-boot"})
    def interrupted_body():
        raise KeyboardInterrupt("实际 runtime 未交付，不能以外壳退出补成成功")
    with pytest.raises(KeyboardInterrupt):
        evidence.run_actual_dispatcher_with_writer_execution_evidence(interrupted_body, dispatcher_script_path=script)
    monkeypatch.setattr(evidence, "current_local_host_boot_identity_for_dispatcher_writer_execution", lambda: {"host_identity_sha256": new_host, "boot_session_id": new_boot})
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"] is (not quiet)


@pytest.mark.parametrize("invalid", ["dispatcher_writer_execution_id", "run_id", "dispatcher_writer_execution_command_sha256", "symlink"])
def test_foreign_or_replaced_execution_evidence_never_releases_gate(bound_dispatcher_writer_execution_fixture, invalid):
    evidence, _, _, status, payload, _ = bound_dispatcher_writer_execution_fixture
    _, path = prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload)
    value = json.loads(path.read_text())
    if invalid == "symlink":
        target = path.with_name("foreign-execution.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        value[invalid] = "foreign"
        write_dispatcher_writer_execution_fixture_json(path, value)
    assert evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"] is True


def start_dispatcher_writer_execution_fixture_monitor(evidence, status, bound, log):
    argv = evidence.build_dispatcher_writer_execution_monitor_argv(status, bound, 20)
    with log.open("w") as output:
        process = subprocess.Popen(argv, stdout=output, stderr=output, start_new_session=True)
    return process


def finish_actual_dispatcher_writer_execution_fixture(script, evidence, status):
    (script.parent / "stop-actual-dispatcher").write_text("stop\n")
    wait_until_dispatcher_writer_execution_fixture_condition(lambda: not evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"])


@pytest.mark.parametrize("forwarder", [False, True])
def test_monitor_or_python_forwarder_exit_does_not_hide_live_actual_writer(bound_dispatcher_writer_execution_fixture, forwarder):
    evidence, run, _, status, payload, script = bound_dispatcher_writer_execution_fixture
    if forwarder:
        selected = script.parent / "selected-python-forwarder.py"
        selected.write_text("#!" + sys.executable + "\nimport json,os,subprocess,sys\nfrom pathlib import Path\n"
            "child=subprocess.Popen([" + repr(sys.executable) + ",*sys.argv[1:]],start_new_session=True)\n"
            "Path(sys.argv[2]).joinpath('forwarder-exit.json').write_text(json.dumps({'forwarder_pid':os.getpid(),'actual_child_pid':child.pid}))\n")
        selected.chmod(0o755)
        payload["command"][0] = str(selected)
    bound, path = prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload)
    monitor = start_dispatcher_writer_execution_fixture_monitor(evidence, status, bound, script.parent / "monitor.log")
    try:
        beats = script.parent / "writer-heartbeats.jsonl"
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: beats.exists() and len(beats.read_text().splitlines()) >= 3)
        if forwarder:
            wait_until_dispatcher_writer_execution_fixture_condition(lambda: json.loads(path.read_text()).get("dispatcher_writer_execution_monitor_child_returncode") == 0)
            actual = json.loads(path.read_text())
            forwarded = json.loads((script.parent / "forwarder-exit.json").read_text())
            assert actual["actual_dispatcher_runtime_pid"] == forwarded["actual_child_pid"]
            assert actual["dispatcher_writer_execution_monitor_child_pid"] == forwarded["forwarder_pid"]
            assert actual["dispatcher_writer_execution_monitor_child_pid"] != actual["actual_dispatcher_runtime_pid"]
        else:
            monitor.kill()
            assert monitor.wait(timeout=5) == -signal.SIGKILL
        before = len(beats.read_text().splitlines())
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: len(beats.read_text().splitlines()) > before + 2)
        while_alive = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
        assert while_alive["dispatcher_writers_may_still_write"]
        write_dispatcher_writer_execution_fixture_json(script.parent / "native-writer-negative-observations.json", {
            "selected_python_forwarder": forwarder, "monitor_argv": monitor.args,
            "monitor_returncode_while_writer_alive": monitor.poll(), "heartbeat_count_before": before,
            "heartbeat_count_after": len(beats.read_text().splitlines()), "writer_gate_while_alive": while_alive,
            "actual_runtime_record_while_alive": json.loads(path.read_text())})
        archive = load_script_module("review_run_terminal_archive")
        assert archive._writers_still_active(run)
        finish_actual_dispatcher_writer_execution_fixture(script, evidence, status)
        observed = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
        assert observed["evidence_kind"] == "actual_dispatcher_delivery"
        assert observed["process_exit_status"] == "unknown"
    finally:
        (script.parent / "stop-actual-dispatcher").write_text("stop\n")
        if monitor.poll() is None:
            monitor.wait(timeout=8)


def test_actual_dispatcher_kill_does_not_hide_surviving_descendant_writer(bound_dispatcher_writer_execution_fixture):
    evidence, run, _, status, payload, script = bound_dispatcher_writer_execution_fixture
    descendant = script.parent / "owned-reviewer-descendant.py"
    descendant.write_text("import json,os,sys,time\nfrom pathlib import Path\nroot=Path(sys.argv[1])\n"
        "with (root/'descendant-heartbeats.jsonl').open('a') as stream:\n"
        "    for index in range(400):\n"
        "        if (root/'stop-owned-descendant').exists(): break\n"
        "        stream.write(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'sid':os.getsid(0)})+'\\n');stream.flush();time.sleep(.05)\n"
        "(root/'owned-descendant-exit.json').write_text('{}')\n", encoding="utf-8")
    script.write_text("import subprocess,sys\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(Path(evidence.__file__).parent)) + ")\n"
        "from dispatcher_writer_execution_evidence import run_actual_dispatcher_with_writer_execution_evidence\n"
        "def actual_dispatcher_body():\n"
        "    child=subprocess.Popen([sys.executable," + repr(str(descendant)) + ",sys.argv[1]],start_new_session=True)\n"
        "    return child.wait()\n"
        "raise SystemExit(run_actual_dispatcher_with_writer_execution_evidence(actual_dispatcher_body,dispatcher_script_path=Path(__file__).resolve()))\n",
        encoding="utf-8")
    bound, path = prepare_dispatcher_writer_execution_fixture_binding(evidence, status, payload)
    monitor = start_dispatcher_writer_execution_fixture_monitor(evidence, status, bound, script.parent / "descendant-monitor.log")
    try:
        beats = script.parent / "descendant-heartbeats.jsonl"
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: beats.exists() and len(beats.read_text().splitlines()) >= 3)
        child_identity = json.loads(beats.read_text().splitlines()[0])
        runtime = json.loads(path.read_text())
        assert child_identity["ppid"] == runtime["actual_dispatcher_runtime_pid"]
        assert child_identity["sid"] == child_identity["pid"]
        os.kill(runtime["actual_dispatcher_runtime_pid"], signal.SIGKILL)
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: json.loads(path.read_text()).get("dispatcher_writer_execution_monitor_child_returncode") == -signal.SIGKILL)
        before = len(beats.read_text().splitlines())
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: len(beats.read_text().splitlines()) > before + 2)
        observed = evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)
        assert observed["dispatcher_writers_may_still_write"]
        archive = load_script_module("review_run_terminal_archive")
        assert archive._writers_still_active(run)
        write_dispatcher_writer_execution_fixture_json(script.parent / "native-descendant-writer-negative-observations.json", {
            "killed_actual_dispatcher_pid": runtime["actual_dispatcher_runtime_pid"], "surviving_descendant": child_identity,
            "heartbeat_count_before": before, "heartbeat_count_after": len(beats.read_text().splitlines()),
            "actual_runtime_after_root_kill": json.loads(path.read_text()), "writer_gate": observed})
    finally:
        (script.parent / "stop-owned-descendant").write_text("stop\n")
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: (script.parent / "owned-descendant-exit.json").exists())
        if monitor.poll() is None:
            monitor.kill()
            monitor.wait(timeout=5)


def test_real_tmux_loss_blocks_archive_and_recovers_from_actual_dispatcher_delivery(bound_dispatcher_writer_execution_fixture, monkeypatch):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("当前主机没有 tmux；其余实际 runtime/forwarder 用例仍执行")
    evidence, run, repo, status, payload, script = bound_dispatcher_writer_execution_fixture
    from core.run_ledger.run_ledger import RunLedger
    dispatcher = load_script_module("dispatch_reviewers")
    archive = load_script_module("review_run_terminal_archive")
    detached = load_script_module("rvf_detached_thread")
    socket_directory = script.parent / "private-tmux-socket-directory"
    socket_directory.mkdir(mode=0o700)
    alias = Path("/tmp") / ("rvf020-socket-" + secrets.token_hex(6))
    alias.symlink_to(socket_directory, target_is_directory=True)
    socket = alias / "control.sock"
    wrapper = script.parent / "private-tmux-wrapper.py"
    wrapper.write_text("#!" + sys.executable + "\nimport os,sys\nos.execv(" + repr(tmux) + ",["
                       + repr(tmux) + ",'-S'," + repr(str(socket)) + ",*sys.argv[1:]])\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("RVF_TMUX_BIN", str(wrapper))
    monkeypatch.delenv("TMUX", raising=False)
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    ledger = RunLedger(component="dispatch", repo=repo, run_id=run.name, run_dir=run)
    def launch():
        with patch.object(dispatcher, "require_prepared_run_review_start_boundary", return_value={"session_id": "rvf020-session"}), patch.object(
            dispatcher, "_build_detached_child_argv", return_value=payload["command"]
        ):
            return dispatcher.launch_detached_dispatch(argparse.Namespace(total_timeout=20), ledger,
                status.parent, repo=str(repo), review_packet=None, session_context=None, scope_contract=None)
    try:
        assert launch() == 0
        beats = script.parent / "writer-heartbeats.jsonl"
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: beats.exists() and len(beats.read_text().splitlines()) >= 3)
        native_writer = json.loads(beats.read_text().splitlines()[-1])
        assert native_writer["sid"] == native_writer["pid"]
        original_id = json.loads(status.read_text())["dispatcher_writer_execution_id"]
        subprocess.run([tmux, "-S", str(socket), "kill-server"], check=True)
        assert detached._tmux_session_alive(json.loads(status.read_text())["tmux_session"]) is False
        before = len(beats.read_text().splitlines())
        wait_until_dispatcher_writer_execution_fixture_condition(lambda: len(beats.read_text().splitlines()) > before + 2)
        pending = archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted", reason="隔离 tmux 丢失",
                                                            event={"session_id": "rvf020-session"})
        assert pending["status"] == "waiting_for_evidence"
        assert not calls
        assert launch() == 0
        assert json.loads(status.read_text())["dispatcher_writer_execution_id"] == original_id
        finish_actual_dispatcher_writer_execution_fixture(script, evidence, status)
        assert json.loads(status.read_text())["returncode"] is None
        result = archive.resume_pending_review_run_terminal_archives(repo=repo, session_id="rvf020-session")[0]
        assert result["status"] == "archived"
        assert result["writer_evidence"][0]["evidence_kind"] == "actual_dispatcher_delivery"
        assert len(calls) == 1
        write_dispatcher_writer_execution_fixture_json(script.parent / "native-tmux-loss-observations.json", {"native_writer_identity": native_writer,
            "tmux_server_kill_argv": [tmux, "-S", str(socket), "kill-server"],
            "heartbeat_count_before_tmux_loss": before, "heartbeat_count_after_tmux_loss": len(beats.read_text().splitlines()),
            "waiting_archive_status": pending["status"], "final_archive_status": result["status"],
            "unchanged_execution_id_after_relaunch_attempt": original_id, "finalize_invocation_count": len(calls),
            "dispatch_status_returncode_after_actual_delivery": json.loads(status.read_text())["returncode"],
            "actual_delivery_evidence": result["writer_evidence"][0]})
    finally:
        (script.parent / "stop-actual-dispatcher").write_text("stop\n")
        subprocess.run([tmux, "-S", str(socket), "kill-server"], capture_output=True, check=False)
        alias.unlink(missing_ok=True)
