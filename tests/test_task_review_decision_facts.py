"""共用事实保留统计口径、证据身份及失败后的有界恢复。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters import resolve_hook_host_adapter
from adapters.cline_kanban.task_review_starting_point import record_task_review_start
from core.task_snapshot_review.hook_entrypoint import handle_hook
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_review_decision_facts import collect_task_review_decision_facts
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_kanban_task_review_starting_point import kanban_task, commit_task, resolve
from test_task_snapshot_review_workflow import ROOT, canary, repository


def collect(task, **kwargs):
    return collect_task_review_decision_facts(task['repo'], task_id='task-1',
        starting_point_evidence=resolve(task), **kwargs)


def test_mixed_dirty_counts_net_content_and_preserves_background_index(kanban_task):
    task = kanban_task
    repo = task['repo']
    record_task_review_start(repo, 'task-1', base_ref=task['initial'], scope_paths=['task.py', 'tests', 'docs', 'skills'])
    (repo / 'task.py').write_text('value = 2\n')
    git(repo, 'add', 'task.py')
    (repo / 'task.py').write_text('value = 3\nextra = True\n')
    for directory in ('tests/fixtures', 'docs', 'skills/example'):
        (repo / directory).mkdir(parents=True)
    (repo / 'tests/test_feature.py').write_text('assert True\n')
    (repo / 'tests/fixtures/blob.bin').write_bytes(b'\x00\x01binary')
    (repo / 'docs/change.md').write_text('说明\n')
    (repo / 'skills/example/SKILL.md').write_text('控制 Agent 行为\n')
    git(repo, 'mv', 'background.txt', 'renamed\tbackground.txt')
    before = canary(repo)
    packet = collect(task)
    assert canary(repo) == before
    dirty = packet['facts']['workspace_change_statistics']
    assert dirty['status'] == 'verified'
    value = dirty['value']
    assert value['total'] == {'file_count': 6, 'additions': 5, 'deletions': 1, 'binary_file_count': 1}
    assert value['groups']['implementation_behavior']['file_count'] == 2
    assert value['groups']['tests_fixtures']['binary_file_count'] == 1
    assert value['groups']['documentation']['additions'] == 1
    assert value['background_wip']['file_count'] == 1
    assert value['task_scope']['file_count'] == 5
    rename = next(item for item in value['files'] if item['path'] == 'renamed\tbackground.txt')
    assert rename['previous_path'] == 'background.txt'
    assert packet['measurement']['additional_worktree_capture_count'] == 0
    assert Path(packet['artifact_path']).is_file()


def test_clean_committed_task_has_history_and_zero_dirty(kanban_task):
    task = kanban_task
    commit_task(task, count=3)
    packet = collect(task)
    assert packet['facts']['workspace_change_statistics']['value']['total']['file_count'] == 0
    history = packet['facts']['task_commit_history_statistics']
    assert history['status'] == 'verified'
    assert history['value']['commit_count'] == 1
    assert history['value']['additions'] == 3
    assert '历史累计' in packet['summary'] and 'Dirty' in packet['summary']


def test_task_card_statistics_are_reused_without_git_history_queries(kanban_task, monkeypatch):
    task = kanban_task
    commit = commit_task(task, count=2)
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    source = read_json(path)
    entry = source['tasks']['task-1']
    entry['lastWorkspaceGitSnapshot'] = {'path': str(task['repo']), 'headCommit': commit,
        'baseRefTipCommit': task['initial'], 'observedAt': 2000}
    entry['retainedCommittedLineChangeHistory'] = {'summary': {'status': 'complete', 'commitCount': 1,
        'additions': 2, 'deletions': 0, 'historyVersion': 'isolated-history'},
        'commits': [{'commitSha': commit, 'additions': 2, 'deletions': 0}]}
    write_json(path, source)
    evidence = resolve(task)
    import core.task_snapshot_review.task_review_decision_facts as facts_module
    ordinary_git = facts_module.git
    commands = []
    def observed_git(repo, *arguments, **kwargs):
        commands.append(arguments)
        assert arguments[0] not in {'show', 'log', 'rev-list'}
        return ordinary_git(repo, *arguments, **kwargs)
    monkeypatch.setattr(facts_module, 'git', observed_git)
    packet = collect_task_review_decision_facts(task['repo'], task_id='task-1', starting_point_evidence=evidence)
    history = packet['facts']['task_commit_history_statistics']
    assert history['value']['basis'] == 'task_card_retained_committed_history'
    assert history['value']['additions'] == 2
    assert packet['measurement']['statistics_git_command_count'] == 1


def test_repeated_observation_and_dirty_only_change_reuse_history(kanban_task, monkeypatch):
    task = kanban_task
    commit_task(task)
    first = collect(task)
    second = collect(task)
    assert second['artifact_path'] == first['artifact_path']
    assert second['measurement']['statistics_git_command_count'] == 0
    (task['repo'] / 'task.py').write_text('value = 4\n')
    third = collect(task)
    assert third['observation_id'] != first['observation_id']
    assert third['facts']['task_commit_history_statistics']['observation_id'] == first['facts']['task_commit_history_statistics']['observation_id']
    assert third['measurement']['statistics_git_command_count'] == 1


def test_stat_failure_is_unknown_until_source_change_or_next_real_prompt(kanban_task, monkeypatch):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 7\n')
    evidence = resolve(task)
    import core.task_snapshot_review.task_review_decision_facts as facts_module
    ordinary_git = facts_module.git
    attempts = []
    def failed_statistics(repo, *arguments, **kwargs):
        if '--numstat' in arguments:
            attempts.append(arguments)
            raise RuntimeError('隔离统计暂不可用')
        return ordinary_git(repo, *arguments, **kwargs)
    monkeypatch.setattr(facts_module, 'git', failed_statistics)
    first = collect_task_review_decision_facts(task['repo'], task_id='task-1', starting_point_evidence=evidence)
    assert first['facts']['workspace_change_statistics']['status'] == 'unknown'
    assert first['facts']['task_identity']['status'] == 'verified'
    assert first['facts']['workspace_change_statistics']['recovery_condition']
    collect_task_review_decision_facts(task['repo'], task_id='task-1', starting_point_evidence=evidence)
    assert len(attempts) == 1
    monkeypatch.setattr(facts_module, 'git', ordinary_git)
    recovered = collect_task_review_decision_facts(task['repo'], task_id='task-1', starting_point_evidence=evidence,
        real_prompt_identity='真实用户消息-2')
    assert recovered['facts']['workspace_change_statistics']['status'] == 'verified'
    assert read_json(Path(first['artifact_path']))['facts']['workspace_change_statistics']['status'] == 'unknown'


def test_corrupt_cached_fact_does_not_erase_other_facts(kanban_task):
    task = kanban_task
    commit_task(task)
    first = collect(task)
    artifact = Path(first['artifact_path'])
    artifact.write_text('{broken')
    recovered = collect(task)
    assert recovered['facts']['task_commit_history_statistics']['status'] == 'verified'
    assert artifact.read_text() == '{broken'


def test_identity_mismatch_never_reuses_verified_task_history(kanban_task):
    task = kanban_task
    commit_task(task)
    first = collect(task)
    path = task['workspace'] / 'sessions.json'
    source = read_json(path)
    source['task-1']['workspacePath'] = str(task['main'])
    write_json(path, source)
    packet = collect(task)
    assert packet['facts']['task_identity']['status'] == 'unknown'
    assert packet['facts']['task_commit_history_statistics']['status'] == 'unknown'
    assert packet['facts']['workspace_change_statistics']['status'] == 'verified'
    assert read_json(Path(first['artifact_path']))['facts']['task_identity']['status'] == 'verified'


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_stop_and_explicit_entry_deliver_same_persistent_facts_without_extra_prompt(kanban_task, host):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 5\n')
    adapter = resolve_hook_host_adapter(host)
    event = {'cwd': str(task['repo']), 'session_id': 'session-1'}
    payload = handle_hook(event, event_name='Stop', host_adapter=adapter)
    assert payload and 'Dirty' in json.dumps(payload, ensure_ascii=False)
    assert '跳过' in json.dumps(payload, ensure_ascii=False)
    git(task['repo'], 'add', 'task.py')
    assert handle_hook(event, event_name='Stop', host_adapter=adapter) is None
    git(task['repo'], 'commit', '-qm', 'fix: 提交相同内容')
    assert handle_hook(event, event_name='Stop', host_adapter=adapter) is None
    explicit = handle_hook({**event, 'prompt': '/review-validate-fix', 'turn_id': 'turn-2'},
        event_name='UserPromptSubmit', host_adapter=adapter)
    assert '历史累计' in json.dumps(explicit, ensure_ascii=False)


def test_snapshot_facts_are_bound_to_candidate_and_not_stale_hook_content(kanban_task, tmp_path):
    task = kanban_task
    evidence = resolve(task, scope_paths=['task.py'])
    (task['repo'] / 'task.py').write_text('value = 8\n')
    run = tmp_path / 'review-run'
    record = create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'],
        requirements=b'requirements', run_directory=run, starting_point_evidence=evidence)
    packet = read_json(run / 'decision-facts.json')
    assert packet['observation']['candidate_commit'] == record['candidate_commit']
    assert packet['observation']['worktree_tree'] == git_text(task['repo'], 'rev-parse', f"{record['original_worktree_observation_commit']}^{{tree}}")
    assert packet['facts']['workspace_change_statistics']['value']['total']['additions'] == 1
    assert packet['facts']['existing_review_evidence']['status'] == 'unknown'
    assert record['decision_facts_sha256']
    assert 'decision-facts.json' in (run / 'review-input.md').read_text()


def test_cli_prepare_only_reports_facts_and_next_process_reuses_cache(kanban_task, tmp_path):
    task = kanban_task
    commit_task(task)
    first = collect(task)
    program = ('import json; from pathlib import Path; '
        'from adapters.cline_kanban.task_review_starting_point import resolve_task_review_starting_point; '
        'from core.task_snapshot_review.task_review_decision_facts import collect_task_review_decision_facts; '
        f'repo=Path({str(task["repo"])!r}); '
        'print(json.dumps(collect_task_review_decision_facts(repo, task_id="task-1", '
        'starting_point_evidence=resolve_task_review_starting_point(repo,"task-1"))))')
    restarted = subprocess.run([sys.executable, '-c', program], cwd=ROOT, capture_output=True, text=True)
    assert restarted.returncode == 0, restarted.stderr
    packet = json.loads(restarted.stdout)
    assert packet['artifact_path'] == first['artifact_path']
    assert packet['measurement']['statistics_git_command_count'] == 0
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('隔离入口验收')
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    result = subprocess.run([sys.executable, str(entry), 'review-task-snapshot', '--repo', str(task['repo']),
        '--base-from-kanban-task', 'task-1', '--scope', '.', '--requirements-file', str(requirements),
        '--run-directory', str(tmp_path / 'cli-run'), '--prepare-only'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert Path(receipt['decision_facts_file']).is_file()
    assert 'Dirty' in receipt['decision_facts_summary']


def test_history_sums_each_commit_instead_of_net_diff(kanban_task):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'fix: 第一段行为')
    first = git_text(task['repo'], 'rev-parse', 'HEAD')
    git(task['repo'], 'revert', '--no-edit', 'HEAD')
    reverted = git_text(task['repo'], 'rev-parse', 'HEAD')
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    source = read_json(path)
    source['tasks']['task-1']['incarnations'][-1]['observedTaskCommits'] = [
        {'commitSha': sha, 'stablePatchId': None} for sha in (first, reverted)]
    write_json(path, source)
    packet = collect(task)
    history = packet['facts']['task_commit_history_statistics']
    assert history['status'] == 'verified'
    assert history['value']['commit_count'] == 2
    assert history['value']['additions'] == 2 and history['value']['deletions'] == 2
    assert packet['facts']['workspace_change_statistics']['value']['total']['file_count'] == 0


def test_stale_task_card_history_uses_immutable_per_commit_cache(kanban_task, monkeypatch):
    task = kanban_task
    commit = commit_task(task, count=2)
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    source = read_json(path)
    entry = source['tasks']['task-1']
    entry['lastWorkspaceGitSnapshot'] = {'path': str(task['repo']), 'headCommit': task['initial'],
                                       'baseRefTipCommit': task['initial']}
    entry['retainedCommittedLineChangeHistory'] = {'summary': {'status': 'complete', 'commitCount': 0,
        'additions': 0, 'deletions': 0}, 'commits': []}
    entry['committedLineChangeStatisticsByCommitSha'] = {commit: {'commitSha': commit,
        'isMergeCommit': False, 'additions': 2, 'deletions': 0}}
    write_json(path, source)
    packet = collect(task)
    assert packet['facts']['task_commit_history_statistics']['value']['basis'] == 'fixed_task_window_commit_sum'
    assert packet['facts']['task_commit_history_statistics']['value']['additions'] == 2
    assert packet['measurement']['statistics_git_command_count'] == 1


def test_history_failure_does_not_erase_dirty_or_repeat_failed_query(kanban_task, monkeypatch):
    task = kanban_task
    commit_task(task)
    (task['repo'] / 'task.py').write_text('value = 9\n')
    import core.task_snapshot_review.task_review_decision_facts as facts_module
    ordinary_git = facts_module.git
    calls = []
    def missing_object(repo, *arguments, **kwargs):
        if arguments[0] == 'show':
            calls.append(arguments)
            raise RuntimeError('精确提交材料暂不可读')
        return ordinary_git(repo, *arguments, **kwargs)
    monkeypatch.setattr(facts_module, 'git', missing_object)
    first = collect(task)
    assert first['facts']['task_commit_history_statistics']['status'] == 'unknown'
    assert first['facts']['workspace_change_statistics']['status'] == 'verified'
    collect(task)
    assert len(calls) == 1
    monkeypatch.setattr(facts_module, 'git', ordinary_git)
    (task['repo'] / 'task.py').write_text('value = 10\n')
    recovered = collect(task)
    # Dirty 变化只重采 Dirty；历史缺口等自己的来源变化或真实用户消息。
    assert recovered['facts']['task_commit_history_statistics']['status'] == 'unknown'
    retried = collect(task, real_prompt_identity='next-real-prompt')
    assert retried['facts']['task_commit_history_statistics']['status'] == 'verified'


def test_active_run_comparison_reuses_the_resolvers_full_worktree_observation(kanban_task, tmp_path, monkeypatch):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    evidence = resolve(task, scope_paths=['task.py'])
    create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'], requirements=b'requirements',
        run_directory=tmp_path / 'active-review', starting_point_evidence=evidence)
    import adapters.cline_kanban.task_review_starting_point as starting_module
    import core.task_snapshot_review.active_run_observation as active_module
    import core.task_snapshot_review.task_review_decision_facts as facts_module
    original_capture = starting_module.capture_worktree_tree
    captures = []
    def observe_capture(*args, **kwargs):
        captures.append(args)
        return original_capture(*args, **kwargs)
    def unexpected_capture(*args, **kwargs):
        pytest.fail('不得重复捕获已经观察的完整工作树')
    monkeypatch.setattr(starting_module, 'capture_worktree_tree', observe_capture)
    monkeypatch.setattr(active_module, 'capture_worktree_tree', unexpected_capture)
    monkeypatch.setattr(facts_module, 'capture_worktree_tree', unexpected_capture)
    assert handle_hook({'cwd': str(task['repo'])}, event_name='Stop',
                       host_adapter=resolve_hook_host_adapter('codex')) is None
    assert len(captures) == 1


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_native_hook_process_preserves_host_fields_and_restart_dedup(kanban_task, host):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    script = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/task_snapshot_review_hook.py'
    command = [sys.executable, str(script), '--host', host]
    event = json.dumps({'cwd': str(task['repo']), 'session_id': 'native-session'})
    first = subprocess.run(command, input=event, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert '持久详单' in json.dumps(payload, ensure_ascii=False)
    if host == 'codex':
        assert payload['decision'] == 'block'
    elif host == 'claude_code':
        assert payload['hookSpecificOutput']['hookEventName'] == 'Stop'
    else:
        assert payload['hookSpecificOutput']['permissionDecision'] == 'deny'
    repeated = subprocess.run(command, input=event, text=True, capture_output=True)
    assert repeated.returncode == 0 and repeated.stdout == ''


def test_frozen_decision_facts_corruption_is_detected_before_reviewer_execution(kanban_task, tmp_path):
    from core.task_snapshot_review.workflow import resume_review
    task = kanban_task
    run = tmp_path / 'immutable-facts-run'
    create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'], requirements=b'requirements',
                    run_directory=run, starting_point_evidence=resolve(task, scope_paths=['task.py']))
    (run / 'decision-facts.json').write_text('{}')
    with pytest.raises(ValueError, match='固定决策事实已改变'):
        resume_review(run)


def test_revoked_run_never_receives_a_resume_hint(kanban_task, tmp_path):
    from core.task_snapshot_review.execution_lifecycle import persist_execution_revocation
    task = kanban_task
    run = tmp_path / 'cancelled-review'
    evidence = resolve(task, scope_paths=['task.py'])
    create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'], requirements=b'requirements',
                    run_directory=run, starting_point_evidence=evidence)
    active = collect(task)
    assert active['facts']['command_guidance']['value']['resume_commands']
    persist_execution_revocation(run, '隔离测试中的用户 Stop')
    revoked = collect(task)
    assert revoked['facts']['command_guidance']['value']['resume_commands'] == []


def test_explicit_different_baseline_preserves_task_facts_and_command_choice(kanban_task, tmp_path):
    task = kanban_task
    head = commit_task(task)
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('显式选择当前提交作为评审基线')
    script = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    run = tmp_path / 'explicit-baseline-run'
    result = subprocess.run([sys.executable, str(script), 'review-task-snapshot', '--repo', str(task['repo']),
        '--base-ref', head, '--scope', '.', '--requirements-file', str(requirements), '--run-directory', str(run),
        '--prepare-only'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    packet = read_json(run / 'decision-facts.json')
    assert packet['facts']['task_identity']['status'] == 'verified'
    assert packet['observation']['base_commit'] == head
    command = packet['facts']['command_guidance']['value']['new_run_command']
    assert '--base-ref ' + head in command and '--base-from-kanban-task' not in command
    assert 'starting_point_evidence' not in read_json(run / 'run.json')


def test_cli_unknown_start_still_provides_independent_dirty_facts(kanban_task, tmp_path):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 6\n')
    (task['workspace'] / 'sessions.json').write_text('{}')
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('未知身份不影响独立 Dirty 统计')
    script = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    result = subprocess.run([sys.executable, str(script), 'review-task-snapshot', '--repo', str(task['repo']),
        '--base-from-kanban-task', 'task-1', '--scope', '.', '--requirements-file', str(requirements), '--prepare-only'],
        capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['status'] == 'unknown'
    packet = read_json(Path(receipt['decision_facts_file']))
    assert packet['facts']['task_identity']['status'] == 'unknown'
    assert packet['facts']['workspace_change_statistics']['value']['total']['file_count'] == 1


def test_all_roles_receive_original_candidate_fact_path(repository, tmp_path):
    from core.task_snapshot_review.workflow import resume_review
    from test_task_snapshot_review_workflow import fake_config, prepare
    run = prepare(repository, tmp_path)
    finding = {'severity': 'high', 'message': '修复当前任务的隔离行为', 'path': 'task.py', 'line': 1}
    reviewer = fake_config(tmp_path, findings=[finding])
    result = resume_review(run, reviewer_configs=[reviewer, reviewer], fix=True,
                           fixer_config=fake_config(tmp_path, fixer=True))
    assert result['status'] == 'ready_for_validation'
    requests = list(run.glob('reviewer-*/attempt-*/request.json')) + list(run.glob('fixer/attempt-*/request.json'))
    assert len(requests) == 3
    for path in requests:
        request = read_json(path)
        assert request['decision_facts_file'] == str(run / 'decision-facts.json')
        assert request['decision_facts_sha256'] == read_json(run / 'run.json')['decision_facts_sha256']
        assert '仅适用于原始候选' in path.with_name('prompt.md').read_text()


def test_collector_source_change_invalidates_facts_without_new_decision(kanban_task, monkeypatch):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    adapter = resolve_hook_host_adapter('codex')
    event = {'cwd': str(task['repo'])}
    assert handle_hook(event, event_name='Stop', host_adapter=adapter)
    first = collect(task)
    import core.task_snapshot_review.task_review_decision_facts as facts_module
    monkeypatch.setattr(facts_module, 'DECISION_FACT_IMPLEMENTATION_SHA256', 'changed-collector-source')
    assert handle_hook(event, event_name='Stop', host_adapter=adapter) is None
    changed = collect(task)
    assert changed['observation_id'] != first['observation_id']
    assert changed['facts']['workspace_change_statistics']['observation_id'] != first['facts']['workspace_change_statistics']['observation_id']
