"""匿名发现的真实 Git 回归；完成 run 由明确的状态替身提供，不启动评审。"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters import resolve_hook_host_adapter
from adapters.cline_kanban import task_review_starting_point as starting_points
from core.task_snapshot_review.hook_entrypoint import handle_hook
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_kanban_task_review_starting_point import kanban_task, commit_task, repository, resolve, ROOT


def update_provenance(task, mutate):
    path = task['workspace'] / 'task-commit-integration-provenance.json'
    document = read_json(path)
    mutate(document['tasks']['task-1'])
    write_json(path, document)


def stop(task, host='codex'):
    return handle_hook({'cwd': str(task['repo'])}, event_name='Stop',
                       host_adapter=resolve_hook_host_adapter(host))


def fresh_process_resolution(task):
    program = (
        'import json,sys; from pathlib import Path; '
        'from adapters.cline_kanban.task_review_starting_point import resolve_task_review_starting_point; '
        'print(json.dumps(resolve_task_review_starting_point(Path(sys.argv[1]), "task-1", '
        'event={"prompt":"继续", "turn_id":"fresh-native-turn"}), ensure_ascii=False))'
    )
    completed = subprocess.run([sys.executable, '-c', program, str(task['repo'])], cwd=ROOT,
                               text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def freeze_and_complete_run_without_reviewers(task, run, *, incremental_after_run=None, scope_paths=None):
    """只替代 reviewer 结果；Git、固定快照、绑定和完整 tested_tree 均实际生成。"""
    scope_paths = scope_paths or ['task.py']
    evidence = resolve(task, scope_paths=scope_paths, incremental_after_run=incremental_after_run)
    assert evidence['status'] == 'verified', evidence
    record = create_snapshot(task['repo'], base_ref=evidence['base_commit'], scope_paths=scope_paths,
                             requirements='隔离测试需求'.encode(), run_directory=run,
                             starting_point_evidence=evidence)
    starting_points.attach_task_review_run(task['repo'], 'task-1', run)
    record.update(status='completed', commit=git_text(task['repo'], 'rev-parse', 'HEAD'))
    write_json(run / 'run.json', record)
    write_json(run / 'validation.json', {'status': 'passed',
                                        'tested_tree': git_text(task['repo'], 'rev-parse', 'HEAD^{tree}')})
    return record


@pytest.mark.parametrize('file_existed_at_start', [True, False])
def test_external_rename_keeps_committed_task_discoverable(kanban_task, file_existed_at_start):
    task = kanban_task
    if file_existed_at_start:
        (task['main'] / 'task.py').write_text('value = 1\n' + '# 原有上下文\n' * 100)
        git(task['main'], 'commit', '-qam', 'test: 增加重命名相似度上下文')
        git(task['main'], 'worktree', 'remove', str(task['repo']))
        git(task['main'], 'worktree', 'add', '-q', '--detach', str(task['repo']), 'main')
        task['initial'] = git_text(task['repo'], 'rev-parse', 'HEAD')
        update_provenance(task, lambda entry: entry['incarnations'][0].update(
            initialBaseCommit=task['initial'], incarnationId=task['initial'] + ':1001'))
    original_path = 'task.py' if file_existed_at_start else 'new-task.py'
    (task['repo'] / original_path).write_text('value = 2\n' + '# 原有上下文\n' * 100)
    git(task['repo'], 'add', original_path)
    git(task['repo'], 'commit', '-qm', 'feat: 本任务修改')
    owned = git_text(task['repo'], 'rev-parse', 'HEAD')
    update_provenance(task, lambda entry: entry['incarnations'][0]['observedTaskCommits'].append(
        {'commitSha': owned, 'stablePatchId': None}))
    git(task['main'], 'merge', '--ff-only', owned)
    git(task['main'], 'mv', original_path, 'renamed-task.py')
    git(task['main'], 'commit', '-qm', 'refactor: 外部重命名')
    git(task['repo'], 'merge', '--ff-only', 'main')
    evidence = resolve(task)
    assert evidence['changed_paths'] == ['renamed-task.py']
    assert evidence['committed_work_status'] == 'present', evidence
    assert evidence['owned_changed_paths'] == ['renamed-task.py']
    assert 'value = 2' in (task['repo'] / 'renamed-task.py').read_text()
    assert stop(task)


def test_partial_reverted_ownership_retains_unknown_and_recovers(kanban_task):
    task = kanban_task
    known = commit_task(task)
    git(task['repo'], 'revert', '--no-edit', known)
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'feat: 尚未被 provenance 观察的新任务提交')
    unobserved = git_text(task['repo'], 'rev-parse', 'HEAD')
    git(task['repo'], 'reflog', 'expire', '--expire=now', '--all')
    evidence = resolve(task)
    assert evidence['status'] == 'verified'
    assert evidence['changed_paths'] == ['task.py']
    assert evidence['committed_work_status'] == 'unknown', evidence
    assert evidence['recovery_condition']
    assert stop(task) is None
    update_provenance(task, lambda entry: entry['incarnations'][0]['observedTaskCommits'].append(
        {'commitSha': unobserved, 'stablePatchId': None}))
    assert resolve(task)['committed_work_status'] == 'present'


@pytest.mark.parametrize('later_initial_matches', [True, False])
def test_old_first_checkpoint_cannot_bind_later_backfill_incarnation(kanban_task, later_initial_matches):
    task = kanban_task
    encoded = base64.urlsafe_b64encode(b'task-1').decode().rstrip('=')
    checkpoint = git_text(task['repo'], 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', input_bytes=b'checkpoint\n')
    git(task['repo'], 'update-ref', f'refs/kanban/checkpoints/{encoded}/turn/1', checkpoint)
    own = commit_task(task)
    later_initial = task['initial'] if later_initial_matches else own
    update_provenance(task, lambda entry: entry['incarnations'].append({
        'incarnationId': later_initial + ':2000', 'initialBaseCommit': later_initial,
        'captureSource': 'live_pre_handback_backfill', 'startedAt': 2000, 'observedTaskCommits': []}))
    evidence = resolve(task)
    assert evidence['status'] == 'unknown', evidence
    assert evidence['recovery_condition']


def test_recreated_card_rejects_old_incarnation_on_first_observation(kanban_task):
    task = kanban_task
    commit_task(task)
    board = task['workspace'] / 'board.json'
    document = read_json(board)
    document['columns'][0]['cards'][0]['createdAt'] = 2000
    write_json(board, document)
    assert resolve(task)['status'] == 'unknown'


def test_recreated_card_cannot_use_old_checkpoint_without_provenance(kanban_task):
    task = kanban_task
    encoded = base64.urlsafe_b64encode(b'task-1').decode().rstrip('=')
    checkpoint = git_text(task['repo'], 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', input_bytes=b'checkpoint\n')
    git(task['repo'], 'update-ref', f'refs/kanban/checkpoints/{encoded}/turn/1', checkpoint)
    (task['workspace'] / 'task-commit-integration-provenance.json').unlink()
    log = Path(git_text(task['repo'], 'rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD'))
    creation_seconds = int(log.read_text().splitlines()[0].split('\t')[0].split()[-2])
    board = task['workspace'] / 'board.json'
    document = read_json(board)
    document['columns'][0]['cards'][0]['createdAt'] = (creation_seconds + 5) * 1000
    write_json(board, document)
    assert resolve(task)['status'] == 'unknown'


def test_cached_project_alias_is_revalidated_before_verified_return(kanban_task, tmp_path, monkeypatch):
    task = kanban_task
    alias = tmp_path / 'project-alias'
    alias.symlink_to(task['main'], target_is_directory=True)
    monkeypatch.setenv('KANBAN_PROJECT_PATH', str(alias))
    assert resolve(task)['status'] == 'verified'
    unrelated = tmp_path / 'unrelated-project'
    unrelated.mkdir()
    git(unrelated, 'init', '-q')
    alias.unlink()
    alias.symlink_to(unrelated, target_is_directory=True)
    evidence = resolve(task, event={'prompt': '继续', 'turn_id': 'new-turn'})
    assert evidence['status'] == 'unknown', evidence
    assert 'common-dir' in evidence['diagnostic']


@pytest.mark.parametrize('corrupt_start_first', [True, False])
def test_verified_history_recovers_original_start_after_transient_unknown(kanban_task, corrupt_start_first):
    task = kanban_task
    own = commit_task(task)
    saved = starting_points.record_task_review_start(task['repo'], 'task-1', base_ref=own, scope_paths=['task.py'])
    marker = Path(saved['recovery_record_path']).with_name('starting-point.json')
    session = task['workspace'] / 'sessions.json'
    original_sessions = session.read_bytes()
    if corrupt_start_first:
        marker.write_text('{corrupt-start')
    session.write_text('{temporarily-broken-session')
    assert resolve(task)['status'] == 'unknown'
    session.write_bytes(original_sessions)
    marker.write_text('{corrupt-start')
    recovered = fresh_process_resolution(task)
    assert recovered['status'] == 'verified', recovered
    assert recovered['base_commit'] == own
    assert recovered['recorded_scope_paths'] == ['task.py']
    assert list(marker.parent.glob('starting-point.corrupt-*.json'))


def test_identical_native_user_prompts_retry_restored_git_object_once(kanban_task, tmp_path):
    task = kanban_task
    commit_task(task)
    transcript = tmp_path / 'native-transcript.jsonl'
    user_record = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '继续'}}
    transcript.write_text(json.dumps(user_record) + '\n')
    event = {'prompt': '继续', 'session_id': 'native-session', 'transcript_path': str(transcript)}
    common = Path(git_text(task['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    original_object = common / 'objects' / task['initial'][:2] / task['initial'][2:]
    saved_bytes = original_object.read_bytes()
    original_object.unlink()
    try:
        missing = resolve(task, event=event)
        assert missing['status'] == 'unknown'
        with transcript.open('a') as handle:
            handle.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '处理中'}}) + '\n')
        assert resolve(task, event=event)['resolution_attempt_count'] == missing['resolution_attempt_count']
    finally:
        original_object.write_bytes(saved_bytes)
    with transcript.open('a') as handle:
        handle.write(json.dumps(user_record) + '\n')
    recovered = resolve(task, event=event)
    assert recovered['status'] == 'verified', recovered
    assert recovered['resolution_attempt_count'] == missing['resolution_attempt_count'] + 1
    assert resolve(task, event=event)['resolution_attempt_count'] == recovered['resolution_attempt_count']

@pytest.mark.parametrize('custom_home', [True, False])
def test_kimi_equal_native_prompts_recover_once_per_submitted_message(kanban_task, tmp_path, monkeypatch, custom_home):
    task = kanban_task
    commit_task(task)
    home = tmp_path / 'kimi-home' if custom_home else Path.home() / '.kimi-code'
    if custom_home:
        monkeypatch.setenv('KIMI_CODE_HOME', str(home))
    else:
        monkeypatch.delenv('KIMI_CODE_HOME', raising=False)
    session_id = 'session_native-equal-prompts'
    journal = home / 'server/events' / f'{session_id}.jsonl'
    journal.parent.mkdir(parents=True)
    event = {'hook_event_name': 'UserPromptSubmit', 'session_id': session_id, 'cwd': str(task['repo']),
             'client_type': 'kimi_code_cli', 'prompt': [{'type': 'text', 'text': '继续'}], 'is_steer': False}
    adapter = resolve_hook_host_adapter('kimi_code')

    def append_event(payload):
        with journal.open('a') as handle:
            handle.write(json.dumps({'kind': 'event', 'envelope': {
                'session_id': session_id, 'type': payload['type'], 'payload': payload}}) + '\n')

    def submit(prompt_id):
        append_event({'type': 'prompt.submitted', 'agentId': 'main', 'sessionId': session_id,
                      'promptId': prompt_id, 'userMessageId': prompt_id, 'status': 'running',
                      'content': event['prompt']})

    def prompt():
        assert handle_hook(event, event_name='UserPromptSubmit', host_adapter=adapter) is None
        return resolve(task)

    common = Path(git_text(task['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    original_object = common / 'objects' / task['initial'][:2] / task['initial'][2:]
    saved_bytes = original_object.read_bytes()
    original_object.unlink()
    try:
        submit('msg_first')
        missing = prompt()
        assert missing['status'] == 'unknown'
        append_event({'type': 'turn.step.completed', 'agentId': 'main'})
        assert prompt()['resolution_attempt_count'] == missing['resolution_attempt_count']
        assert stop(task, 'kimi_code') is None
    finally:
        original_object.write_bytes(saved_bytes)
    # 对象恢复不改变 HEAD/reflog/来源观察；同一条 hook 仍然不能重验。
    assert prompt()['resolution_attempt_count'] == missing['resolution_attempt_count']
    # 记录缺失/损坏、其他 agent/session、通知、排队项和不匹配文本都不是新的真实回合。
    journal.unlink()
    assert prompt()['resolution_attempt_count'] == missing['resolution_attempt_count']
    journal.write_text('{partial-record\n')
    invalid_submissions = [
        {'agentId': 'subagent'},
        {'sessionId': 'another-session'},
        {'type': 'context.spliced'},
        {'status': 'queued'},
        {'content': [{'type': 'text', 'text': '其他文本'}]},
    ]
    for override in invalid_submissions:
        append_event({'type': 'prompt.submitted', 'agentId': 'main', 'sessionId': session_id,
                      'promptId': 'msg_not_current', 'status': 'running', 'content': event['prompt'], **override})
        assert prompt()['resolution_attempt_count'] == missing['resolution_attempt_count']
    submit('msg_second')
    recovered = prompt()
    assert recovered['status'] == 'verified', recovered
    assert recovered['base_commit'] == task['initial']
    assert recovered['committed_work_status'] == 'present'
    assert recovered['resolution_attempt_count'] == missing['resolution_attempt_count'] + 1
    assert prompt()['resolution_attempt_count'] == recovered['resolution_attempt_count']
    assert stop(task, 'kimi_code')['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert stop(task, 'kimi_code') is None



def test_native_rename_patch_id_survives_message_only_amend(kanban_task):
    task = kanban_task
    git(task['repo'], 'mv', 'background.txt', 'renamed-background.txt')
    git(task['repo'], 'commit', '-qm', 'refactor: 任务重命名')
    original = git_text(task['repo'], 'rev-parse', 'HEAD')
    patch_id = git_text(task['repo'], 'patch-id', '--stable', input_bytes=git(task['repo'], 'show', original)).split()[0]
    update_provenance(task, lambda entry: entry['incarnations'][0]['observedTaskCommits'].append(
        {'commitSha': original, 'stablePatchId': patch_id}))
    git(task['repo'], 'commit', '--amend', '-qm', 'refactor: 仅改提交说明')
    evidence = resolve(task)
    assert evidence['status'] == 'verified', evidence
    assert evidence['task_owned_commit_shas'] == [git_text(task['repo'], 'rev-parse', 'HEAD')]


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_automatic_default_scope_suppresses_only_covered_current_content(kanban_task, tmp_path, host):
    task = kanban_task
    assert handle_hook({'cwd': str(task['repo']), 'prompt': '实施任务'}, event_name='UserPromptSubmit',
                       host_adapter=resolve_hook_host_adapter(host)) is None
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'feat: 完成窄范围实现')
    freeze_and_complete_run_without_reviewers(task, tmp_path / 'completed-narrow-run')
    evidence = resolve(task)
    assert evidence['scope_paths'] == ['.']
    assert evidence['current_scope_reviewed'], evidence
    assert stop(task, host) is None
    (task['repo'] / 'unreviewed-task-path.txt').write_text('新的未审任务内容\n')
    git(task['repo'], 'add', '.')
    git(task['repo'], 'commit', '-qm', 'feat: 增加范围外未审路径')
    assert not resolve(task)['current_scope_reviewed']
    assert stop(task, host)


def test_completed_incremental_run_is_associated_without_advancing_default_start(kanban_task, tmp_path):
    task = kanban_task
    starting_points.record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'], scope_paths=['task.py'])
    (task['repo'] / 'task.py').write_text('value = 2\n')
    git(task['repo'], 'commit', '-qam', 'feat: 第一段实现')
    first = tmp_path / 'first-completed-run'
    freeze_and_complete_run_without_reviewers(task, first)
    assert stop(task) is None
    (task['repo'] / 'task.py').write_text('value = 3\n')
    git(task['repo'], 'commit', '-qam', 'feat: 后续增量实现')
    second = tmp_path / 'second-completed-run'
    freeze_and_complete_run_without_reviewers(task, second, incremental_after_run=first)
    evidence = fresh_process_resolution(task)
    assert evidence['base_commit'] == task['initial']
    assert evidence['fixed_run_directory'] == str(second), evidence
    assert evidence['current_scope_reviewed'], evidence
    assert stop(task) is None


def test_narrow_completed_run_does_not_cover_unreviewed_rename_source_deletion(kanban_task, tmp_path):
    task = kanban_task
    resolve(task)
    git(task['repo'], 'mv', 'task.py', 'renamed-task.py')
    git(task['repo'], 'commit', '-qm', 'refactor: 本任务重命名')
    freeze_and_complete_run_without_reviewers(task, tmp_path / 'destination-only-completed-run',
                                             scope_paths=['renamed-task.py'])
    evidence = resolve(task)
    assert not evidence['current_scope_reviewed'], evidence
    assert stop(task)


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('failure', ['repository_identity', 'state_write'])
def test_machine_evidence_failures_preserve_explicit_prompt_context(kanban_task, monkeypatch, capsys, host, failure):
    task = kanban_task
    if failure == 'repository_identity':
        invalid_cwd = task['repo'] / 'missing-repository'
    else:
        invalid_cwd = task['repo']
        def cannot_write(*arguments, **keywords):
            raise OSError('隔离注入的机器写入失败')
        monkeypatch.setattr(starting_points, 'write_json', cannot_write)
    adapter = resolve_hook_host_adapter(host)
    event = {'cwd': str(invalid_cwd), 'prompt': '/review-validate-fix'}
    payload = handle_hook(event, event_name='UserPromptSubmit', host_adapter=adapter)
    assert payload
    assert 'decision' not in payload
    assert handle_hook({**event, 'prompt': '继续'}, event_name='UserPromptSubmit', host_adapter=adapter) is None
    assert not capsys.readouterr().out


def rebuild_worktree(task, destination):
    git(task['main'], 'worktree', 'remove', str(task['repo']))
    git(task['main'], 'worktree', 'add', '-q', '--detach', str(destination), 'main')
    task['repo'] = destination
    write_json(task['workspace'] / 'sessions.json', {'task-1': {'taskId': 'task-1', 'workspacePath': str(destination)}})


def append_creation_incarnation(task, *, keep_history=True):
    initial = git_text(task['repo'], 'rev-parse', 'HEAD')
    def mutate(entry):
        incarnation = {'incarnationId': initial + ':3000', 'initialBaseCommit': initial,
                       'captureSource': 'worktree_creation', 'startedAt': 3000, 'observedTaskCommits': []}
        entry['incarnations'] = entry['incarnations'] + [incarnation] if keep_history else [incarnation]
        entry['lastWorkspaceGitSnapshot'] = {'path': str(task['repo'])}
    update_provenance(task, mutate)


@pytest.mark.parametrize('different_path', [False, True])
def test_new_worktree_creation_evidence_recovers_same_card_without_losing_original_window(kanban_task, tmp_path, different_path):
    task = kanban_task
    owned = commit_task(task)
    original = resolve(task)
    git(task['main'], 'merge', '--ff-only', owned)
    destination = tmp_path / 'rebuilt-task-worktree' if different_path else task['repo']
    rebuild_worktree(task, destination)
    assert fresh_process_resolution(task)['status'] == 'unknown'
    append_creation_incarnation(task)
    recovered = fresh_process_resolution(task)
    assert recovered['status'] == 'verified', recovered
    assert recovered['base_commit'] == task['initial']
    assert recovered['committed_work_status'] == 'present'
    assert owned in recovered['task_owned_commit_shas']
    assert recovered['recovery_actions']
    assert Path(recovered['recovery_actions'][0]['preserved_original']).exists()
    assert read_json(Path(original['recovery_record_path']).parent / 'resolution-attempts' /
                     f"{original['resolution_attempt_count']}-{original['source_observation_sha256']}.json")['status'] == 'verified'
    assert stop(task)


@pytest.mark.parametrize('missing_continuity', ['old_head_not_carried', 'previous_incarnation_missing'])
def test_new_worktree_does_not_rebind_without_positive_continuity(kanban_task, missing_continuity):
    task = kanban_task
    owned = commit_task(task)
    resolve(task)
    if missing_continuity != 'old_head_not_carried':
        git(task['main'], 'merge', '--ff-only', owned)
    rebuild_worktree(task, task['repo'])
    append_creation_incarnation(task, keep_history=missing_continuity != 'previous_incarnation_missing')
    assert fresh_process_resolution(task)['status'] == 'unknown'


def test_moving_existing_worktree_and_changing_incarnation_does_not_prove_recreation(kanban_task, tmp_path):
    task = kanban_task
    saved = resolve(task)
    marker = Path(saved['recovery_record_path']).with_name('starting-point.json')
    original_marker = marker.read_bytes()
    destination = tmp_path / 'moved-existing-task-worktree'
    git(task['main'], 'worktree', 'move', str(task['repo']), str(destination))
    task['repo'] = destination
    write_json(task['workspace'] / 'sessions.json', {'task-1': {'taskId': 'task-1', 'workspacePath': str(destination)}})
    append_creation_incarnation(task)
    assert fresh_process_resolution(task)['status'] == 'unknown'
    assert marker.read_bytes() == original_marker


def test_new_card_with_positive_new_creation_uses_independent_window(kanban_task):
    task = kanban_task
    owned = commit_task(task)
    old = resolve(task)
    git(task['main'], 'merge', '--ff-only', owned)
    rebuild_worktree(task, task['repo'])
    append_creation_incarnation(task)
    board = task['workspace'] / 'board.json'
    document = read_json(board)
    document['columns'][0]['cards'][0]['createdAt'] = 2000
    write_json(board, document)
    current = fresh_process_resolution(task)
    assert current['status'] == 'verified', current
    assert current['base_commit'] == owned
    assert current['task_owned_commit_shas'] == []
    assert current['committed_work_status'] == 'absent'
    assert current['recovery_actions']
    assert Path(current['recovery_actions'][0]['preserved_original']).exists()
    assert current['binding']['task_created_at'] != old['binding']['task_created_at']
