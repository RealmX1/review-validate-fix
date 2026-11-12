"""角色业务结果与所属后台资源的独立完成、回收和恢复验收。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import execution_lifecycle, role_process_execution, workflow
from core.task_snapshot_review.native_owned_process_observation import native_process_identity, native_process_start_identity_matches
from core.task_snapshot_review.persistent_artifacts import read_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def create_role_with_detached_service(repository, directory, *, returncode=0, wait_for_release=False,
                                       output_format='text', role='reviewer', ignore_service_sigterm=False):
    run = directory / 'isolated-review'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'owned resources', run_directory=run)
    marker = directory / 'service-native-identity.json'
    release = directory / 'release-role-cli'
    service = (
        f'import sys; sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n'
        'import json,os,signal,time\nfrom pathlib import Path\n'
        'from core.task_snapshot_review.native_owned_process_observation import native_process_identity\n'
        + ('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if ignore_service_sigterm else '')
        +
        f'Path({str(marker)!r}).write_text(json.dumps(native_process_identity(os.getpid())))\n'
        'time.sleep(30)\n'
    )
    payload = ({'status': 'completed', 'audit_summary': '已完成审计', 'findings': []} if role == 'reviewer'
        else {'status': 'completed', 'dispositions': [], 'new_findings': []})
    output = f'print(json.dumps({payload!r}))\n'
    if output_format == 'kimi_stream_json':
        output = (f'print(json.dumps({{"role":"assistant","content":json.dumps({payload!r})}}))\n'
            + 'print(json.dumps({"role":"meta","type":"session.resume_hint","session_id":"isolated-role-session"}))\n')
    elif output_format == 'codex_json':
        output = (f'print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":json.dumps({payload!r})}}}}))\n'
            + 'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))\n')
    program = ('import json,subprocess,sys,time\nfrom pathlib import Path\n'
        f'subprocess.Popen([sys.executable,"-B","-c",{service!r}],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'while not Path({str(marker)!r}).exists(): time.sleep(0.01)\n'
        + (f'while not Path({str(release)!r}).exists(): time.sleep(0.01)\n' if wait_for_release else 'time.sleep(0.4)\n')
        + output
        + f'raise SystemExit({returncode})\n')
    config = {'command': [sys.executable, '-B', '-c', program], 'output_format': output_format}
    role_directory = run / ('reviewer-1' if role == 'reviewer' else 'fixer')
    _, worker = workflow._start_role(run, read_json(run / 'run.json'), role_directory, role, [config], [])
    return run, role_directory / 'attempt-001', marker, release, worker


def safely_clean_service(marker):
    if marker.exists():
        identity = read_json(marker)
        if native_process_start_identity_matches(native_process_identity(identity['pid']), identity):
            os.kill(identity['pid'], signal.SIGKILL)
            wait_until(lambda: not native_process_start_identity_matches(native_process_identity(identity['pid']), identity))


@pytest.mark.parametrize('returncode', [0, 2])
@pytest.mark.parametrize('output_format', ['kimi_stream_json', 'codex_json'])
@pytest.mark.parametrize('role', ['reviewer', 'fixer'])
def test_role_cli_exit_reclaims_detached_service_without_changing_business_result(repository, tmp_path, returncode, output_format, role):
    run, attempt, marker, _, worker = create_role_with_detached_service(repository, tmp_path,
        returncode=returncode, output_format=output_format, role=role)
    try:
        assert worker.wait(timeout=15) == 0
        outcome = read_json(attempt / 'outcome.json')
        assert outcome['status'] == ('completed' if returncode == 0 else 'failed')
        assert outcome['returncode'] == returncode
        identity = read_json(marker)
        assert not native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
        assert outcome['role_owned_process_cleanup_status'] == 'exited'
        assert read_json(attempt / 'role-owned-process-cleanup.json')['status'] == 'exited'
        if returncode == 0:
            assert workflow._role_result(attempt.parent)['result']['status'] == 'completed'
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        safely_clean_service(marker)


def test_worker_crash_keeps_single_model_invocation_and_supervisor_reclaims_service(repository, tmp_path):
    run, attempt, marker, release, worker = create_role_with_detached_service(repository, tmp_path,
        wait_for_release=True, output_format='kimi_stream_json')
    try:
        wait_until(marker.exists)
        worker.kill()
        worker.wait(timeout=5)
        assert role_process_execution.process_lock_is_held(attempt)
        assert workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [], []) == (None, None)
        release.touch()
        wait_until(lambda: not role_process_execution.process_lock_is_held(attempt))
        identity = read_json(marker)
        assert not native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
        assert read_json(attempt / 'role-owned-process-cleanup.json')['status'] == 'exited'
        assert workflow._role_result(attempt.parent)['result']['status'] == 'completed'
        assert len(list((run / 'reviewer-1').glob('attempt-*'))) == 1
    finally:
        release.touch()
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        safely_clean_service(marker)


def test_explicit_stop_cleans_role_service_and_preserves_cancelled_result(repository, tmp_path):
    run, attempt, marker, release, worker = create_role_with_detached_service(repository, tmp_path, wait_for_release=True)
    try:
        wait_until(marker.exists)
        execution_lifecycle.revoke_execution(run, '自动化验证明确 Stop')
        worker.wait(timeout=12)
        identity = read_json(marker)
        wait_until(lambda: not native_process_start_identity_matches(native_process_identity(identity['pid']), identity))
        assert read_json(attempt / 'role-owned-process-cleanup.json')['status'] == 'exited'
        assert read_json(attempt / 'outcome.json')['status'] == 'cancelled'
        assert not (attempt / 'result.json').exists()
    finally:
        release.touch()
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        safely_clean_service(marker)


@pytest.mark.parametrize('identity_change', ['start_identity', 'unique_process_id'])
def test_cleanup_recovery_preserves_observation_gap_and_does_not_signal_reused_pid(tmp_path, monkeypatch, identity_change):
    from core.task_snapshot_review import role_owned_process_cleanup as cleanup
    from core.task_snapshot_review.native_owned_process_observation import (
        REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
    )
    from core.task_snapshot_review.persistent_artifacts import write_json
    attempt = tmp_path / 'reviewer-1/attempt-001'
    original_identity = {'pid': 800603, 'start_identity': [101, 0], 'unique_process_id': 101}
    facts = {'execution_token': 'fixed-role-attempt-token', 'host_boot_identity': 'fixed-boot',
        'command_directory': str(attempt.resolve()),
        'supervisor_pid': 800601, 'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'status': 'descendant_exit_unproven', 'ownership_observation_coverage_status': 'unknown',
        'command_process_lifetime_contract': ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'process_ownership_scope': REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'owned_processes': [original_identity]}
    write_json(attempt / 'command-supervision.json', facts)
    monkeypatch.setattr(cleanup, 'native_host_boot_identity', lambda: 'fixed-boot')
    changed_identity = {**original_identity, **({identity_change: [102, 0]} if identity_change == 'start_identity' else {identity_change: 202})}
    monkeypatch.setattr(cleanup, 'native_process_identity', lambda pid: changed_identity if pid == 800603 else None)
    monkeypatch.setattr(cleanup, 'owned_process_snapshot', lambda *args, **kwargs: {800603: original_identity})
    signals = []
    monkeypatch.setattr(cleanup.os, 'kill', lambda *args: signals.append(args))
    first = cleanup.cleanup_role_owned_processes(attempt)
    assert first['status'] == 'unknown'
    assert signals == []
    assert cleanup.cleanup_role_owned_processes(attempt) == first
    monkeypatch.setattr(cleanup, 'native_host_boot_identity', lambda: 'new-host-boot')
    assert cleanup.cleanup_role_owned_processes(attempt)['status'] == 'exited'
    assert signals == []
    assert read_json(attempt / 'command-supervision.json') == facts


def test_supervisor_crash_recovery_waits_for_cli_and_never_repeats_completed_model(repository, tmp_path):
    from core.task_snapshot_review.role_owned_process_cleanup import reconcile_registered_role_owned_process_cleanup
    run, attempt, marker, release, worker = create_role_with_detached_service(repository, tmp_path,
        wait_for_release=True, output_format='kimi_stream_json')
    try:
        wait_until(marker.exists)
        supervision_file = attempt / 'command-supervision.json'
        identity = read_json(marker)
        wait_until(lambda: any(item['pid'] == identity['pid'] for item in read_json(supervision_file)['owned_processes']))
        facts = read_json(supervision_file)
        worker.kill()
        worker.wait(timeout=5)
        os.kill(facts['supervisor_pid'], signal.SIGKILL)
        wait_until(lambda: native_process_identity(facts['supervisor_pid']) is None)
        reconcile_registered_role_owned_process_cleanup(run)
        assert role_process_execution.process_lock_is_held(attempt)
        assert native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
        release.touch()
        wait_until(lambda: not role_process_execution.process_lock_is_held(attempt))
        # 原生结果补证与清理分别接续，保留未知覆盖历史，不再次调用模型。
        reconcile_registered_role_owned_process_cleanup(run)
        assert not native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
        assert read_json(attempt / 'role-owned-process-cleanup.json')['status'] == 'unknown'
        assert not (attempt / 'outcome.json').exists()
        completed, replacement = workflow._start_role(run, read_json(run / 'run.json'), attempt.parent, 'reviewer', [], [])
        assert completed['result']['status'] == 'completed'
        assert completed['role_owned_process_cleanup_status'] == 'unknown'
        assert replacement is None
        assert len(list(attempt.parent.glob('attempt-*'))) == 1
    finally:
        release.touch()
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        safely_clean_service(marker)


def test_role_cleanup_escalates_only_after_grace_period_and_verifies_exit(repository, tmp_path):
    run, attempt, marker, _, worker = create_role_with_detached_service(repository, tmp_path, ignore_service_sigterm=True)
    try:
        wait_until(marker.exists)
        started = time.monotonic()
        assert worker.wait(timeout=8) == 0
        assert time.monotonic() - started >= 2
        cleanup = read_json(attempt / 'role-owned-process-cleanup.json')
        identity = read_json(marker)
        assert cleanup['status'] == 'exited'
        assert [item['signal'] for item in cleanup['signal_requests'] if item['pid'] == identity['pid']] == ['SIGTERM', 'SIGKILL']
        assert not native_process_start_identity_matches(native_process_identity(identity['pid']), identity)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        safely_clean_service(marker)


@pytest.mark.parametrize('invalid_identity', [
    {'pid': 2**32 + 800603, 'start_identity': [101, 0]},
    {'pid': 800603},
    {'pid': 800603, 'start_identity': None},
])
def test_invalid_role_resource_identity_never_reaches_native_query_or_signal(tmp_path, monkeypatch, invalid_identity):
    from core.task_snapshot_review import role_owned_process_cleanup as cleanup
    from core.task_snapshot_review.native_owned_process_observation import (
        REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
    )
    from core.task_snapshot_review.persistent_artifacts import write_json
    attempt = tmp_path / 'reviewer-1/attempt-001'
    write_json(attempt / 'command-supervision.json', {'execution_token': 'fixed-role-attempt-token',
        'command_directory': str(attempt.resolve()), 'host_boot_identity': 'fixed-boot',
        'supervisor_native_identity': {'pid': 800601, 'start_identity': [100, 0]},
        'status': 'descendant_exit_unproven', 'ownership_observation_coverage_status': 'unknown',
        'command_process_lifetime_contract': ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'process_ownership_scope': REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'owned_processes': [invalid_identity]})
    monkeypatch.setattr(cleanup, 'native_host_boot_identity', lambda: 'fixed-boot')
    def guarded_query(pid):
        assert pid == 800601, '无效资源身份不得传入原生 PID 查询'
        return None
    monkeypatch.setattr(cleanup, 'native_process_identity', guarded_query)
    monkeypatch.setattr(cleanup.os, 'kill', lambda *args: pytest.fail('不能对缺少世代证明的资源发送信号'))
    first = cleanup.cleanup_role_owned_processes(attempt)
    assert first['status'] == 'unknown'
    assert cleanup.cleanup_role_owned_processes(attempt) == first
