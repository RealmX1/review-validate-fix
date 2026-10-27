"""Kanban 起点来自任务证据；进入 base 不改变已固定窗口。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, ROOT
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from adapters.cline_kanban.task_review_starting_point import (
    attach_task_review_run, record_task_review_start, resolve_task_review_starting_point,
)


@pytest.fixture
def kanban_task(repository, tmp_path, monkeypatch):
    git(repository, 'branch', '-M', 'main')
    initial = git_text(repository, 'rev-parse', 'HEAD')
    worktree = tmp_path / 'kanban-task-worktree'
    git(repository, 'worktree', 'add', '-q', '--detach', str(worktree), 'main')
    workspace = tmp_path / 'kanban-state' / 'workspaces' / 'isolated-project'
    workspace.mkdir(parents=True)
    write_json(workspace / 'board.json', {'columns': [{'id': 'in_progress', 'cards': [
        {'id': 'task-1', 'createdAt': 1000, 'baseRef': 'main'}]}]})
    write_json(workspace / 'sessions.json', {'task-1': {'taskId': 'task-1', 'workspacePath': str(worktree)}})
    entry = {'taskId': 'task-1', 'baseRef': 'main', 'incarnations': [{
        'incarnationId': initial + ':1001', 'initialBaseCommit': initial,
        'captureSource': 'worktree_creation', 'startedAt': 1001, 'observedTaskCommits': [],
    }], 'lastWorkspaceGitSnapshot': None}
    write_json(workspace / 'task-commit-integration-provenance.json', {'schemaVersion': 1, 'tasks': {'task-1': entry}})
    monkeypatch.setenv('RVF_CLINE_KANBAN_STATE_DIRECTORY', str(workspace.parents[1]))
    monkeypatch.setenv('KANBAN_TASK_ID', 'task-1')
    monkeypatch.setenv('KANBAN_PROJECT_PATH', str(repository))
    return {'repo': worktree, 'main': repository, 'workspace': workspace, 'initial': initial}


def commit_task(task, *, count=1):
    repo = task['repo']
    for index in range(count):
        (repo / f'feature-{index}.txt').write_text(f'任务实现 {index}\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'feat: 完成隔离任务')
    commit = git_text(repo, 'rev-parse', 'HEAD')
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    provenance = read_json(path)
    provenance['tasks']['task-1']['incarnations'][-1]['observedTaskCommits'].append({'commitSha': commit, 'stablePatchId': None})
    write_json(path, provenance)
    return commit


def resolve(task, **kwargs):
    return resolve_task_review_starting_point(task['repo'], 'task-1', **kwargs)


@pytest.mark.parametrize('landing', ['ff', 'merge'])
def test_automatic_start_preserves_ten_committed_paths_after_landing(kanban_task, tmp_path, landing):
    task = kanban_task
    own = commit_task(task, count=10)
    git(task['main'], 'merge', '--ff-only' if landing == 'ff' else '--no-ff', '-m', 'test: 合回 base', own)
    evidence = resolve(task, scope_paths=['.'])
    assert evidence['status'] == 'verified'
    assert evidence['base_commit'] == task['initial']
    assert evidence['task_owned_commit_shas'] == [own]
    assert evidence['committed_work_status'] == 'present'
    assert len(evidence['changed_paths']) == 10
    run = tmp_path / 'fixed-review-run'
    record = create_snapshot(task['repo'], base_ref=evidence['base_commit'], scope_paths=['.'],
                             starting_point_evidence=evidence, requirements=b'original requirements', run_directory=run)
    (task['main'] / 'external.txt').write_text('外部工作\n')
    git(task['main'], 'add', '.')
    git(task['main'], 'commit', '-qm', 'test: 外部前移')
    assert read_json(run / 'run.json') == record
    assert len(record['changed_paths']) == 10
    assert record['starting_point_evidence']['base_commit'] == task['initial']


def test_explicit_implementation_start_survives_prompts_and_new_sessions(kanban_task):
    task = kanban_task
    first = commit_task(task)
    recorded = record_task_review_start(task['repo'], 'task-1', base_ref=first, scope_paths=['task.py'])
    assert recorded['status'] == 'verified'
    (task['repo'] / 'task.py').write_text('value = 5\n')
    git(task['repo'], 'commit', '-qam', 'fix: 新一段实现')
    for event in ({'prompt': '继续', 'turn_id': 'turn-1'}, {'prompt': '继续', 'turn_id': 'turn-2', 'session_id': 'new-session'}):
        evidence = resolve(task, event=event)
        assert evidence['base_commit'] == first
        assert evidence['changed_paths'] == ['task.py']


@pytest.mark.parametrize('kind', ['ff', 'merge', 'rebase'])
def test_base_absorption_does_not_claim_external_commits(kanban_task, kind):
    task = kanban_task
    (task['main'] / 'external.txt').write_text('仅外部工作\n')
    git(task['main'], 'add', '.')
    git(task['main'], 'commit', '-qm', 'test: 外部提交')
    if kind == 'rebase':
        git(task['repo'], 'rebase', 'main')
    else:
        git(task['repo'], 'merge', '--ff-only' if kind == 'ff' else '--no-ff', '-m', 'test: 吸收 base', 'main')
    evidence = resolve(task, scope_paths=['.'])
    assert not evidence.get('task_owned_commit_shas')
    assert evidence['committed_work_status'] in ('absent', 'unknown')


def test_missing_provenance_recovers_from_checkpoint_and_complete_git_creation(kanban_task):
    import base64
    task = kanban_task
    (task['workspace'] / 'task-commit-integration-provenance.json').unlink()
    encoded = base64.urlsafe_b64encode(b'task-1').decode().rstrip('=')
    checkpoint = git_text(task['repo'], 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', input_bytes=b'checkpoint\n')
    git(task['repo'], 'update-ref', f'refs/kanban/checkpoints/{encoded}/turn/1', checkpoint)
    (task['repo'] / 'task.py').write_text('value = 9\n')
    git(task['repo'], 'commit', '-qam', 'fix: 本地提交补证')
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert evidence['base_commit'] == task['initial']
    assert evidence['committed_work_status'] == 'present'


def test_unknown_is_durable_bounded_and_recovers_when_evidence_arrives(kanban_task):
    task = kanban_task
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    source = path.read_bytes()
    path.write_text('{broken')
    first = resolve(task)
    assert first['status'] == 'unknown'
    assert first['recovery_condition']
    assert Path(first['recovery_record_path']).exists()
    assert resolve(task)['resolution_attempt_count'] == first['resolution_attempt_count']
    event = {'prompt': '继续', 'turn_id': 'real-turn-2'}
    retried = resolve(task, event=event)
    assert retried['resolution_attempt_count'] == first['resolution_attempt_count'] + 1
    assert resolve(task, event=event)['resolution_attempt_count'] == retried['resolution_attempt_count']
    path.write_bytes(source)
    recovered = resolve(task)
    assert recovered['status'] == 'verified'
    assert recovered['base_commit'] == task['initial']


@pytest.mark.parametrize('replacement', ['card', 'worktree', 'incarnation', 'project'])
def test_replaced_identity_never_inherits_saved_start(kanban_task, tmp_path, monkeypatch, replacement):
    task = kanban_task
    record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'], scope_paths=['.'])
    if replacement == 'card':
        path = task['workspace'] / 'board.json'
        data = read_json(path); data['columns'][0]['cards'][0]['createdAt'] = 2000; write_json(path, data)
    elif replacement == 'worktree':
        path = task['workspace'] / 'sessions.json'
        data = read_json(path); data['task-1']['workspacePath'] = str(tmp_path / 'different-worktree'); write_json(path, data)
    elif replacement == 'incarnation':
        path = task['workspace'] / 'task-commit-integration-provenance.json'
        data = read_json(path); incarnation = data['tasks']['task-1']['incarnations'][-1]
        incarnation.update(startedAt=3000, incarnationId=task['initial'] + ':3000'); write_json(path, data)
    else:
        other = tmp_path / 'different-project'; other.mkdir(); git(other, 'init', '-q')
        monkeypatch.setenv('KANBAN_PROJECT_PATH', str(other))
    assert resolve(task)['status'] == 'unknown'


def test_non_main_base_is_preserved(kanban_task):
    task = kanban_task
    git(task['main'], 'branch', 'integration')
    for name in ['board.json', 'task-commit-integration-provenance.json']:
        path = task['workspace'] / name
        write_json(path, json.loads(path.read_text().replace('"main"', '"integration"')))
    commit_task(task)
    assert resolve(task)['base_ref'] == 'integration'


def test_cli_requires_one_baseline_and_uses_kanban_evidence(kanban_task, tmp_path):
    task = kanban_task
    own = commit_task(task, count=10)
    git(task['main'], 'merge', '--ff-only', own)
    requirements = tmp_path / 'requirements.md'; requirements.write_text('原始需求')
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    argv = [sys.executable, str(entry), 'review-task-snapshot', '--repo', str(task['repo']),
            '--base-from-kanban-task', 'task-1', '--scope', '.', '--requirements-file', str(requirements),
            '--run-directory', str(tmp_path / 'cli-review-run'), '--prepare-only']
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    record = read_json(tmp_path / 'cli-review-run' / 'run.json')
    assert record['base_commit'] == task['initial']
    assert len(record['changed_paths']) == 10
    both = subprocess.run(argv + ['--base-ref', 'main'], capture_output=True, text=True)
    assert both.returncode == 2


def test_amended_task_commit_requires_unique_patch_identity(kanban_task):
    task = kanban_task
    original = commit_task(task)
    git(task['repo'], 'commit', '--amend', '-qm', 'feat: 改写任务提交说明')
    rewritten = git_text(task['repo'], 'rev-parse', 'HEAD')
    assert rewritten != original
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert evidence['task_owned_commit_shas'] == [rewritten]


def test_duplicate_patch_identity_remains_unknown(kanban_task):
    task = kanban_task
    original = commit_task(task)
    git(task['repo'], 'reset', '--hard', task['initial'])
    git(task['repo'], 'cherry-pick', '-x', original)
    rewritten = git_text(task['repo'], 'rev-parse', 'HEAD')
    git(task['repo'], 'revert', '--no-edit', rewritten)
    git(task['repo'], 'cherry-pick', '-x', original)
    evidence = resolve(task)
    assert evidence['status'] == 'unknown'
    assert 'patch-id' in evidence['diagnostic']


def test_backfilled_live_merge_base_is_never_used_as_creation_start(kanban_task):
    task = kanban_task
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    data = read_json(path)
    data['tasks']['task-1']['incarnations'][-1]['captureSource'] = 'live_pre_handback_backfill'
    write_json(path, data)
    assert resolve(task)['status'] == 'unknown'


def test_corrupt_start_automatically_recovers_from_independent_persisted_evidence(kanban_task):
    task = kanban_task
    own = commit_task(task)
    saved = record_task_review_start(task['repo'], 'task-1', base_ref=own, scope_paths=['task.py'])
    starting_path = Path(saved['recovery_record_path']).with_name('starting-point.json')
    starting_path.write_text('{corrupt-start')
    recovered = resolve(task)
    assert recovered['status'] == 'verified'
    assert recovered['base_commit'] == own
    assert recovered['recovery_actions']
    assert list(starting_path.parent.glob('starting-point.corrupt-*.json'))


def test_same_path_recreated_worktree_does_not_inherit_start(kanban_task):
    task = kanban_task
    record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'], scope_paths=['.'])
    git(task['main'], 'worktree', 'remove', str(task['repo']))
    git(task['main'], 'worktree', 'add', '--detach', str(task['repo']), 'main')
    assert resolve(task)['status'] == 'unknown'


def completed_scoped_run(task, tmp_path):
    from test_task_snapshot_review_workflow import fake_config
    from core.task_snapshot_review.workflow import resume_review
    record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'], scope_paths=['task.py'])
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'fix: 被精确范围评审的实现')
    evidence = resolve(task, scope_paths=['task.py'])
    run = tmp_path / 'completed-scoped-review'
    create_snapshot(task['repo'], base_ref=evidence['base_commit'], scope_paths=['task.py'], requirements=b'requirements',
                    run_directory=run, starting_point_evidence=evidence)
    attach_task_review_run(task['repo'], 'task-1', run)
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    result = resume_review(run, validation_commands=[[sys.executable, '-c', 'pass']])
    assert result['status'] == 'completed'
    return run


def test_increment_requires_explicit_completed_run_and_expansion_restarts_original_scope(kanban_task, tmp_path):
    task = kanban_task
    run = completed_scoped_run(task, tmp_path)
    completed = git_text(task['repo'], 'rev-parse', 'HEAD')
    (task['repo'] / 'task.py').write_text('value = 3\n')
    (task['repo'] / 'new-path.txt').write_text('扩大范围的工作\n')
    git(task['repo'], 'add', '.'); git(task['repo'], 'commit', '-qm', 'fix: 后续实现')
    assert resolve(task, scope_paths=['task.py'])['base_commit'] == task['initial']
    incremental = resolve(task, scope_paths=['task.py'], incremental_after_run=run)
    assert incremental['status'] == 'verified'
    assert incremental['base_commit'] == completed
    expanded = resolve(task, scope_paths=['task.py', 'new-path.txt'], incremental_after_run=run)
    assert expanded['base_commit'] == task['initial']
    assert expanded['changed_paths'] == ['new-path.txt', 'task.py']
    assert resolve(task, scope_paths=['task.py'])['base_commit'] == task['initial']


def test_incomplete_or_corrupted_run_cannot_advance_start(kanban_task, tmp_path):
    task = kanban_task
    run = completed_scoped_run(task, tmp_path)
    validation = read_json(run / 'validation.json')
    validation['tested_tree'] = '0' * 40
    write_json(run / 'validation.json', validation)
    assert resolve(task, scope_paths=['task.py'], incremental_after_run=run)['status'] == 'unknown'


def test_shared_path_uses_full_net_diff_and_preserves_background_wip(kanban_task, tmp_path):
    from test_task_snapshot_review_workflow import canary
    task = kanban_task
    record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'], scope_paths=['task.py'])
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'fix: 本任务部分')
    (task['main'] / 'task.py').write_text('value = 1\nexternal_value = 10\n')
    git(task['main'], 'commit', '-qam', 'feat: 同路径外部部分')
    git(task['repo'], 'merge', '-s', 'ours', '--no-ff', '-m', 'test: 吸收外部历史', 'main')
    (task['repo'] / 'task.py').write_text('value = 2\nexternal_value = 10\n')
    git(task['repo'], 'commit', '-qam', 'test: 保留同路径上下文')
    (task['repo'] / 'background.txt').write_text('staged background\n'); git(task['repo'], 'add', 'background.txt')
    (task['repo'] / 'background.txt').write_text('unstaged background\n')
    (task['repo'] / 'background-untracked.txt').write_text('untracked background\n')
    before = canary(task['repo'])
    evidence = resolve(task, scope_paths=['task.py'])
    run = tmp_path / 'full-path-net-diff'
    create_snapshot(task['repo'], base_ref=evidence['base_commit'], scope_paths=['task.py'], requirements=b'requirements',
                    run_directory=run, starting_point_evidence=evidence)
    patch = (run / 'task-changes.patch').read_text()
    assert '+value = 2' in patch
    assert '+external_value = 10' in patch
    assert canary(task['repo']) == before


def test_changed_task_environment_invalidates_cached_binding(kanban_task, monkeypatch):
    task = kanban_task
    assert resolve(task)['status'] == 'verified'
    monkeypatch.setenv('KANBAN_TASK_ID', 'different-task')
    assert resolve(task)['status'] == 'unknown'


def test_stale_starting_evidence_cannot_freeze_new_head(kanban_task, tmp_path):
    task = kanban_task
    evidence = resolve(task, scope_paths=['task.py'])
    (task['repo'] / 'task.py').write_text('value = 3\n')
    git(task['repo'], 'commit', '-qam', 'fix: 解析之后的新提交')
    with pytest.raises(ValueError, match='起点证据'):
        create_snapshot(task['repo'], base_ref=evidence['base_commit'], scope_paths=['task.py'],
                        starting_point_evidence=evidence, requirements=b'requirements', run_directory=tmp_path / 'stale-evidence-run')


def test_packaged_cli_automatically_selects_start_without_checkout_imports(kanban_task, tmp_path):
    import os
    from test_official_cli_developer_plugin_installation import _load_installer
    task = kanban_task
    commit_task(task)
    release = _load_installer().build_developer_marketplace_release(source_repository=ROOT,
        home_directory=tmp_path / 'packaged-home', source_commit='c' * 40, source_commit_count=3)
    entry = release.release_root / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    requirements = tmp_path / 'requirements.md'; requirements.write_text('用户需求')
    run = tmp_path / 'packaged-task-run'
    environment = {**os.environ, 'PYTHONPATH': ''}
    result = subprocess.run([sys.executable, str(entry), 'review-task-snapshot', '--repo', str(task['repo']),
        '--base-from-kanban-task', 'task-1', '--scope', '.', '--requirements-file', str(requirements),
        '--run-directory', str(run), '--prepare-only'], cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read_json(run / 'run.json')['base_commit'] == task['initial']
