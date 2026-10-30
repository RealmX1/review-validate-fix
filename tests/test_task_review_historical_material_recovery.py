"""历史材料损坏只降级完成补证，不关闭可靠任务的新内容决策。"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from core.task_snapshot_review.persistent_artifacts import git, git_text, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_completed_task_review_stop_hook_recovery import create_completed_task_snapshot_with_real_git_evidence
from test_kanban_task_review_starting_point import kanban_task, repository, resolve, ROOT


def invoke_task_hook(task, host, *, event='Stop', **payload):
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/task_snapshot_review_hook.py'
    result = subprocess.run([sys.executable, str(entry), '--host', host, '--event', event],
        input=json.dumps({'cwd': str(task['repo']), 'session_id': 'fresh-process', **payload}),
        text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result


def assert_task_decision(result, host):
    payload = json.loads(result.stdout)
    if host == 'codex':
        assert payload['decision'] == 'block'
    elif host == 'claude_code':
        assert payload['hookSpecificOutput']['additionalContext']
    else:
        assert payload['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('material', ['run.json', 'validation.json'])
@pytest.mark.parametrize('linked', [False, True])
def test_unreadable_current_completion_preserves_independent_content_observation(
        kanban_task, tmp_path, host, material, linked):
    task = kanban_task
    run = tmp_path / 'unreadable-current-completion'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=linked)
    original = (run / material).read_bytes()
    (run / material).write_text('{broken')
    first = invoke_task_hook(task, host)
    assert not first.stdout
    assert first.stderr
    unknown = resolve(task)
    assert unknown['status'] == 'verified'
    assert unknown['completion_evidence_status'] == 'unknown'
    assert unknown['observed_worktree_tree']
    assert not invoke_task_hook(task, host).stdout
    assert resolve(task)['resolution_attempt_count'] == unknown['resolution_attempt_count']

    prompt = [{'type': 'text', 'text': '继续'}] if host == 'kimi_code' else '继续'
    assert not invoke_task_hook(task, host, event='UserPromptSubmit', prompt=prompt,
                                prompt_id='real-message-after-damage').stdout
    retried = resolve(task)
    assert retried['resolution_attempt_count'] == unknown['resolution_attempt_count'] + 1
    (task['repo'] / 'task.py').write_text('value = 8\n')
    assert_task_decision(invoke_task_hook(task, host), host)
    changed = resolve(task)
    assert changed['status'] == 'verified'
    assert changed['completion_evidence_status'] == 'unknown'
    assert changed['observed_worktree_tree'] != unknown['observed_worktree_tree']
    assert changed['new_content_since_unproven_completion']
    assert not invoke_task_hook(task, host).stdout
    git(task['repo'], 'commit', '-qam', 'fix: 历史材料损坏后的真实新内容')
    assert not invoke_task_hook(task, host).stdout
    (run / material).write_bytes(original)
    assert resolve(task)['completion_evidence_status'] == 'verified'
    assert not invoke_task_hook(task, host).stdout
    (task['repo'] / 'task.py').write_text('value = 9\n')
    assert_task_decision(invoke_task_hook(task, host), host)


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('material', ['run.json', 'validation.json'])
def test_unrelated_damaged_history_does_not_hide_valid_latest_completion(
        kanban_task, tmp_path, host, material):
    task = kanban_task
    older = tmp_path / 'older-completed-run'
    create_completed_task_snapshot_with_real_git_evidence(task, older)
    newer = tmp_path / 'newer-completed-run'
    (task['repo'] / 'task.py').write_text('value = 4\n')
    record = create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'],
        requirements='新一轮需求'.encode(), run_directory=newer)
    git(task['repo'], 'commit', '--only', '-qm', 'fix: 新一轮完成内容', '--', 'task.py')
    record.update(status='completed', commit=git_text(task['repo'], 'rev-parse', 'HEAD'))
    write_json(newer / 'run.json', record)
    write_json(newer / 'validation.json', {'status': 'passed',
        'tested_tree': git_text(task['repo'], 'rev-parse', 'HEAD^{tree}')})
    assert not invoke_task_hook(task, host).stdout
    assert resolve(task)['fixed_run_directory'] == str(newer)
    original = (older / material).read_bytes()
    (older / material).write_text('["不是 JSON object"]')
    assert not invoke_task_hook(task, host).stdout
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert evidence['completion_evidence_status'] == 'verified'
    assert evidence['current_scope_reviewed']
    assert evidence['fixed_run_directory'] == str(newer)
    (task['repo'] / 'task.py').write_text('value = 8\n')
    assert_task_decision(invoke_task_hook(task, host), host)
    assert not invoke_task_hook(task, host).stdout
    (older / material).write_bytes(original)
    assert not invoke_task_hook(task, host).stdout
