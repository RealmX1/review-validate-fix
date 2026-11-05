"""Stop 消费追加对账身份，查询失败时仍清理已证明所属的进程。"""
import signal
from unittest.mock import patch

import pytest

from core.task_snapshot_review import execution_lifecycle, native_owned_process_observation as observation
from core.task_snapshot_review import retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import write_json
from test_owned_command_process_scope import darwin_owned_process_queries


@pytest.mark.parametrize('child_state', ['live', 'pid_reused', 'identity_unavailable'])
@pytest.mark.parametrize('scoped_query_fails', [False, True])
@pytest.mark.parametrize('start_format', ['darwin', 'linux'])
def test_stop_cancels_reconciled_child_with_identity_and_revocation_guards(tmp_path, child_state, scoped_query_fails, start_format):
    run = tmp_path / 'reconciled-child-cancellation'
    command = run / 'commands/command-001'
    write_json(run / 'run.json', {})
    execution_lifecycle.register_supervised_command_directory(run, command)
    parent = {'pid': 800602, 'start_identity': [101, 0] if start_format == 'darwin' else '101'}
    child = {'pid': 800603, 'start_identity': [102, 0] if start_format == 'darwin' else '102'}
    supervision_file = command / 'command-supervision.json'
    reconciliation_file = command / 'descendant-exit-reconciliation.json'
    write_json(supervision_file, {'execution_token': 'controlled-execution', 'supervisor_pid': 800601,
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'command_pid': parent['pid'], 'command_native_identity': parent, 'owned_processes': [parent]})
    write_json(reconciliation_file, {'status': 'pending', 'owned_processes': [child]})
    originals = {path: path.read_bytes() for path in (supervision_file, reconciliation_file)}
    signals = []

    def scoped_snapshot(supervisor_pid, recorded, **kwargs):
        if scoped_query_fails:
            raise RuntimeError('所属子链查询暂不可用')
        return {pid: value for pid, value in recorded.items() if pid == child['pid']}

    def current_identity(pid):
        if pid != child['pid']:
            return None
        if child_state == 'identity_unavailable':
            raise RuntimeError('已登记子进程身份查询暂不可用')
        reused_start = [202, 0] if start_format == 'darwin' else '202'
        return {**child, 'start_identity': reused_start} if child_state == 'pid_reused' else child

    def record_signal(pid, requested_signal):
        assert (run / 'execution-revocation.json').exists()
        signals.append((pid, requested_signal))

    with patch.object(retained_command_supervisor, 'owned_process_snapshot', side_effect=scoped_snapshot), \
            patch.object(observation, 'native_process_identity', side_effect=current_identity), \
            patch.object(observation.os, 'kill', side_effect=record_signal):
        result = execution_lifecycle.revoke_execution(run, '取消只在追加对账中登记的后代')
    expected = {'live': 'termination_requested', 'pid_reused': 'identity_changed_no_signal', 'identity_unavailable': 'unknown'}
    child_observations = [item for item in result['supervised_descendants'] if item.get('pid') == child['pid']]
    assert len(child_observations) == 1
    assert child_observations[0]['cancellation_status'] == expected[child_state]
    assert signals == ([(child['pid'], signal.SIGKILL)] if child_state == 'live' else [])
    assert all(path.read_bytes() == original for path, original in originals.items())


@pytest.mark.parametrize('reconciliation_content', ['{invalid-json', '{"owned_processes": null}', '{"owned_processes": {}}'])
def test_malformed_reconciliation_does_not_prevent_registered_process_cancellation(tmp_path, reconciliation_content):
    command = tmp_path / 'commands/command-001'
    command.mkdir(parents=True)
    child = {'pid': 800603, 'start_identity': [102, 0]}
    write_json(command / 'command-supervision.json', {'execution_token': 'controlled-execution',
        'supervisor_pid': 800601, 'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'owned_processes': [child]})
    (command / 'descendant-exit-reconciliation.json').write_text(reconciliation_content)
    signals = []
    with patch.object(observation, 'native_process_identity', return_value=child), \
            patch.object(observation.os, 'kill', side_effect=lambda *args: signals.append(args)), \
            patch.object(retained_command_supervisor, 'owned_process_snapshot', side_effect=RuntimeError('限定发现不可用')):
        result = retained_command_supervisor.terminate_supervised_command_descendants(command)
    assert signals == [(child['pid'], signal.SIGKILL)]
    assert result[0]['cancellation_status'] == 'termination_requested'
    assert result[-1]['cancellation_status'] == 'unknown'


@pytest.mark.parametrize('bad_identity', [
    {'pid': 800602},
    {'pid': 800602, 'start_identity': None},
    {'pid': 800602, 'start_identity': []},
    {'pid': 800602, 'start_identity': [101]},
    {'pid': 800602, 'start_identity': [True, 0]},
    {'pid': 800602, 'start_identity': {}},
    {'pid': 800602, 'start_identity': 'not-a-start-time'},
    {'pid': [800602], 'start_identity': [101, 0]},
    None,
])
@pytest.mark.parametrize('scoped_query_fails', [False, True])
def test_invalid_reconciled_identity_preserves_registered_and_later_valid_processes(
        tmp_path, monkeypatch, darwin_owned_process_queries, bad_identity, scoped_query_fails):
    state = darwin_owned_process_queries
    del state.processes[state.supervisor_pid]
    state.processes[800604] = (1, 800604, 104)
    run = tmp_path / 'invalid-reconciled-identity'
    command = run / 'commands/command-001'
    execution_lifecycle.register_supervised_command_directory(run, command)
    supervision_file = command / 'command-supervision.json'
    reconciliation_file = command / 'descendant-exit-reconciliation.json'
    write_json(supervision_file, {'execution_token': 'controlled-execution', 'supervisor_pid': state.supervisor_pid,
        'supervisor_native_identity': state.identity, 'owned_processes': [
            {'pid': state.child_pid, 'start_identity': [101, 0]},
            {'pid': state.orphan_pid, 'start_identity': [102, 0]}]})
    write_json(reconciliation_file, {'status': 'unknown', 'owned_processes': [
        bad_identity, {'pid': 800604, 'start_identity': [104, 0]}]})
    originals = {path: path.read_bytes() for path in (supervision_file, reconciliation_file)}
    if scoped_query_fails:
        def unavailable_scoped_query(*args):
            raise RuntimeError('所属子链查询暂不可用')
        monkeypatch.setattr(observation, 'darwin_scoped_process_ids', unavailable_scoped_query)
    signals = []
    def record_signal(pid, requested_signal):
        assert (run / 'execution-revocation.json').exists()
        signals.append((pid, requested_signal))
    monkeypatch.setattr(observation.os, 'kill', record_signal)
    result = execution_lifecycle.revoke_execution(run, '损坏补证不应阻止清理有效身份')
    assert set(signals) == {(pid, signal.SIGKILL) for pid in (state.child_pid, state.orphan_pid, 800604)}
    assert any(item['cancellation_status'] == 'unknown' for item in result['supervised_descendants'])
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert state.unrelated_pid not in state.inspected


@pytest.mark.parametrize('registered_start,reconciled_start,current_start,expected_status', [
    (101, 100, 101, 'termination_requested'),
    (100, 101, 101, 'termination_requested'),
    (100, 101, 103, 'identity_changed_no_signal'),
    (100, 101, None, 'exited'),
    (100, 101, 'unavailable', 'unknown'),
])
@pytest.mark.parametrize('scoped_query_fails', [False, True])
def test_conflicting_reconciled_identity_uses_current_proof_without_losing_other_processes(
        tmp_path, monkeypatch, darwin_owned_process_queries,
        registered_start, reconciled_start, current_start, expected_status, scoped_query_fails):
    state = darwin_owned_process_queries
    del state.processes[state.supervisor_pid]
    if current_start is None:
        del state.processes[state.child_pid]
    elif current_start == 'unavailable':
        state.denied.add(state.child_pid)
    else:
        state.processes[state.child_pid] = (1, state.child_pid, current_start)
    run = tmp_path / 'conflicting-reconciled-identity'
    command = run / 'commands/command-001'
    execution_lifecycle.register_supervised_command_directory(run, command)
    supervision_file = command / 'command-supervision.json'
    reconciliation_file = command / 'descendant-exit-reconciliation.json'
    write_json(supervision_file, {'execution_token': 'controlled-execution', 'supervisor_pid': state.supervisor_pid,
        'supervisor_native_identity': state.identity, 'owned_processes': [
            {'pid': state.child_pid, 'start_identity': [registered_start, 0]},
            {'pid': state.orphan_pid, 'start_identity': [102, 0]}]})
    write_json(reconciliation_file, {'status': 'unknown', 'owned_processes': [
        {'pid': state.child_pid, 'start_identity': [reconciled_start, 0]}]})
    originals = {path: path.read_bytes() for path in (supervision_file, reconciliation_file)}
    if scoped_query_fails:
        def unavailable_scoped_query(*args):
            raise RuntimeError('所属子链查询暂不可用')
        monkeypatch.setattr(observation, 'darwin_scoped_process_ids', unavailable_scoped_query)
    signals = []
    def record_signal(pid, requested_signal):
        assert (run / 'execution-revocation.json').exists()
        signals.append((pid, requested_signal))
    monkeypatch.setattr(observation.os, 'kill', record_signal)
    result = execution_lifecycle.revoke_execution(run, '按当前原生身份解决同 PID 补证冲突')
    child_observations = [item for item in result['supervised_descendants'] if item.get('pid') == state.child_pid]
    assert [item['cancellation_status'] for item in child_observations] == [expected_status]
    expected_pids = {state.orphan_pid}
    if expected_status == 'termination_requested':
        expected_pids.add(state.child_pid)
    assert set(signals) == {(pid, signal.SIGKILL) for pid in expected_pids}
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert state.unrelated_pid not in state.inspected


@pytest.mark.parametrize('reconciled_pid,valid', [
    (2**31 - 1, True),
    (2**31, False),
    (2**32 + 800777, False),
])
def test_reconciled_pid_must_fit_signed_pid_t_before_any_native_query(
        tmp_path, monkeypatch, darwin_owned_process_queries, reconciled_pid, valid):
    state = darwin_owned_process_queries
    del state.processes[state.supervisor_pid]
    if valid:
        state.processes[reconciled_pid] = (1, reconciled_pid, 103)
    run = tmp_path / 'reconciled-pid-bounds'
    command = run / 'commands/command-001'
    execution_lifecycle.register_supervised_command_directory(run, command)
    supervision_file = command / 'command-supervision.json'
    reconciliation_file = command / 'descendant-exit-reconciliation.json'
    write_json(supervision_file, {'execution_token': 'controlled-execution', 'supervisor_pid': state.supervisor_pid,
        'supervisor_native_identity': state.identity,
        'owned_processes': [{'pid': state.child_pid, 'start_identity': [101, 0]}]})
    write_json(reconciliation_file, {'status': 'unknown', 'owned_processes': [
        {'pid': reconciled_pid, 'start_identity': [103, 0]},
        {'pid': state.orphan_pid, 'start_identity': [102, 0]}]})
    originals = {path: path.read_bytes() for path in (supervision_file, reconciliation_file)}
    signals = []
    def record_signal(pid, requested_signal):
        assert (run / 'execution-revocation.json').exists()
        signals.append((pid, requested_signal))
    monkeypatch.setattr(observation.os, 'kill', record_signal)
    result = execution_lifecycle.revoke_execution(run, '补证 PID 必须在原生有符号 pid_t 范围内')
    expected_pids = {state.child_pid, state.orphan_pid}
    if valid:
        expected_pids.add(reconciled_pid)
    assert set(signals) == {(pid, signal.SIGKILL) for pid in expected_pids}
    assert set(state.inspected) <= expected_pids | {state.supervisor_pid}
    if not valid:
        assert any(item['cancellation_status'] == 'unknown' for item in result['supervised_descendants'])
        assert all(item.get('pid') != reconciled_pid for item in result['supervised_descendants'])
    assert all(path.read_bytes() == original for path, original in originals.items())
