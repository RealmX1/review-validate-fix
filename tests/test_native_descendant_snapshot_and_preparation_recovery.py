"""进程表父链消失、最终验证未派发与角色准备重试的独立回归。"""
import ctypes
import errno
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core.task_snapshot_review import machine_evidence, native_owned_process_observation as observation, workflow
from core.task_snapshot_review.execution_lifecycle import register_supervised_command_directory
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository


@pytest.mark.parametrize('query_errno', [errno.ESRCH, errno.EINVAL, errno.EIO])
def test_darwin_disappearing_parent_requires_fresh_snapshot(monkeypatch, query_errno):
    supervisor_pid, parent_pid, child_pid = 800601, 800602, 800603
    identities = {supervisor_pid: (1, 100), parent_pid: (1, 101), child_pid: (parent_pid, 102)}
    queried = []
    def process_info(pid, flavor, argument, pointer, size):
        info = pointer._obj
        info.process_id, info.process_group_id = pid, pid
        info.parent_process_id, info.start_seconds = identities[pid]
        info.user_id, info.process_status = os.getuid(), 2
        return size
    def process_list(selection, user_id, destination, size):
        assert selection == 4 and user_id == os.getuid()
        if destination is not None:
            for index, pid in enumerate(identities):
                destination[index] = pid
        return len(identities) * ctypes.sizeof(ctypes.c_int)
    def environment_query(query, count, arguments, length, replacement, replacement_length):
        pid = query[2]
        queried.append(pid)
        if pid == parent_pid:
            ctypes.set_errno(query_errno)
            return -1
        arguments.value = (observation.REGISTERED_COMMAND_OWNERSHIP_ENVIRONMENT_KEY + '=current-execution').encode()
        length._obj.value = len(arguments.value) + 1
        return 0
    library = SimpleNamespace(proc_pidinfo=process_info, proc_listpids=process_list)
    system = SimpleNamespace(sysctl=environment_query)
    monkeypatch.setattr(observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda name, **kwargs: library if name else system)
    identity = {'pid': supervisor_pid, 'start_identity': [100, 0]}
    with pytest.raises(RuntimeError, match=str(parent_pid)):
        observation.owned_process_snapshot('current-execution', supervisor_pid, {}, supervisor_native_identity=identity)
    del identities[parent_pid]
    owned = observation.owned_process_snapshot('current-execution', supervisor_pid, {}, supervisor_native_identity=identity)
    assert set(owned) == {child_pid}
    assert queried == [parent_pid, child_pid]


@pytest.mark.parametrize(('parent_unique_id', 'supervisor_unique_id', 'unrelated'), [
    (99, 100, True), (100, 100, False), (101, 100, False),
    (1, 100, False), (0, 100, False), (99, None, False),
])
def test_darwin_orphan_origin_distinguishes_older_parent_from_unproven_descendant(
        monkeypatch, parent_unique_id, supervisor_unique_id, unrelated):
    supervisor_pid, orphan_pid = 800601, 800603
    def process_info(pid, flavor, argument, pointer, size):
        info = pointer._obj
        if flavor == 17:
            info.unique_process_id = 1 if pid == 1 else 102
            info.parent_unique_process_id = parent_unique_id
            info.process_id_version = 10 if pid == 1 else 30
            info.original_parent_process_id_version = 20
        else:
            info.process_id, info.parent_process_id, info.process_group_id = pid, 1, pid
            info.start_seconds = 100 if pid == supervisor_pid else 101
            info.user_id, info.process_status = os.getuid(), 2
        return size
    def process_list(selection, user_id, destination, size):
        if destination is not None:
            destination[0], destination[1] = supervisor_pid, orphan_pid
        return 2 * ctypes.sizeof(ctypes.c_int)
    def environment_query(query, count, arguments, length, replacement, replacement_length):
        arguments.value = b''
        length._obj.value = 1
        return 0
    library = SimpleNamespace(proc_pidinfo=process_info, proc_listpids=process_list)
    system = SimpleNamespace(sysctl=environment_query)
    monkeypatch.setattr(observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda name, **kwargs: library if name else system)
    identity = {'pid': supervisor_pid, 'start_identity': [100, 0], 'unique_process_id': supervisor_unique_id}
    if unrelated:
        assert observation.owned_process_snapshot('current-execution', supervisor_pid, {},
            supervisor_native_identity=identity) == {}
    else:
        with pytest.raises(RuntimeError, match=str(orphan_pid)):
            observation.owned_process_snapshot('current-execution', supervisor_pid, {},
                supervisor_native_identity=identity)


def test_linux_disappearing_parent_requires_fresh_snapshot(tmp_path, monkeypatch):
    supervisor_pid, parent_pid, child_pid = 800601, 800602, 800603
    process_root = tmp_path / 'native-process-table'
    process_root.mkdir()
    def write_process(pid, parent_pid, start):
        directory = process_root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['S', str(parent_pid), str(pid), *(['0'] * 16), str(start)]
        (directory / 'stat').write_text(f'{pid} (process) ' + ' '.join(fields))
    write_process(supervisor_pid, 1, 100)
    write_process(parent_pid, 1, 101)
    write_process(child_pid, parent_pid, 102)
    (process_root / str(child_pid) / 'environ').write_bytes(
        (observation.REGISTERED_COMMAND_OWNERSHIP_ENVIRONMENT_KEY + '=current-execution\0').encode())
    monkeypatch.setattr(observation.sys, 'platform', 'linux')
    monkeypatch.setattr(observation, 'Path', lambda value: process_root if value == '/proc' else Path(value))
    identity = {'pid': supervisor_pid, 'start_identity': '100'}
    with pytest.raises(RuntimeError, match=str(parent_pid)):
        observation.owned_process_snapshot('current-execution', supervisor_pid, {}, supervisor_native_identity=identity)
    (process_root / str(parent_pid) / 'stat').unlink()
    (process_root / str(parent_pid)).rmdir()
    owned = observation.owned_process_snapshot('current-execution', supervisor_pid, {}, supervisor_native_identity=identity)
    assert set(owned) == {child_pid}


@pytest.mark.parametrize('resume_without_commands', [False, True])
def test_final_validation_native_launch_failure_can_resume(repository, tmp_path, monkeypatch, resume_without_commands):
    run = tmp_path / 'final-validation-native-launch-recovery'
    command = [sys.executable, '-B', '-c', 'pass']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'launch recovery',
        run_directory=run, execution_plan=None if resume_without_commands else {'fix': True, 'validation_commands': [command]})
    original_popen = workflow.subprocess.Popen
    def temporarily_unavailable(command, *args, **kwargs):
        if any(str(argument).endswith('retained_command_supervisor.py') for argument in command):
            raise OSError(errno.EAGAIN, '原生命令启动资源暂不可用')
        return original_popen(command, *args, **kwargs)
    with exclusive_lock(run / 'workflow.lock') as lock:
        with monkeypatch.context() as failed_launch:
            failed_launch.setattr(workflow.subprocess, 'Popen', temporarily_unavailable)
            with pytest.raises(OSError):
                workflow._validate(run, record, [command], lock)
        first = run / 'final-validation/attempt-001/command-001'
        facts = read_json(first / 'execution.json')
        assert facts['status'] == 'launch_failed'
        assert facts['execution_started'] is False
        assert facts['native_launch_error']['errno'] == errno.EAGAIN
        assert not (first / 'command-supervision.json').exists()
        if not resume_without_commands:
            assert workflow._validate(run, record, [command], lock)['status'] == 'passed'
    if resume_without_commands:
        # 复用已完成角色的结果，验证无新命令参数的接续入口，不派发模型。
        monkeypatch.setattr(workflow, '_run_roles', lambda *args, **kwargs: [{'result': {'findings': []}}] * 2)
        monkeypatch.setattr(workflow, '_role_result', lambda *args, **kwargs: None)
        configuration = {'command': [sys.executable, '-c', 'pass']}
        result = workflow._resume_review_iteration(run, reviewer_configs=[configuration, configuration])
        assert result['status'] == 'ready_to_commit'
    assert read_json(run / 'validation.json')['status'] == 'passed'
    assert read_json(first / 'descendant-exit-reconciliation.json')['status'] == 'not_started'
    assert len(list((run / 'final-validation').glob('attempt-*'))) == 2


@pytest.mark.parametrize('same_attempt', [False, True])
def test_role_preparation_waits_for_previous_descendants(repository, tmp_path, monkeypatch, same_attempt):
    run = tmp_path / 'role-preparation-descendant-recovery'
    write_json(run / 'run.json', {})
    previous = run / 'reviewer-1/attempt-001/preparation/command-001'
    register_supervised_command_directory(run, previous)
    write_json(previous / 'command-supervision.json', {'status': 'descendant_exit_unproven', 'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'execution_token': 'previous-execution', 'owned_processes': []})
    descendants = {800603: {'pid': 800603, 'start_identity': [102, 0]}}
    monkeypatch.setattr(observation, 'native_process_identity', lambda pid: None)
    monkeypatch.setattr(observation, 'owned_process_snapshot', lambda *args, **kwargs: descendants.copy())
    executions = []
    monkeypatch.setattr(machine_evidence, 'execute_registered_commands',
        lambda *args, **kwargs: executions.append(args) or {'status': 'passed', 'commands': []})
    artifacts = previous.parents[1] if same_attempt else run / 'reviewer-1/attempt-002'
    command = [sys.executable, '-c', 'pass']
    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.prepare_isolated_workspace(run, repository, [command], artifacts)
    assert executions == []
    assert read_json(previous / 'descendant-exit-reconciliation.json')['status'] == 'pending'
    descendants.clear()
    machine_evidence.prepare_isolated_workspace(run, repository, [command], artifacts)
    assert len(executions) == 1
    assert read_json(previous / 'descendant-exit-reconciliation.json')['status'] == 'exited'


@pytest.mark.parametrize('current_start_seconds', [100, 101, None])
def test_additional_native_identity_evidence_preserves_live_supervisor_recovery_gate(
        tmp_path, monkeypatch, current_start_seconds):
    run = tmp_path / 'supervisor-native-identity-recovery'
    command_directory = run / 'machine-evidence/attempt-001/commands/command-001'
    write_json(run / 'run.json', {})
    register_supervised_command_directory(run, command_directory)
    write_json(command_directory / 'command-supervision.json', {
        'status': 'descendant_exit_unproven', 'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'execution_token': 'recorded-execution', 'owned_processes': [],
    })
    current_identity = (None if current_start_seconds is None else {
        'pid': 800601, 'start_identity': [current_start_seconds, 0], 'unique_process_id': 123456,
    })
    monkeypatch.setattr(observation, 'native_process_identity', lambda pid: current_identity)
    monkeypatch.setattr(observation, 'owned_process_snapshot', lambda *args, **kwargs: {})
    if current_start_seconds == 100:
        with pytest.raises(machine_evidence.WorkspacePreparationFailed):
            machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'machine-evidence')
        expected_status = 'pending'
    else:
        machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'machine-evidence')
        expected_status = 'exited'
    assert read_json(command_directory / 'descendant-exit-reconciliation.json')['status'] == expected_status
