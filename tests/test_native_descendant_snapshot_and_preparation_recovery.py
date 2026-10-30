"""进程表父链消失、最终验证未派发与角色准备重试的独立回归。"""
import errno
import os
from pathlib import Path
import sys

import pytest

from core.task_snapshot_review import machine_evidence, native_owned_process_observation as observation, workflow
from core.task_snapshot_review.execution_lifecycle import register_supervised_command_directory
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository
from test_owned_command_process_scope import darwin_owned_process_queries


def test_registered_child_survives_parent_disappearance(darwin_owned_process_queries):
    state = darwin_owned_process_queries
    previous = {state.orphan_pid: {'pid': state.orphan_pid, 'start_identity': [102, 0]}}
    state.processes[state.child_pid] = (1, state.child_pid, 101)
    previous[state.child_pid] = {'pid': state.child_pid, 'start_identity': [101, 0]}
    del state.processes[state.orphan_pid]
    owned = observation.owned_process_snapshot(state.supervisor_pid, previous,
        supervisor_native_identity=state.identity)
    assert set(owned) == {state.child_pid}


@pytest.mark.parametrize('unrelated_start', [99, 100, 101])
def test_unrelated_orphan_needs_no_origin_or_environment_query(darwin_owned_process_queries, unrelated_start):
    state = darwin_owned_process_queries
    state.processes[state.unrelated_pid] = (1, state.unrelated_pid, unrelated_start)
    owned = observation.owned_process_snapshot(state.supervisor_pid, {}, supervisor_native_identity=state.identity)
    assert set(owned) == {state.child_pid}
    assert state.unrelated_pid not in state.inspected


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
    write_json(previous / 'command-supervision.json', {'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'status': 'descendant_exit_unproven', 'supervisor_pid': 800601,
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
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
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


@pytest.mark.parametrize('discovery_query_fails', [False, True])
def test_reconciliation_retains_discovered_child_after_parent_exit_and_query_failure(
        tmp_path, monkeypatch, darwin_owned_process_queries, discovery_query_fails):
    state = darwin_owned_process_queries
    run = tmp_path / 'reconciliation-owned-child-recovery'
    command_directory = run / 'commands/attempt-001/command-001'
    register_supervised_command_directory(run, command_directory)
    parent_identity = observation.native_process_identity(state.child_pid)
    supervision_file = command_directory / 'command-supervision.json'
    write_json(supervision_file, {
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'status': 'running', 'supervisor_pid': state.supervisor_pid,
        'supervisor_native_identity': state.identity,
        'command_pid': state.child_pid, 'command_native_identity': parent_identity,
        'owned_processes': [parent_identity],
    })
    original = supervision_file.read_bytes()
    del state.processes[state.supervisor_pid]
    state.processes[state.orphan_pid] = (state.child_pid, state.orphan_pid, 102)
    reconciliation_file = command_directory / 'descendant-exit-reconciliation.json'
    scoped_query = observation.darwin_scoped_process_ids

    def fail_after_child_discovery(selection, scope_id):
        if scope_id == state.orphan_pid:
            raise RuntimeError('新发现子进程的后续归属查询暂不可用')
        return scoped_query(selection, scope_id)

    with monkeypatch.context() as discovery:
        if discovery_query_fails:
            discovery.setattr(observation, 'darwin_scoped_process_ids', fail_after_child_discovery)
        with pytest.raises(machine_evidence.WorkspacePreparationFailed):
            machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    first = read_json(reconciliation_file)
    assert first['status'] == ('unknown' if discovery_query_fails else 'pending')
    assert state.orphan_pid in {item['pid'] for item in first['owned_processes']}

    del state.processes[state.child_pid]
    state.processes[state.orphan_pid] = (1, state.orphan_pid, 102)
    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    orphaned_child = read_json(reconciliation_file)
    assert orphaned_child['status'] == 'pending'
    assert {item['pid'] for item in orphaned_child['owned_processes']} == {state.orphan_pid}

    native_identity = observation.native_process_identity

    def unavailable_supervisor_identity(pid):
        if pid == state.supervisor_pid:
            raise RuntimeError('监督者启动身份查询暂不可用')
        return native_identity(pid)

    with monkeypatch.context() as unavailable:
        unavailable.setattr(observation, 'native_process_identity', unavailable_supervisor_identity)
        with pytest.raises(machine_evidence.WorkspacePreparationFailed):
            machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    unavailable_facts = read_json(reconciliation_file)
    assert unavailable_facts['status'] == 'unknown'
    assert state.orphan_pid in {item['pid'] for item in unavailable_facts['owned_processes']}

    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    live_child = read_json(reconciliation_file)
    assert live_child['status'] == 'pending'
    assert {item['pid'] for item in live_child['owned_processes']} == {state.orphan_pid}

    state.processes[state.orphan_pid] = (1, state.orphan_pid, 202)
    machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
    assert read_json(reconciliation_file)['status'] == 'exited'
    assert state.unrelated_pid not in state.inspected
    assert supervision_file.read_bytes() == original
