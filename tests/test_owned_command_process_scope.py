"""命令监督只查询已登记身份、自有进程组及其子链。"""
import ctypes
import errno
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from core.task_snapshot_review import execution_lifecycle, machine_evidence, native_owned_process_observation as observation
from core.task_snapshot_review import retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json


@pytest.fixture
def darwin_owned_process_queries(monkeypatch):
    supervisor_pid, owned_child, owned_orphan, unrelated_orphan = 800601, 800602, 800603, 800777
    processes = {
        supervisor_pid: (1, supervisor_pid, 100),
        owned_child: (supervisor_pid, supervisor_pid, 101),
        owned_orphan: (1, owned_orphan, 102),
        unrelated_orphan: (1, unrelated_orphan, 103),
    }
    queries, inspected = [], []
    denied = set()

    def process_info(pid, flavor, argument, pointer, size):
        assert pid != unrelated_orphan, '不能读取无关孤儿的详情'
        inspected.append(pid)
        if pid in denied or pid not in processes:
            ctypes.set_errno(errno.EACCES if pid in denied else errno.ESRCH)
            return 0
        info = pointer._obj
        if flavor == 17:
            info.unique_process_id = processes[pid][2]
        else:
            info.process_id = pid
            info.parent_process_id, info.process_group_id, info.start_seconds = processes[pid]
            info.process_status, info.user_id = 2, os.getuid()
        return size

    def process_list(selection, scope_id, destination, size):
        assert selection in {2, 6}, '禁止全 UID 或全 PID 枚举'
        assert scope_id != unrelated_orphan
        queries.append((selection, scope_id))
        selected = [pid for pid, (parent, group, _) in processes.items()
                    if (group if selection == 2 else parent) == scope_id]
        if destination is not None:
            for index, pid in enumerate(selected):
                destination[index] = pid
        return len(selected) * ctypes.sizeof(ctypes.c_int)

    library = SimpleNamespace(proc_pidinfo=process_info, proc_listpids=process_list)
    def load_library(name, **kwargs):
        assert name == '/usr/lib/libproc.dylib', '禁止全局孤儿环境查询'
        return library
    monkeypatch.setattr(observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', load_library)
    return SimpleNamespace(supervisor_pid=supervisor_pid, child_pid=owned_child, orphan_pid=owned_orphan,
        unrelated_pid=unrelated_orphan, processes=processes, queries=queries, inspected=inspected, denied=denied,
        identity={'pid': supervisor_pid, 'start_identity': [100, 0]})


def test_darwin_only_queries_owned_groups_and_parent_chains(darwin_owned_process_queries):
    state = darwin_owned_process_queries
    previous = {state.orphan_pid: {'pid': state.orphan_pid, 'start_identity': [102, 0]}}
    owned = observation.owned_process_snapshot(state.supervisor_pid, previous,
        supervisor_native_identity=state.identity)
    assert set(owned) == {state.child_pid, state.orphan_pid}
    assert state.unrelated_pid not in state.inspected


def test_owned_query_failure_keeps_proven_processes_for_cancellation(darwin_owned_process_queries):
    state = darwin_owned_process_queries
    state.denied.add(state.orphan_pid)
    previous = {state.child_pid: {'pid': state.child_pid, 'start_identity': [101, 0]},
                state.orphan_pid: {'pid': state.orphan_pid, 'start_identity': [102, 0]}}
    with pytest.raises(RuntimeError, match=str(state.orphan_pid)) as pending:
        observation.owned_process_snapshot(state.supervisor_pid, previous,
            supervisor_native_identity=state.identity)
    assert state.child_pid in pending.value.observed_owned_processes


def test_reused_supervisor_does_not_discover_foreign_group(darwin_owned_process_queries):
    state = darwin_owned_process_queries
    state.processes[state.supervisor_pid] = (1, state.supervisor_pid, 200)
    previous = {state.orphan_pid: {'pid': state.orphan_pid, 'start_identity': [102, 0]}}
    owned = observation.owned_process_snapshot(state.supervisor_pid, previous,
        supervisor_native_identity=state.identity)
    assert set(owned) == {state.orphan_pid}
    assert all(scope_id != state.supervisor_pid for _, scope_id in state.queries)
    assert state.child_pid not in state.inspected


def test_outer_wait_never_requests_global_ps(tmp_path, monkeypatch, darwin_owned_process_queries):
    state = darwin_owned_process_queries
    write_json(tmp_path / 'owned-processes.json', {'processes': [{
        'pid': state.supervisor_pid, 'native_identity': state.identity, 'start_and_command': 'registered',
        'purpose': 'final_validation', 'owns_process_group': True, 'is_active_registration': True}]})
    def scoped_process_identity(pid, **kwargs):
        return {'pid': pid, 'start_and_command': 'registered-child'}
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', scoped_process_identity)
    def forbid_global_ps(*args, **kwargs):
        pytest.fail('外层等待不能发起全局 ps')
    monkeypatch.setattr(execution_lifecycle.subprocess, 'run', forbid_global_ps)
    execution_lifecycle.observe_owned_process_group_descendants(tmp_path, state.supervisor_pid)
    records = read_json(tmp_path / 'owned-processes.json')['processes']
    assert state.child_pid in {item['pid'] for item in records}


@pytest.mark.parametrize('empty_children', [False, True])
def test_linux_only_reads_known_process_tasks(tmp_path, monkeypatch, empty_children):
    supervisor_pid, child_pid, observed_orphan = 800601, 800602, 800603
    process_root = tmp_path / 'owned-process-fixture'
    processes = {supervisor_pid: (1, 100), child_pid: (supervisor_pid, 101), observed_orphan: (1, 102)}
    for pid, (parent, start) in processes.items():
        directory = process_root / str(pid)
        task_directory = directory / 'task' / str(pid)
        task_directory.mkdir(parents=True)
        fields = ['S', str(parent), str(pid), *(['0'] * 16), str(start)]
        (directory / 'stat').write_text(f'{pid} (fixture) ' + ' '.join(fields))
        (task_directory / 'children').write_text(str(child_pid) if pid == supervisor_pid and not empty_children else '')
    requested = []
    def scoped_process_path(value):
        requested.append(value)
        assert value.startswith('/proc/') and value != '/proc/'
        assert 'environ' not in value
        assert int(value.split('/')[2]) in processes
        return process_root / value.removeprefix('/proc/')
    monkeypatch.setattr(observation, 'Path', scoped_process_path)
    monkeypatch.setattr(observation.sys, 'platform', 'linux')
    previous = {observed_orphan: {'pid': observed_orphan, 'start_identity': '102'}}
    owned = observation.owned_process_snapshot(supervisor_pid, previous,
        supervisor_native_identity={'pid': supervisor_pid, 'start_identity': '100'})
    assert set(owned) == ({observed_orphan} if empty_children else {child_pid, observed_orphan})
    assert '/proc' not in requested


@pytest.mark.parametrize('interrupted_registration', [False, True])
def test_missing_contract_or_interrupted_registration_never_becomes_exit_proof(tmp_path, monkeypatch, interrupted_registration):
    command_directory = tmp_path / 'commands/command-001'
    execution_lifecycle.register_supervised_command_directory(tmp_path, command_directory)
    facts = {'status': 'descendant_exit_unproven', 'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]}, 'owned_processes': [],
        'execution_token': 'original-command'}
    if interrupted_registration:
        facts.update({'process_ownership_scope': 'registered_process_identities_owned_groups_and_descendant_chains',
            'command_process_lifetime_contract': 'wait_for_background_work_or_register_separate_supervised_commands',
            'command_launch_intent_recorded': True})
    supervision_file = command_directory / 'command-supervision.json'
    write_json(supervision_file, facts)
    original = supervision_file.read_bytes()
    monkeypatch.setattr(observation, 'native_process_identity', lambda pid: None)
    monkeypatch.setattr(observation, 'owned_process_snapshot', lambda *args, **kwargs: {})
    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(tmp_path, tmp_path / 'commands')
    assert supervision_file.read_bytes() == original
    assert read_json(command_directory / 'descendant-exit-reconciliation.json')['status'] == 'unknown'


def test_outer_wait_defers_unchanged_query_failure_until_direct_exit(tmp_path, monkeypatch):
    write_json(tmp_path / 'run.json', {})
    observations = []
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants',
        lambda *args: observations.append(args) or False)
    class DirectCommandProcess:
        pid = 800601
        wait_count = 0

        def poll(self):
            return 0 if self.wait_count == 4 else None

        def wait(self, timeout):
            self.wait_count += 1
            if self.wait_count < 4:
                raise subprocess.TimeoutExpired('已登记命令', timeout)
            return 0
    assert execution_lifecycle.wait_for_owned_process(DirectCommandProcess(), tmp_path) == 0
    assert len(observations) == 2


def test_outer_partial_observation_survives_query_failure(tmp_path, monkeypatch, darwin_owned_process_queries):
    state = darwin_owned_process_queries
    write_json(tmp_path / 'owned-processes.json', {'processes': [{
        'pid': state.supervisor_pid, 'native_identity': state.identity, 'start_and_command': 'registered',
        'purpose': 'final_validation', 'owns_process_group': True, 'is_active_registration': True}]})
    error = RuntimeError('后续所属 PID 查询失败')
    error.observed_owned_processes = {state.child_pid: {'pid': state.child_pid, 'start_identity': [101, 0]}}
    def partial_observation(*args, **kwargs):
        raise error
    monkeypatch.setattr(execution_lifecycle, 'owned_process_snapshot', partial_observation)
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda pid: None)
    assert execution_lifecycle.observe_owned_process_group_descendants(tmp_path, state.supervisor_pid) is False
    assert state.child_pid in {item['pid'] for item in read_json(tmp_path / 'owned-processes.json')['processes']}


def test_stop_uses_saved_direct_identity_when_descendant_list_was_not_persisted(tmp_path, monkeypatch):
    child = {'pid': 800602, 'start_identity': [101, 0]}
    write_json(tmp_path / 'command-supervision.json', {'execution_token': 'registered-command',
        'supervisor_pid': 800601, 'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'command_pid': child['pid'], 'command_native_identity': child, 'owned_processes': []})
    def unavailable_snapshot(*args, **kwargs):
        raise RuntimeError('限定查询暂不可用')
    monkeypatch.setattr(retained_command_supervisor, 'owned_process_snapshot', unavailable_snapshot)
    monkeypatch.setattr(observation, 'native_process_identity', lambda pid: child if pid == child['pid'] else None)
    signals = []
    monkeypatch.setattr(os, 'kill', lambda *args: signals.append(args))
    retained_command_supervisor.terminate_supervised_command_descendants(tmp_path)
    assert [pid for pid, _ in signals] == [child['pid']]


def test_proven_direct_spawn_failure_can_resume_without_rewriting_original(tmp_path):
    run = tmp_path / 'direct-spawn-failure-recovery'
    run.mkdir()
    write_json(run / 'run.json', {})
    executable = tmp_path / 'temporarily-unavailable-command'
    command = [str(executable)]
    artifacts = run / 'commands/attempt-001'
    with exclusive_lock(run / 'collection.lock') as lock:
        with pytest.raises(machine_evidence.WorkspacePreparationFailed):
            machine_evidence.execute_registered_commands(run, tmp_path, [command], artifacts,
                'workspace_preparation', inherited_lock_descriptors=(lock.fileno(),))
    supervision_file = artifacts / 'command-001/command-supervision.json'
    original = supervision_file.read_bytes()
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    assert read_json(artifacts / 'command-001/descendant-exit-reconciliation.json')['status'] == 'not_started'
    with exclusive_lock(run / 'collection.lock') as lock:
        result = machine_evidence.execute_registered_commands(run, tmp_path, [command], run / 'commands/attempt-002',
            'workspace_preparation', inherited_lock_descriptors=(lock.fileno(),))
    assert result['status'] == 'passed'
    assert supervision_file.read_bytes() == original
