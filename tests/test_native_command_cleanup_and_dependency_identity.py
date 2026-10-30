"""原生查询缺证、跨 session 清理和依赖身份失效的独立回归。"""
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from core.task_snapshot_review import execution_lifecycle, machine_evidence, native_owned_process_observation
from core.task_snapshot_review import retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository


@pytest.mark.parametrize('previously_observed', [False, True])
def test_process_info_error_preserves_exit_unknown(monkeypatch, previously_observed):
    supervisor_pid, owned_pid = os.getpid(), 800101
    def process_info(pid, flavor, argument, pointer, size):
        if pid == owned_pid:
            ctypes.set_errno(errno.EIO)
            return 0
        info = pointer._obj
        info.process_id, info.parent_process_id = pid, 1
        info.process_group_id, info.start_seconds = pid, 100
        info.user_id, info.process_status = os.getuid(), 2
        return size
    def process_list(selection, user_id, destination, size):
        assert selection == 4 and user_id == os.getuid()
        if destination is not None:
            destination[0], destination[1] = supervisor_pid, owned_pid
        return 2 * ctypes.sizeof(ctypes.c_int)
    library = SimpleNamespace(proc_pidinfo=process_info, proc_listpids=process_list)
    monkeypatch.setattr(native_owned_process_observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: library)
    with pytest.raises(RuntimeError, match='800101'):
        native_owned_process_observation.owned_process_snapshot('owned-token', supervisor_pid,
            {owned_pid: {'pid': owned_pid, 'start_identity': [101, 0]}} if previously_observed else {})


def test_changed_external_dependency_invalidates_unproven_pass(repository, tmp_path, monkeypatch):
    dependency = tmp_path / 'external-installed-dependency'
    dependency.write_text('ready')
    command = [sys.executable, '-B', '-c', f'from pathlib import Path; assert Path({str(dependency)!r}).read_text()=="ready"']
    run = tmp_path / 'dependency-identity-validation'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'dependency freshness',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    original_execute = machine_evidence.execute_registered_commands
    def execute_without_native_supervision(*args, **kwargs):
        kwargs.pop('inherited_lock_descriptors', None)
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(machine_evidence, 'execute_registered_commands', execute_without_native_supervision)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    initial = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    initial_bytes = initial.read_bytes()
    assert read_json(initial)['status'] == 'passed'
    dependency.write_text('changed')
    current = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert current != initial
    assert read_json(current)['status'] == 'failed'
    assert read_json(current)['collection_attempt'] == 2
    assert initial.read_bytes() == initial_bytes


def test_native_observation_outage_does_not_abandon_known_detached_child(tmp_path):
    run = tmp_path / 'native-query-outage-cleanup'
    run.mkdir()
    write_json(run / 'run.json', {})
    artifacts = run / 'command-observations'
    artifacts.mkdir()
    started, late_effect = run / 'detached-child-pid', run / 'forbidden-late-effect'
    child_code = f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(1.2); Path({str(late_effect)!r}).touch()'
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],start_new_session=True)']
    script = (
        'import time; from pathlib import Path; from unittest.mock import patch\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'original=supervisor.owned_process_snapshot\nobserved_at=None\n'
        'def observe(*args):\n'
        ' global observed_at\n'
        ' if observed_at is not None and time.monotonic()-observed_at<0.57: raise RuntimeError("原生查询暂不可用")\n'
        ' result=original(*args)\n'
        f' if Path({str(started)!r}).exists() and result and observed_at is None: observed_at=time.monotonic()\n'
        ' return result\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=observe):\n'
        f' raise SystemExit(supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),(),{command!r}))\n'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    try:
        process.wait(timeout=3)
        assert started.exists()
        time.sleep(1.25)
        assert not late_effect.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_reconciles_detached_child_after_supervisor_exit(tmp_path):
    run = tmp_path / 'cancel-after-supervisor-exit'
    run.mkdir()
    write_json(run / 'run.json', {})
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        identity = native_owned_process_observation.native_process_identity(child.pid)
        execution_lifecycle.register_supervised_command_directory(run, run / 'commands/command-001')
        write_json(run / 'commands/command-001/command-supervision.json', {
            'execution_token': 'persisted-owned-execution', 'supervisor_pid': 99999999,
            'status': 'descendant_exit_unproven', 'owned_processes': [identity]})
        evidence = execution_lifecycle.revoke_execution(run, '监督者退出后补齐持久撤销')
        assert child.wait(timeout=1) == -signal.SIGKILL
        assert evidence['supervised_descendants'][0]['cancellation_status'] == 'termination_requested'
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_reused_native_owned_pid_does_not_receive_cancellation(monkeypatch):
    monkeypatch.setattr(native_owned_process_observation, 'native_process_identity',
        lambda pid: {'pid': pid, 'start_identity': [102, 0]})
    signals = []
    monkeypatch.setattr(os, 'kill', lambda *args: signals.append(args))
    observations = native_owned_process_observation.terminate_observed_owned_processes({
        800111: {'pid': 800111, 'start_identity': [101, 0]}})
    assert observations[0]['cancellation_status'] == 'identity_changed_no_signal'
    assert signals == []


@pytest.mark.parametrize('case', range(3))
def test_registered_command_waits_for_healthy_detached_descendant_completion(tmp_path, monkeypatch, case):
    run = tmp_path / 'registered-deadline-cleanup'
    run.mkdir()
    write_json(run / 'run.json', {})
    started, late_effect = run / 'child-pid', run / 'forbidden-late-effect'
    code = f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(0.85); Path({str(late_effect)!r}).touch()'
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{code!r}],start_new_session=True)']
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: None)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    with exclusive_lock(run / 'collection.lock') as lock:
        result = machine_evidence.execute_registered_commands(run, tmp_path, [command], run / 'commands', 'workspace_preparation',
            inherited_lock_descriptors=(lock.fileno(),))
    try:
        assert result['commands'][0]['returncode'] == 0
        assert started.exists()
        time.sleep(0.9)
        assert late_effect.exists()
    finally:
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
