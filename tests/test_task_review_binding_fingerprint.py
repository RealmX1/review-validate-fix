"""真实 Git 补证只补齐绑定来源，不为相同内容重复发起决策。"""
from __future__ import annotations

import base64

import pytest

from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from test_completed_task_review_stop_hook_recovery import (
    create_completed_task_snapshot_with_real_git_evidence,
    task_stop_hook_payload,
    task_stop_hook_payload_from_fresh_process,
)
from test_kanban_task_review_starting_point import kanban_task, repository, resolve


@pytest.fixture
def checkpoint_task_without_provenance(kanban_task):
    task = kanban_task
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    provenance = read_json(path)
    path.unlink()
    encoded = base64.urlsafe_b64encode(b'task-1').decode().rstrip('=')
    checkpoint = git_text(task['repo'], 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD',
                          input_bytes=b'checkpoint\n')
    git(task['repo'], 'update-ref', f'refs/kanban/checkpoints/{encoded}/turn/1', checkpoint)
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert evidence['source'] == 'kanban_checkpoint_and_git_creation'
    assert evidence['base_commit'] == task['initial']
    assert evidence['binding']['creation_commit'] == task['initial']
    assert evidence['binding']['provenance_incarnation_id'] is None
    return task, path, provenance


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('commit_before_backfill', [False, True])
def test_verified_content_survives_provenance_backfill_and_commit(
        checkpoint_task_without_provenance, host, commit_before_backfill):
    task, path, provenance = checkpoint_task_without_provenance
    (task['repo'] / 'task.py').write_text('value = 2\n')
    before = resolve(task)
    assert task_stop_hook_payload(task, host)
    if commit_before_backfill:
        git(task['repo'], 'commit', '-qam', 'fix: 补证前原样提交')
    write_json(path, provenance)
    after = resolve(task)
    assert after['status'] == 'verified'
    assert after['base_commit'] == before['base_commit']
    assert after['scope_paths'] == before['scope_paths']
    assert after['observed_worktree_tree'] == before['observed_worktree_tree']
    assert after['binding']['provenance_incarnation_id'] == task['initial'] + ':1001'
    assert task_stop_hook_payload(task, host, session='metadata-backfilled-session') is None
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
    if not commit_before_backfill:
        git(task['repo'], 'add', 'task.py')
        assert task_stop_hook_payload(task, host) is None
        git(task['repo'], 'commit', '-qm', 'fix: 补证后原样提交')
        assert task_stop_hook_payload_from_fresh_process(task, host) is None
    assert resolve(task)['committed_work_status'] == 'present'
    (task['repo'] / 'task.py').write_text('value = 4\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_unproven_completion_new_content_is_not_repeated_after_provenance_backfill(
        checkpoint_task_without_provenance, tmp_path, host, capsys):
    task, path, provenance = checkpoint_task_without_provenance
    run = tmp_path / 'unproven-completion-without-provenance'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False)
    validation = read_json(run / 'validation.json')
    validation['tested_tree'] = '0' * 40
    write_json(run / 'validation.json', validation)
    assert task_stop_hook_payload(task, host) is None
    assert resolve(task)['completion_evidence_status'] == 'unknown'
    (task['repo'] / 'task.py').write_text('value = 8\n')
    assert task_stop_hook_payload(task, host)
    before = resolve(task)
    assert before['status'] == 'verified'
    assert before['new_content_since_unproven_completion']
    assert before['binding']['provenance_incarnation_id'] is None
    write_json(path, provenance)
    after = resolve(task)
    assert after['status'] == 'verified'
    assert after['completion_evidence_status'] == 'unknown'
    assert after['new_content_since_unproven_completion']
    assert after['base_commit'] == before['base_commit']
    assert after['scope_paths'] == before['scope_paths']
    assert after['observed_worktree_tree'] == before['observed_worktree_tree']
    assert after['binding']['provenance_incarnation_id'] == task['initial'] + ':1001'
    assert task_stop_hook_payload(task, host, session='history-backfilled-session') is None
    git(task['repo'], 'commit', '-qam', 'fix: 历史缺证后的新内容原样提交')
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
    (task['repo'] / 'task.py').write_text('value = 9\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None
    assert resolve(task)['completion_evidence_status'] == 'unknown'
    assert not capsys.readouterr().out
