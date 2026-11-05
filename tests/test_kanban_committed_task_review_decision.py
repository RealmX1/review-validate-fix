"""干净提交只触发一次真实评审决策；缺证与外部同步不续跑 Agent。"""
import pytest

from adapters import resolve_hook_host_adapter
from core.task_snapshot_review.hook_entrypoint import handle_hook
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from test_kanban_task_review_starting_point import kanban_task, commit_task, resolve, repository, completed_scoped_run


def stop(task, host='codex', session='session-1'):
    return handle_hook({'cwd': str(task['repo']), 'session_id': session}, event_name='Stop',
                       host_adapter=resolve_hook_host_adapter(host))


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_clean_committed_after_landing_prompts_once_across_sessions(kanban_task, host):
    task = kanban_task
    own = commit_task(task)
    git(task['main'], 'merge', '--ff-only', own)
    assert stop(task, host)
    assert stop(task, host, session='different-session') is None


def test_unknown_only_persists_diagnostic_and_recovers_without_waking_agent(kanban_task):
    task = kanban_task
    own = commit_task(task)
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    source = path.read_bytes(); path.write_text('{broken')
    assert stop(task) is None
    assert resolve(task)['status'] == 'unknown'
    path.write_bytes(source)
    assert stop(task)


def test_net_zero_reverted_work_has_no_decision(kanban_task):
    task = kanban_task
    own = commit_task(task)
    git(task['repo'], 'revert', '--no-edit', own)
    evidence = resolve(task)
    assert evidence['changed_paths'] == []
    assert stop(task) is None


def test_clean_base_only_absorption_has_no_decision(kanban_task):
    task = kanban_task
    (task['main'] / 'external.txt').write_text('外部提交\n')
    git(task['main'], 'add', '.'); git(task['main'], 'commit', '-qm', 'test: 外部')
    git(task['repo'], 'merge', '--ff-only', 'main')
    assert stop(task) is None


@pytest.mark.parametrize('adds_task_page', [False, True])
def test_merge_authored_task_page_is_reviewed_without_claiming_imported_base_paths(kanban_task, adds_task_page):
    task = kanban_task
    commit_task(task)
    (task['main'] / 'external.txt').write_text('仅属于 base 的内容\n')
    git(task['main'], 'add', '--', 'external.txt')
    git(task['main'], 'commit', '-qm', 'test: 前移 base')
    git(task['repo'], 'merge', '--no-ff', '--no-commit', 'main')
    (task['repo'] / 'feature-0.txt').unlink()
    git(task['repo'], 'add', '--', 'feature-0.txt')
    if adds_task_page:
        (task['repo'] / 'merge-authored-task-page.txt').write_text('合并时新增的任务页\n')
        git(task['repo'], 'add', '--', 'merge-authored-task-page.txt')
    git(task['repo'], 'commit', '-qm', 'test: 合并时完成任务内容')
    merge_commit = git_text(task['repo'], 'rev-parse', 'HEAD')
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    provenance = read_json(path)
    provenance['tasks']['task-1']['incarnations'][-1]['observedTaskCommits'].append(
        {'commitSha': merge_commit, 'stablePatchId': None})
    write_json(path, provenance)
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert (evidence['committed_work_status'] == 'present') is adds_task_page
    assert bool(stop(task)) is adds_task_page


@pytest.mark.parametrize('prompt', ['继续实现', [{'type': 'text', 'text': '继续实现'}]])
def test_real_prompt_rechecks_evidence_without_adding_diagnostic_context(kanban_task, prompt):
    task = kanban_task
    (task['workspace'] / 'task-commit-integration-provenance.json').write_text('{broken')
    first = resolve(task)
    event = {'cwd': str(task['repo']), 'prompt': prompt, 'turn_id': 'real-turn'}
    assert handle_hook(event, event_name='UserPromptSubmit', host_adapter=resolve_hook_host_adapter('kimi_code')) is None
    after = resolve(task)
    assert after['resolution_attempt_count'] == first['resolution_attempt_count'] + 1


def test_completed_current_scope_is_not_prompted_again(kanban_task, tmp_path):
    task = kanban_task
    completed_scoped_run(task, tmp_path)
    assert stop(task) is None
    (task['repo'] / 'task.py').write_text('value = 4\n')
    git(task['repo'], 'commit', '-qam', 'fix: 新的未审工作')
    assert stop(task)
