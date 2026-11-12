"""进程观测恢复不伪造覆盖，历史登记与心跳不得放大查询及写盘。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest

from core.task_snapshot_review import execution_lifecycle, machine_evidence, retained_command_supervisor
from core.task_snapshot_review.native_owned_process_observation import (
    REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
    native_process_identity, native_process_start_identity_matches,
)
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


@pytest.fixture
def pending_registered_role_recovery_observation(tmp_path, monkeypatch):
    from core.task_snapshot_review import review_recovery_evidence_observation as recovery
    run = tmp_path / 'pending-role-readonly-recovery'
    attempt = run / 'reviewer-1/attempt-001'
    supervisor_identity = {'pid': 800601, 'start_identity': [100, 0], 'unique_process_id': 100}
    command_identity = {'pid': 800602, 'start_identity': [101, 0], 'unique_process_id': 101}
    facts = {'execution_token': 'pending-role-recovery-token', 'command_directory': str(attempt.resolve()),
        'command_process_lifetime_contract': ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'host_boot_identity': 'registered-host-boot', 'status': 'running',
        'ownership_observation_coverage_status': 'complete',
        'supervisor_pid': supervisor_identity['pid'], 'supervisor_native_identity': supervisor_identity,
        'command_pid': command_identity['pid'], 'command_native_identity': command_identity}
    write_json(run / 'run.json', {key + '_sha256': 'frozen-input-digest'
        for key in ('requirements', 'patch', 'review_input', 'decision_facts')})
    write_json(attempt / 'command-supervision.json', facts)
    write_json(attempt / 'outcome.json', {'status': 'unknown', 'cli_exit_proven': False})
    execution_lifecycle.register_supervised_command_directory(run, attempt)
    monkeypatch.setattr(recovery, 'verify_repository_identity', lambda record: None)
    monkeypatch.setattr(recovery, 'verified_execution_plan', lambda *args: {'fix': True})
    monkeypatch.setattr(recovery, 'machine_evidence_request_binding', lambda *args: {'run_id': 'pending-role-recovery'})
    monkeypatch.setattr(recovery, 'registered_host_recovery_inputs', lambda *args: {})
    monkeypatch.setattr(recovery, 'file_sha256', lambda path: 'frozen-input-digest')
    return recovery, run, attempt, facts


@pytest.mark.parametrize('exited_process', ['supervisor', 'command'])
def test_pending_cleanup_readonly_recovery_evidence_changes_when_registered_process_exits(
        pending_registered_role_recovery_observation, monkeypatch, exited_process):
    recovery, run, attempt, facts = pending_registered_role_recovery_observation
    supervisor_identity = facts['supervisor_native_identity']
    command_identity = facts['command_native_identity']
    current_identities = {supervisor_identity['pid']: supervisor_identity if exited_process == 'supervisor' else None,
        command_identity['pid']: command_identity}
    identity_queries = Mock(side_effect=lambda pid: current_identities[pid])
    monkeypatch.setattr(recovery, 'native_host_boot_identity', lambda: facts['host_boot_identity'])
    monkeypatch.setattr(recovery, 'native_process_identity', identity_queries)
    signal_request = Mock()
    monkeypatch.setattr(os, 'kill', signal_request)
    monkeypatch.setattr(os, 'killpg', signal_request)
    original_path_open = Path.open
    def readonly_path_open(path, mode='r', *args, **kwargs):
        assert not any(marker in mode for marker in ('w', 'a', 'x', '+')), '只读恢复观测不能打开写入文件'
        return original_path_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', readonly_path_open)
    originals = {path: path.read_bytes() for path in run.rglob('*') if path.is_file()}

    live = recovery.observe_task_review_recovery_evidence(run)
    assert live['status'] == 'known'
    assert recovery.observe_task_review_recovery_evidence(run) == live
    current_identities[facts[exited_process + '_native_identity']['pid']] = None
    exited = recovery.observe_task_review_recovery_evidence(run)

    assert exited['status'] == 'known'
    assert exited['evidenceSha256'] != live['evidenceSha256']
    assert recovery.observe_task_review_recovery_evidence(run) == exited
    assert [call.args[0] for call in identity_queries.call_args_list] == [supervisor_identity['pid'], command_identity['pid']] * 4
    assert originals == {path: path.read_bytes() for path in run.rglob('*') if path.is_file()}
    signal_request.assert_not_called()


@pytest.mark.parametrize('invalid_identity', [None, {'start_identity': [100, 0]},
    {'pid': 800601}, {'pid': 800604, 'start_identity': [100, 0]},
    {'pid': 2**32 + 800601, 'start_identity': [100, 0]}])
def test_pending_cleanup_readonly_recovery_skips_missing_or_invalid_registered_identities(
        pending_registered_role_recovery_observation, monkeypatch, invalid_identity):
    recovery, run, attempt, facts = pending_registered_role_recovery_observation
    write_json(attempt / 'command-supervision.json', {**facts,
        'supervisor_native_identity': invalid_identity, 'command_native_identity': invalid_identity})
    identity_queries = Mock()
    monkeypatch.setattr(recovery, 'native_host_boot_identity', lambda: facts['host_boot_identity'])
    monkeypatch.setattr(recovery, 'native_process_identity', identity_queries)

    first = recovery.observe_task_review_recovery_evidence(run)

    assert first['status'] == 'known'
    assert recovery.observe_task_review_recovery_evidence(run) == first
    identity_queries.assert_not_called()


def test_pending_cleanup_readonly_recovery_detects_boot_change_without_querying_old_process_ids(
        pending_registered_role_recovery_observation, monkeypatch):
    recovery, run, attempt, facts = pending_registered_role_recovery_observation
    current_boot = [facts['host_boot_identity']]
    identity_queries = Mock(side_effect=lambda pid: facts['supervisor_native_identity']
        if pid == facts['supervisor_pid'] else facts['command_native_identity'])
    monkeypatch.setattr(recovery, 'native_host_boot_identity', lambda: current_boot[0])
    monkeypatch.setattr(recovery, 'native_process_identity', identity_queries)
    first = recovery.observe_task_review_recovery_evidence(run)
    identity_queries.reset_mock()
    current_boot[0] = 'current-host-boot'

    changed = recovery.observe_task_review_recovery_evidence(run)

    assert changed['status'] == 'known'
    assert changed['evidenceSha256'] != first['evidenceSha256']
    assert recovery.observe_task_review_recovery_evidence(run) == changed
    identity_queries.assert_not_called()


@pytest.mark.parametrize('boot_uuid', [b'12345678-1234-5678-1234-567812345678', b''])
def test_darwin_boot_identity_uses_native_boot_session_uuid(tmp_path, monkeypatch, boot_uuid):
    import ctypes
    from types import SimpleNamespace
    from core.task_snapshot_review import native_owned_process_observation as observation
    def sysctlbyname(name, output, *args):
        assert name == b'kern.bootsessionuuid'
        ctypes.memmove(output, boot_uuid + b'\0', len(boot_uuid) + 1)
        return 0
    monkeypatch.setattr(observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(sysctlbyname=sysctlbyname))
    if boot_uuid:
        assert observation.native_host_boot_identity.__wrapped__() == 'darwin:' + boot_uuid.decode()
    else:
        with pytest.raises(RuntimeError, match='UUID'):
            observation.native_host_boot_identity.__wrapped__()


def test_descendant_observation_retires_history_without_repeated_ps(tmp_path, monkeypatch):
    root = {'pid': 800601, 'start_identity': [100, 0]}
    child = {'pid': 800602, 'start_identity': [101, 0]}
    history = [{'pid': 800700 + index, 'native_identity': {'pid': 800700 + index, 'start_identity': [99, 0]},
        'ownership_root_pid': root['pid'], 'is_active_registration': False} for index in range(800)]
    write_json(tmp_path / 'owned-processes.json', {'processes': [*history, {
        'pid': root['pid'], 'native_identity': root, 'is_active_registration': True, 'owns_process_group': True}]})
    observed_previous = []
    def scoped_snapshot(pid, previous, **kwargs):
        assert pid == root['pid']
        observed_previous.append(dict(previous))
        return {child['pid']: child} if len(observed_previous) < 3 else {}
    monkeypatch.setattr(execution_lifecycle, 'owned_process_snapshot', scoped_snapshot)
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: pytest.fail('后代原生身份已足够，不应另起 ps'))
    state = {}
    for _ in range(4):
        assert execution_lifecycle.observe_owned_process_group_descendants(tmp_path, root['pid'], state)
    assert observed_previous == [{}, {child['pid']: child}, {child['pid']: child}, {}]
    records = read_json(tmp_path / 'owned-processes.json')['processes']
    assert len(records) == 802
    assert next(item for item in records if item['pid'] == child['pid'])['is_active_registration'] is False


@pytest.mark.parametrize('lifetime_contract', [REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT])
def test_transient_native_query_outage_recovers_while_cli_runs_but_preserves_coverage_gap(tmp_path, lifetime_contract):
    run = tmp_path / 'isolated-query-outage'
    artifacts = run / 'commands/command-001'
    write_json(run / 'run.json', {})
    execution_lifecycle.register_supervised_command_directory(run, artifacts)
    marker = tmp_path / 'service-native-identity.json'
    observation_times = tmp_path / 'native-observation-times.json'
    root = Path(__file__).resolve().parents[1]
    service = (f'import sys; sys.path.insert(0,{str(root)!r})\n'
        'import json,os,time\nfrom pathlib import Path\n'
        'from core.task_snapshot_review.native_owned_process_observation import native_process_identity\n'
        f'Path({str(marker)!r}).write_text(json.dumps(native_process_identity(os.getpid())))\n'
        + ('time.sleep(30)' if lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT else 'time.sleep(0.4)'))
    command = [sys.executable, '-B', '-c',
        f'import subprocess,sys,time; time.sleep(0.3); subprocess.Popen([sys.executable,"-B","-c",{service!r}],start_new_session=True); time.sleep(1.5)']
    driver = ('import json,time\nfrom pathlib import Path\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'original=supervisor.owned_process_snapshot\ncalls=[]\nstarted=time.monotonic()\n'
        'def observe(*args,**kwargs):\n'
        ' calls.append(time.monotonic()-started)\n'
        f' Path({str(observation_times)!r}).write_text(json.dumps(calls))\n'
        ' if 2<=len(calls)<=4: raise RuntimeError("隔离模拟一次短暂的原生查询故障")\n'
        ' return original(*args,**kwargs)\n'
        'supervisor.owned_process_snapshot=observe\n'
        f'returncode=supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),(),{command!r},command_process_lifetime_contract={lifetime_contract!r})\n'
        f'Path({str(observation_times)!r}).write_text(json.dumps(calls))\n'
        'raise SystemExit(returncode)\n')
    process = subprocess.Popen([sys.executable, '-B', '-c', driver], start_new_session=True)
    try:
        process.wait(timeout=8)
        facts = read_json(artifacts / 'command-supervision.json')
        assert facts['command_returncode'] == 0
        assert facts['ownership_observation_coverage_status'] == 'unknown'
        assert facts['status'] == 'descendant_exit_unproven'
        times = json.loads(observation_times.read_text())
        assert any(0.8 < elapsed < 1.8 for elapsed in times[4:]), '须在 CLI 存活期间自动恢复查询'
        if lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
            assert process.returncode == 0
            assert read_json(artifacts / 'role-owned-process-cleanup.json')['status'] == 'unknown'
            identity = read_json(marker)
            assert not native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
        else:
            original_facts = (artifacts / 'command-supervision.json').read_bytes()
            with pytest.raises(machine_evidence.WorkspacePreparationFailed):
                machine_evidence.ensure_previous_supervised_commands_exited(run, run / 'commands')
            assert read_json(artifacts / 'descendant-exit-reconciliation.json')['status'] == 'unknown'
            assert (artifacts / 'command-supervision.json').read_bytes() == original_facts
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if marker.exists():
            identity = read_json(marker)
            if native_process_start_identity_matches(native_process_identity(identity['pid']), identity):
                os.kill(identity['pid'], signal.SIGKILL)


def test_idle_supervised_command_checkpoint_writes_are_bounded(tmp_path):
    run = tmp_path / 'isolated-checkpoint-budget'
    artifacts = run / 'commands/command-001'
    write_json(run / 'run.json', {})
    count_file = tmp_path / 'checkpoint-write-count.json'
    command = [sys.executable, '-B', '-c', 'import time; time.sleep(1.5)']
    driver = ('import json\nfrom pathlib import Path\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'original=supervisor.write_json\nwrites=0\n'
        'def checkpoint(path,value):\n'
        ' global writes\n'
        ' if path.name=="command-supervision.json": writes+=1\n'
        ' original(path,value)\n'
        'supervisor.write_json=checkpoint\n'
        f'returncode=supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),(),{command!r})\n'
        f'Path({str(count_file)!r}).write_text(json.dumps({{"writes":writes}}))\n'
        'raise SystemExit(returncode)\n')
    process = subprocess.Popen([sys.executable, '-B', '-c', driver], start_new_session=True)
    try:
        assert process.wait(timeout=5) == 0
        assert read_json(count_file)['writes'] <= 10
        assert read_json(artifacts / 'command-supervision.json')['owned_process_exit_status'] == 'exited'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
