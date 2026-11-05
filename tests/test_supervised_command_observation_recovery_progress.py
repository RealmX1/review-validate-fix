"""观测缺证不终止已授权活跃命令，且不阻止已证明未派发的恢复。"""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import machine_evidence, retained_command_supervisor
from core.task_snapshot_review.native_owned_process_observation import native_process_identity
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import _validate
from test_task_snapshot_review_workflow import ROOT, repository


def clean_recorded_child(marker):
    if marker.exists():
        recorded = read_json(marker)
        if native_process_identity(recorded['pid']) == recorded:
            try:
                os.kill(recorded['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_live_registered_command_can_wait_for_reparented_clean_environment_descendant(repository, tmp_path):
    marker = tmp_path / 'environment-cleared-child-native-identity.json'
    finished = tmp_path / 'environment-cleared-descendant-finished'
    child_code = (
        f'import sys,time,json; from pathlib import Path; sys.path.insert(0,{str(ROOT)!r}); '
        'from core.task_snapshot_review.native_owned_process_observation import native_process_identity; '
        f'import os; Path({str(marker)!r}).write_text(json.dumps(native_process_identity(os.getpid()))); time.sleep(0.8); Path({str(finished)!r}).touch()'
    )
    intermediate = f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],env={{}},start_new_session=True)'
    command = [sys.executable, '-B', '-c',
        f'import subprocess,sys,time; from pathlib import Path; subprocess.run([sys.executable,"-B","-c",{intermediate!r}],check=True); '
        f'deadline=time.monotonic()+3\nwhile not Path({str(finished)!r}).exists():\n    assert time.monotonic()<deadline\n    time.sleep(0.01)']
    run = tmp_path / 'live-command-native-observation-recovery'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'continue authorized command',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    try:
        with exclusive_lock(run / 'workflow.lock') as lock:
            result = _validate(run, record, [command], lock)
        assert result['status'] == 'passed'
        facts = read_json(Path(result['attempt_directory']) / 'command-001/command-supervision.json')
        assert facts['status'] == 'completed'
        assert marker.exists()
    finally:
        clean_recorded_child(marker)


def test_stop_cancels_direct_child_handle_during_native_query_outage(tmp_path):
    run = tmp_path / 'direct-child-cancellation-with-observation-outage'
    run.mkdir()
    write_json(run / 'run.json', {})
    observations = run / 'command-observations'
    observations.mkdir()
    marker = tmp_path / 'direct-child-native-identity.json'
    late_effect = tmp_path / 'forbidden-direct-child-side-effect'
    command_code = (
        f'import os,sys,time,json; from pathlib import Path; sys.path.insert(0,{str(ROOT)!r}); '
        'from core.task_snapshot_review.native_owned_process_observation import native_process_identity; '
        f'os.setsid(); Path({str(marker)!r}).write_text(json.dumps(native_process_identity(os.getpid()))); '
        f'time.sleep(0.8); Path({str(late_effect)!r}).touch()'
    )
    command = [sys.executable, '-B', '-c', command_code]
    driver = (
        'from pathlib import Path; from unittest.mock import patch\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'calls=0\n'
        'def unavailable_after_dispatch(*args,**kwargs):\n'
        '    global calls\n'
        '    calls+=1\n'
        '    if calls==1: return {}\n'
        '    raise RuntimeError("派发后原生观测不可用")\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=unavailable_after_dispatch):\n'
        f'    raise SystemExit(supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(observations)!r}),(),{command!r}))\n'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', driver], start_new_session=True)
    try:
        deadline = time.monotonic() + 2
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        write_json(run / 'execution-revocation.json', {'reason': '自动化 Stop 验证'})
        process.wait(timeout=3)
        time.sleep(0.9)
        assert not late_effect.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        clean_recorded_child(marker)


def test_native_spawn_failure_can_retry_without_inventing_exit_evidence(repository, tmp_path, monkeypatch):
    run = tmp_path / 'native-spawn-failure-recovery'
    command = [sys.executable, '-B', '-c', 'pass']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'native launch failure recovery',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    original_popen = subprocess.Popen
    def temporarily_unavailable(command, *args, **kwargs):
        if any(str(argument).endswith('retained_command_supervisor.py') for argument in command):
            raise OSError(errno.EAGAIN, '原生命令启动资源暂不可用')
        return original_popen(command, *args, **kwargs)
    with monkeypatch.context() as failed_launch:
        failed_launch.setattr(machine_evidence.subprocess, 'Popen', temporarily_unavailable)
        with pytest.raises(OSError):
            machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    evidence = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert read_json(evidence)['status'] == 'passed'
    first = run / 'incomplete-fixer-machine-validation/round-001/attempt-001/commands/command-001'
    execution = read_json(first / 'execution.json')
    assert execution['status'] == 'launch_failed'
    assert execution['execution_started'] is False
    assert execution['native_launch_error']['errno'] == errno.EAGAIN
    assert not (first / 'command-supervision.json').exists()
