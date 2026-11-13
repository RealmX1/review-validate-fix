"""质量部件在损坏输出、退出竞态及独立提交后的行为回归。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from adapters.cline_kanban import task_review_starting_point as starting_points
from core.task_snapshot_review import execution_lifecycle, retained_command_supervisor, workflow
from core.task_snapshot_review.native_owned_process_observation import native_process_identity
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_completed_task_review_stop_hook_recovery import (
    create_completed_task_snapshot_with_real_git_evidence, task_stop_hook_payload,
    task_stop_hook_payload_from_fresh_process,
)
from test_kanban_task_review_starting_point import kanban_task, resolve
from test_task_snapshot_review_workflow import canary, plain_reviewer_config, prepare, repository


@pytest.mark.parametrize('corrupted_outputs', [
    ['business-result.json'], ['agent-continuation-notices.json'],
    ['business-result.json', 'agent-continuation-notices.json'],
])
def test_corrupted_delivery_outputs_cannot_block_owned_role_or_orphan_cancellation(
        repository, tmp_path, corrupted_outputs):
    run = prepare(repository, tmp_path)
    before = canary(repository)
    command_directory = run / 'registered-orphan-command'
    command_directory.mkdir()
    execution_lifecycle.register_supervised_command_directory(run, command_directory)
    started = tmp_path / 'detached-child-started'
    release = tmp_path / 'release-detached-child'
    forbidden_write = tmp_path / 'forbidden-write-after-cancellation'
    child_program = (f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid()))\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(0.01)\n'
        f'Path({str(forbidden_write)!r}).touch()')
    command = [sys.executable, '-B', '-c',
        f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_program!r}],start_new_session=True).wait()']
    supervisor = subprocess.Popen([sys.executable, '-B', retained_command_supervisor.__file__,
        str(run), str(command_directory), '[]', json.dumps(command)], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owned_role = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    child_pid = None
    try:
        execution_lifecycle.register_owned_process(run, owned_role.pid, 'role_cli', owns_process_group=True)
        deadline = time.monotonic() + 8
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child_pid = int(started.read_text())
        while child_pid not in {item['pid'] for item in read_json(command_directory / 'command-supervision.json').get('owned_processes', [])}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=3)
        history = run / 'agent-review-summaries/revisions/preserved-history.json'
        write_json(history, {'historical': True})
        originals = {name: b'{invalid-current-output' for name in corrupted_outputs}
        write_json(run / 'business-result.json', {'status': 'review_and_validation_completed', 'disposition': 'agent_review_ready'})
        write_json(run / 'agent-continuation-notices.json', {'notices': [{'stage': 'final_agent_review'}]})
        for name, content in originals.items():
            (run / name).write_bytes(content)
        result = execution_lifecycle.revoke_execution(run, '损坏质量输出不阻断明确取消')
        owned_role.wait(timeout=3)
        while native_process_identity(child_pid) is not None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        release.touch()
        assert not forbidden_write.exists()
        assert read_json(run / 'execution-revocation.json')['scope'] == 'exact_rvf_run'
        assert read_json(run / 'cancellation-observations.json') == result
        observations = result['continuation_delivery_withdrawal_observations']
        assert {item['path'].rsplit('/', 1)[-1] for item in observations} == set(corrupted_outputs)
        for item in observations:
            assert item['status'] == 'recovered'
            assert Path(item['preserved_original']).read_bytes() == originals[item['path'].rsplit('/', 1)[-1]]
        assert read_json(history) == {'historical': True}
        assert workflow.resume_review(run)['status'] == 'cancelled'
        assert read_json(run / 'agent-continuation-notices.json')['notices'] == []
        assert canary(repository) == before
    finally:
        for process in (supervisor, owned_role):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('exited_before_signal', [signal.SIGTERM, signal.SIGKILL])
def test_cancelled_validation_persists_interruption_when_supervisor_exits_before_signal(
        repository, tmp_path, monkeypatch, exited_before_signal):
    run = prepare(repository, tmp_path)
    record = read_json(run / 'run.json')
    supervisor = Mock(pid=900001)
    supervisor.poll.return_value = None
    supervisor.wait.side_effect = ([0] if exited_before_signal == signal.SIGTERM else [
        subprocess.TimeoutExpired('隔离监督命令', 8), 0])
    original_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if len(command) > 1 and command[1] == retained_command_supervisor.__file__:
            return supervisor
        return original_popen(command, *args, **kwargs)

    signals = []

    def signal_exited_supervisor(pid, requested_signal):
        assert pid == supervisor.pid
        signals.append(requested_signal)
        if requested_signal == exited_before_signal:
            raise ProcessLookupError('监督者已响应持久撤销并退出')

    def observe_revocation(*args, **kwargs):
        execution_lifecycle.persist_execution_revocation(run, '取消发生在登记与等待之间')
        raise execution_lifecycle.ExecutionRevoked('隔离复现合法退出竞态')

    monkeypatch.setattr(workflow.subprocess, 'Popen', launch)
    monkeypatch.setattr(workflow, 'register_owned_process', lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow.os, 'killpg', signal_exited_supervisor)
    monkeypatch.setattr(execution_lifecycle, 'wait_for_owned_process', observe_revocation)
    with exclusive_lock(run / 'workflow.lock') as lock_handle:
        with pytest.raises(execution_lifecycle.ExecutionRevoked):
            workflow._validate(run, record, [[sys.executable, '-c', 'pass']], lock_handle)
    assert signals == ([signal.SIGTERM] if exited_before_signal == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL])
    assert read_json(run / 'validation.json')['status'] == 'interrupted'
    attempts = list((run / 'final-validation').glob('attempt-*'))
    assert len(attempts) == 1
    assert read_json(attempts[0] / 'validation.json')['status'] == 'interrupted'


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('linked', [False, True])
def test_completed_quality_covers_independent_commit_without_resuming_rvf(kanban_task, tmp_path, host, linked):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    evidence = resolve(task, scope_paths=['task.py'])
    run = tmp_path / 'quality-completed-before-independent-commit'
    create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'],
        requirements='质量完成不要求提交后额外接续'.encode(), run_directory=run,
        starting_point_evidence=evidence if linked else None)
    if linked:
        starting_points.attach_task_review_run(task['repo'], 'task-1', run)
    config = plain_reviewer_config(tmp_path)
    result = workflow.resume_review(run, reviewer_configs=[config, config],
        validation_commands=[[sys.executable, '-c', 'assert 2 == 2']])
    assert result['status'] == 'review_and_validation_completed'
    assert read_json(run / 'run.json')['status'] == 'ready_to_commit'
    attempts = sorted(run.glob('reviewer-*/attempt-*/outcome.json'))
    original_run = (run / 'run.json').read_bytes()
    original_validation = (run / 'validation.json').read_bytes()
    assert task_stop_hook_payload(task, host) is None
    git(task['repo'], 'commit', '--only', '-qm', 'fix: 所属流程独立提交已验证内容', '--', 'task.py')
    assert git_text(task['repo'], 'rev-parse', 'HEAD^{tree}') == read_json(run / 'validation.json')['tested_tree']
    assert resolve(task)['current_scope_reviewed']
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
    assert (run / 'run.json').read_bytes() == original_run
    assert (run / 'validation.json').read_bytes() == original_validation
    assert sorted(run.glob('reviewer-*/attempt-*/outcome.json')) == attempts
    with pytest.raises(ValueError):
        starting_points._completed_run_baseline(task['repo'], run, evidence['binding'], ['task.py'], task['initial'])
    (task['repo'] / 'task.py').write_text('value = 8\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None


@pytest.mark.parametrize('damage', [
    'validation_status', 'tested_tree', 'candidate_identity', 'tested_snapshot_parent',
    'missing_tested_snapshot', 'revocation', 'frozen_reference', 'task_binding',
])
def test_quality_completed_coverage_preserves_integrity_and_revocation_guards(kanban_task, tmp_path, damage):
    task = kanban_task
    run = tmp_path / 'quality-completed-with-background-and-fix'
    record = create_completed_task_snapshot_with_real_git_evidence(task, run, background=True, fixed=True)
    record['status'] = 'ready_to_commit'
    record.pop('commit')
    validation = read_json(run / 'validation.json')
    validation['tested_head'] = record['original_head']
    validation['tested_commit'] = git_text(task['repo'], 'commit-tree', validation['tested_tree'],
        '-p', validation['tested_head'], input_bytes='隔离的完整被测快照\n'.encode())
    write_json(run / 'run.json', record)
    write_json(run / 'validation.json', validation)
    assert resolve(task)['current_scope_reviewed']
    assert task_stop_hook_payload(task) is None
    if damage == 'validation_status':
        validation['status'] = 'running'
    elif damage == 'tested_tree':
        validation['tested_tree'] = '0' * 40
    elif damage == 'candidate_identity':
        validation['candidate_commit'] = record['original_head']
    elif damage == 'tested_snapshot_parent':
        validation['tested_commit'] = git_text(task['repo'], 'commit-tree', validation['tested_tree'],
            input_bytes='父提交缺失的被测快照\n'.encode())
    elif damage == 'missing_tested_snapshot':
        validation.pop('tested_commit')
    elif damage == 'revocation':
        execution_lifecycle.persist_execution_revocation(run, '完成质量证据已撤销')
    elif damage == 'frozen_reference':
        git(task['repo'], 'update-ref', record['snapshot_reference'], record['original_head'])
    elif damage == 'task_binding':
        record['starting_point_evidence']['binding']['task_id'] = 'different-task'
        write_json(run / 'run.json', record)
    write_json(run / 'validation.json', validation)
    original_run = (run / 'run.json').read_bytes()
    before = canary(task['repo'])
    evidence = resolve(task)
    assert not evidence.get('current_scope_reviewed')
    assert evidence['completion_evidence_status'] == 'unknown' or evidence['status'] == 'unknown'
    assert task_stop_hook_payload_from_fresh_process(task) is None
    assert (run / 'run.json').read_bytes() == original_run
    assert canary(task['repo']) == before
