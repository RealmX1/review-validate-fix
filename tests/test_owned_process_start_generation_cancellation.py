"""正常 exec 不改变所属进程启动身份，复用 PID 或进程组缺证不能授权信号。"""
import os
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import execution_lifecycle
from core.task_snapshot_review.persistent_artifacts import write_json


@pytest.mark.parametrize("ignore_sigterm", [False, True])
def test_revocation_cancels_owned_process_after_exec_changes_command(tmp_path, ignore_sigterm):
    run = tmp_path / "owned-exec-revocation"
    run.mkdir()
    write_json(run / "run.json", {})
    gate, ready = run / "exec-gate", run / "python-ready"
    code = ("from pathlib import Path; import signal,sys,time; "
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_sigterm else "")
        + "Path(sys.argv[1]).write_text('ready'); time.sleep(60)")
    process = subprocess.Popen(["/bin/bash", "-c",
        'while [ ! -f "$1" ]; do /bin/sleep 0.01; done; exec "$2" -c "$3" "$4"',
        "owned-exec-validation", str(gate), sys.executable, code, str(ready)], start_new_session=True)
    try:
        execution_lifecycle.register_owned_process(run, process.pid, "final_validation", owns_process_group=True)
        gate.touch()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        execution_lifecycle.revoke_execution(run, "实际验证进程 exec 后撤销")
        assert process.wait(timeout=3) in {-signal.SIGTERM, -signal.SIGKILL}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.parametrize("recorded,current", [
    ("Wed Oct 28 12:00:00 2026 999999 old-command", "Wed Oct 28 12:00:01 2026 999999 new-command"),
    ("Wed Oct 28 12:00:00 2026 999999 old-command", "Wed Oct 28 12:00:00 2026 999998 new-command"),
    ("无法证明启动身份", "无法证明启动身份"),
])
def test_unproven_or_reused_process_identity_never_receives_signal(tmp_path, monkeypatch, recorded, current):
    run = tmp_path / "owned-process-identity-gate"
    run.mkdir()
    write_json(run / "run.json", {})
    write_json(run / "owned-processes.json", {"processes": [{"pid": 999999, "start_and_command": recorded,
        "purpose": "final_validation", "owns_process_group": True, "is_active_registration": True}]})
    monkeypatch.setattr(execution_lifecycle, "process_start_identity", lambda pid: {"pid": pid, "start_and_command": current})
    signals = []
    monkeypatch.setattr(os, "kill", lambda *arguments: signals.append(arguments))
    monkeypatch.setattr(os, "killpg", lambda *arguments: signals.append(arguments))
    execution_lifecycle.revoke_execution(run, "只核验所属启动身份")
    assert signals == []
