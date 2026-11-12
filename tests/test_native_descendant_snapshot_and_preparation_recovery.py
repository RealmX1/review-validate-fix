"""进程表父链消失、最终验证未派发与角色准备重试的独立回归。"""
import errno
import json
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
        assert read_json(run / 'run.json')['status'] == 'ready_to_commit'
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
        'execution_token': 'reconciliation-owned-child-execution',
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


@pytest.mark.parametrize('collection_path', [
    'reviewer-1/attempt-001/preparation',
    'machine-evidence/attempt-001/commands',
    'final-validation/attempt-001/commands',
])
def test_interrupted_registered_launch_reconciles_after_native_host_boot_changes(
        tmp_path, monkeypatch, collection_path):
    run_directory = tmp_path / 'interrupted-launch-recovery'
    collection_directory = run_directory / collection_path
    command_directory = collection_directory / 'command-001'
    register_supervised_command_directory(run_directory, command_directory)
    supervision_file = command_directory / 'command-supervision.json'
    write_json(supervision_file, {
        'execution_token': 'interrupted-registered-execution',
        'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0], 'unique_process_id': 1001},
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'host_boot_identity': 'previous-native-boot',
        'status': 'running',
        'command_launch_intent_recorded': True,
        'ownership_observation_coverage_status': 'unknown',
    })
    original_supervision = supervision_file.read_bytes()
    boot_observations = []
    current_boot = ['previous-native-boot']

    def observe_native_boot():
        boot_observations.append(current_boot[0])
        return current_boot[0]

    monkeypatch.setattr(observation, 'native_host_boot_identity', observe_native_boot)
    monkeypatch.setattr(observation, 'native_process_identity',
        lambda *_arguments, **_keywords: pytest.fail('派发中断及重启补证均不应查询旧 PID'))
    monkeypatch.setattr(observation, 'owned_process_snapshot',
        lambda *_arguments, **_keywords: pytest.fail('重启证据足够，不应扫描后代'))
    monkeypatch.setattr(observation.os, 'kill',
        lambda *_arguments, **_keywords: pytest.fail('恢复对账不能发送信号'))

    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run_directory, collection_directory)
    reconciliation_file = command_directory / 'descendant-exit-reconciliation.json'
    assert read_json(reconciliation_file)['status'] == 'unknown'

    current_boot[0] = 'current-native-boot'
    machine_evidence.ensure_previous_supervised_commands_exited(run_directory, collection_directory)
    reconciliation = read_json(reconciliation_file)
    assert reconciliation['status'] == 'exited'
    assert reconciliation['source'] == 'host_boot_changed'
    assert reconciliation['execution_token'] == 'interrupted-registered-execution'
    assert supervision_file.read_bytes() == original_supervision
    assert boot_observations == ['previous-native-boot', 'current-native-boot']

    original_reconciliation = reconciliation_file.read_bytes()
    machine_evidence.ensure_previous_supervised_commands_exited(run_directory, collection_directory)
    assert reconciliation_file.read_bytes() == original_reconciliation
    assert boot_observations == ['previous-native-boot', 'current-native-boot']


@pytest.mark.parametrize('registration_field_to_remove,invalid_execution_token', [
    ('process_ownership_scope', None), ('command_process_lifetime_contract', None),
    ('execution_token', None), (None, ''), (None, None),
])
def test_interrupted_launch_boot_reconciliation_preserves_registration_identity_gates(
        tmp_path, monkeypatch, registration_field_to_remove, invalid_execution_token):
    run_directory = tmp_path / 'unproven-interrupted-launch'
    collection_directory = run_directory / 'machine-evidence'
    command_directory = collection_directory / 'command-001'
    register_supervised_command_directory(run_directory, command_directory)
    facts = {
        'execution_token': 'interrupted-registered-execution',
        'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'host_boot_identity': 'previous-native-boot',
        'status': 'running',
        'command_launch_intent_recorded': True,
    }
    if registration_field_to_remove:
        del facts[registration_field_to_remove]
    else:
        facts['execution_token'] = invalid_execution_token
    write_json(command_directory / 'command-supervision.json', facts)
    monkeypatch.setattr(observation, 'native_host_boot_identity',
        lambda: pytest.fail('归属登记不完整时不能使用重启证据'))
    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run_directory, collection_directory)
    assert read_json(command_directory / 'descendant-exit-reconciliation.json')['status'] == 'unknown'


def write_bound_owner_exit_receipt(tmp_path):
    run = tmp_path / 'owner-receipt-run'
    repository = tmp_path / 'owner-repository'
    repository.mkdir()
    owner = tmp_path / 'owner-execution'
    owner.mkdir()
    execution_id = 'execution-1'
    nonce = '11111111-2222-4333-8444-555555555555'
    boot = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    revocation = owner / 'execution-revocation.json'
    collection = run / 'final-validation/attempt-001/commands'
    command_directory = collection / 'command-001'
    write_json(run / 'run.json', {
        'run_id': run.name, 'repo': str(repository),
        'command_execution_identity': {'execution_id': execution_id, 'revocation_file': str(revocation)},
        'command_execution_revocation_file': str(revocation),
    })
    write_json(run / 'kanban-command-execution-binding.json', {'task_id': 'task-1'})
    write_json(run / 'kanban-command-execution-start-receipt.json', {
        'returncode': 0,
        'stdout': json.dumps({'ok': True, 'execution': {
            'executionId': execution_id, 'taskId': 'task-1', 'revocationFile': str(revocation),
            'launchAttemptCount': 1, 'previousLaunchNonces': [], 'launchNonce': nonce,
        }}),
    })
    write_json(owner / f'launch-materials-{nonce}.json', {
        'directory': str(owner), 'executionId': execution_id, 'launchNonce': nonce,
        'revocationFile': str(revocation), 'workingDirectory': str(repository),
        'command': ['python', 'rvf', 'resume-task-review', '--run-dir', str(run)],
        'systemBootIdentity': boot.upper(), 'superviseProcessGroup': True,
        'linuxKernelChildSubreaperContainmentRequired': True,
    })
    write_json(owner / f'supervisor-identity-{nonce}.json', {
        'pid': 700, 'launchNonce': nonce, 'nativeProcessGenerationIdentity': 'linux:70',
    })
    write_json(owner / f'kernel-child-subreaper-containment-{nonce}.json', {
        'pid': 700, 'launchNonce': nonce, 'systemBootIdentity': boot,
        'pidNamespaceIdentity': 'pid:[4026531836]',
        'mode': 'linux_child_subreaper_established_before_script_dispatch',
        'nativeProcessGenerationIdentity': 'linux:70', 'verifiedAtEpochMs': 1000,
    })
    write_json(owner / f'supervisor-progress-{nonce}.json', {
        'launchNonce': nonce, 'status': 'exited', 'returncode': 0,
        'kernelChildExitObservation': 0, 'linuxKernelContainmentVerified': True,
        'linuxKernelContainmentContinuityLost': False, 'nativeOwnershipUnproven': False,
        'unprovenOwnershipProcesses': [], 'ownedProcesses': [
            {'pid': 800601, 'nativeProcessGenerationIdentity': 'linux:101'},
            {'pid': 800602, 'nativeProcessGenerationIdentity': 'linux:102'},
        ],
    })
    facts = {
        'run_directory': str(run), 'command_directory': str(command_directory),
        'execution_token': 'owner-receipt-command', 'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': '101'},
        'command_pid': 800602, 'command_native_identity': {'pid': 800602, 'start_identity': '102'},
        'command_returncode': 0, 'host_boot_identity': 'linux:' + boot,
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'status': 'descendant_exit_unproven', 'ownership_observation_coverage_status': 'unknown',
        'ownership_observation_gap_started_at': 123.5, 'owned_processes': [],
    }
    write_json(command_directory / 'command-supervision.json', facts)
    register_supervised_command_directory(run, command_directory)
    return run, collection, command_directory, owner, nonce


def test_closed_native_owner_receipt_recovers_historical_gap_idempotently(tmp_path, monkeypatch):
    run, collection, command_directory, owner, nonce = write_bound_owner_exit_receipt(tmp_path)
    supervision_file = command_directory / 'command-supervision.json'
    original_supervision = supervision_file.read_bytes()
    monkeypatch.setattr(observation, 'native_host_boot_identity',
        lambda: pytest.fail('可信回执不应依赖当前 boot 查询可用'))
    monkeypatch.setattr(observation, 'native_process_identity',
        lambda *_args: pytest.fail('封闭内核退出回执足够，不应查询历史 PID'))
    monkeypatch.setattr(observation, 'owned_process_snapshot',
        lambda *_args, **_kwargs: pytest.fail('封闭内核退出回执足够，不应扫描后代'))
    monkeypatch.setattr(observation.os, 'kill',
        lambda *_args: pytest.fail('只读退出对账不能发送信号'))

    machine_evidence.ensure_previous_supervised_commands_exited(run, collection)

    reconciliation_file = command_directory / 'descendant-exit-reconciliation.json'
    reconciliation = read_json(reconciliation_file)
    assert reconciliation['status'] == 'exited'
    assert reconciliation['source'] == 'linux_kernel_child_subreaper_exit_receipt'
    assert reconciliation['ownership_observation_coverage_status'] == 'unknown'
    assert reconciliation['ownership_observation_gap_started_at'] == 123.5
    assert reconciliation['owner_exit_receipt']['launch_nonce'] == nonce
    assert supervision_file.read_bytes() == original_supervision
    first = reconciliation_file.read_bytes()
    machine_evidence.ensure_previous_supervised_commands_exited(run, collection)
    assert reconciliation_file.read_bytes() == first
    assert not (owner / 'execution-revocation.json').exists()


@pytest.mark.parametrize('source_name,field,value', [
    ('launch-materials', 'linuxKernelChildSubreaperContainmentRequired', False),
    ('launch-materials', 'superviseProcessGroup', False),
    ('launch-materials', 'systemBootIdentity', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
    ('supervisor-identity', 'nativeProcessGenerationIdentity', 'linux:different'),
    ('kernel-child-subreaper-containment', 'mode', 'linux_child_subreaper_established_after_script_dispatch'),
    ('supervisor-progress', 'status', 'running'),
    ('supervisor-progress', 'kernelChildExitObservation', 1),
    ('supervisor-progress', 'linuxKernelContainmentContinuityLost', True),
    ('supervisor-progress', 'nativeOwnershipUnproven', True),
])
def test_unproven_owner_receipt_never_clears_historical_gap(
        tmp_path, monkeypatch, source_name, field, value):
    run, collection, command_directory, owner, nonce = write_bound_owner_exit_receipt(tmp_path)
    source = owner / f'{source_name}-{nonce}.json'
    write_json(source, {**read_json(source), field: value})
    supervision_file = command_directory / 'command-supervision.json'
    original_supervision = supervision_file.read_bytes()
    monkeypatch.setattr(observation, 'native_host_boot_identity',
        lambda: read_json(supervision_file)['host_boot_identity'])
    monkeypatch.setattr(observation, 'native_process_identity', lambda _pid: None)
    monkeypatch.setattr(observation, 'owned_process_snapshot', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(observation.os, 'kill',
        lambda *_args: pytest.fail('缺证恢复不能发送信号'))

    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run, collection)

    reconciliation = read_json(command_directory / 'descendant-exit-reconciliation.json')
    assert reconciliation['status'] == 'unknown'
    assert reconciliation['ownership_observation_coverage_status'] == 'unknown'
    assert reconciliation['ownership_observation_gap_started_at'] == 123.5
    assert reconciliation['owner_exit_receipt']['status'] == 'unknown'
    assert reconciliation['owner_exit_receipt']['reason']
    assert supervision_file.read_bytes() == original_supervision


def test_owner_receipt_rejects_duplicate_inner_process_identity(tmp_path, monkeypatch):
    run, collection, command_directory, owner, nonce = write_bound_owner_exit_receipt(tmp_path)
    progress_file = owner / f'supervisor-progress-{nonce}.json'
    progress = read_json(progress_file)
    progress['ownedProcesses'].append(progress['ownedProcesses'][0])
    write_json(progress_file, progress)
    monkeypatch.setattr(observation, 'native_host_boot_identity',
        lambda: read_json(command_directory / 'command-supervision.json')['host_boot_identity'])
    monkeypatch.setattr(observation, 'native_process_identity', lambda _pid: None)
    monkeypatch.setattr(observation, 'owned_process_snapshot', lambda *_args, **_kwargs: {})

    with pytest.raises(machine_evidence.WorkspacePreparationFailed):
        machine_evidence.ensure_previous_supervised_commands_exited(run, collection)

    reconciliation = read_json(command_directory / 'descendant-exit-reconciliation.json')
    assert reconciliation['status'] == 'unknown'
    assert '未唯一绑定' in reconciliation['owner_exit_receipt']['reason']


@pytest.mark.parametrize('missing_evidence', ['supervisor_pid', 'command_pid', 'task_identity', 'platform', 'owner_generation'])
def test_owner_receipt_rejects_incomplete_dispatch_binding(tmp_path, missing_evidence):
    from core.task_snapshot_review.registered_command_owner_exit_receipt import observe_registered_command_owner_exit_receipt
    run, _collection, command_directory, owner, nonce = write_bound_owner_exit_receipt(tmp_path)
    facts = read_json(command_directory / 'command-supervision.json')
    if missing_evidence in {'supervisor_pid', 'command_pid'}:
        facts.pop(missing_evidence)
    elif missing_evidence == 'platform':
        facts['host_boot_identity'] = facts['host_boot_identity'].replace('linux:', 'darwin:')
    elif missing_evidence == 'task_identity':
        write_json(run / 'kanban-command-execution-binding.json', {})
        receipt_file = run / 'kanban-command-execution-start-receipt.json'
        receipt = read_json(receipt_file)
        payload = json.loads(receipt['stdout'])
        payload['execution'].pop('taskId')
        receipt['stdout'] = json.dumps(payload)
        write_json(receipt_file, receipt)
    else:
        for kind in ('supervisor-identity', 'kernel-child-subreaper-containment'):
            path = owner / f'{kind}-{nonce}.json'
            write_json(path, {**read_json(path), 'nativeProcessGenerationIdentity': 'invalid-generation'})
    result = observe_registered_command_owner_exit_receipt(run, facts)
    assert result['status'] == 'unknown'
    assert result['reason']


def test_owner_control_directory_can_have_a_workspace_named_ancestor(tmp_path):
    from core.task_snapshot_review.registered_command_owner_exit_receipt import observe_registered_command_owner_exit_receipt
    parent = tmp_path / 'workspace'
    parent.mkdir()
    run, _collection, command_directory, _owner, _nonce = write_bound_owner_exit_receipt(parent)
    assert observe_registered_command_owner_exit_receipt(run,
        read_json(command_directory / 'command-supervision.json'))['status'] == 'exited'


def test_closed_owner_receipt_recovers_role_cleanup_without_signals(tmp_path, monkeypatch):
    from core.task_snapshot_review import role_owned_process_cleanup as role_cleanup
    run, _collection, command_directory, _owner, _nonce = write_bound_owner_exit_receipt(tmp_path)
    attempt = run / 'reviewer-1/attempt-001'
    facts = {**read_json(command_directory / 'command-supervision.json'),
        'command_directory': str(attempt),
        'command_process_lifetime_contract': observation.ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT}
    write_json(attempt / 'command-supervision.json', facts)
    monkeypatch.setattr(role_cleanup, 'native_host_boot_identity', lambda: facts['host_boot_identity'])
    monkeypatch.setattr(role_cleanup, 'native_process_identity',
        lambda *_args: pytest.fail('封闭内核退出回执足够，不应查询角色历史 PID'))
    monkeypatch.setattr(role_cleanup.os, 'kill',
        lambda *_args: pytest.fail('等价退出证明不能触发角色资源信号'))

    cleanup = role_cleanup.cleanup_role_owned_processes(attempt)

    assert cleanup['status'] == 'exited'
    assert cleanup['source'] == 'linux_kernel_child_subreaper_exit_receipt'
    assert cleanup['ownership_observation_coverage_status'] == 'unknown'
    assert cleanup['ownership_observation_gap_started_at'] == 123.5
    assert role_cleanup.cleanup_role_owned_processes(attempt) == cleanup
