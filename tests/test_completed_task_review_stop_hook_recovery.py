"""使用真实 Git 快照验证完成覆盖、缺失关联补证与跨进程去重。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters import resolve_hook_host_adapter
from adapters.cline_kanban import task_review_starting_point as starting_points
from core.task_snapshot_review.hook_entrypoint import handle_hook
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import capture_worktree_tree, create_snapshot
from test_kanban_task_review_starting_point import kanban_task, commit_task, repository, resolve, ROOT
from test_task_snapshot_review_workflow import canary


def task_stop_hook_payload(task, host='codex', *, session='original-session'):
    return handle_hook({'cwd': str(task['repo']), 'session_id': session}, event_name='Stop',
                       host_adapter=resolve_hook_host_adapter(host))


def task_stop_hook_payload_from_fresh_process(task, host='codex'):
    program = (
        'import json,sys; from adapters import resolve_hook_host_adapter; '
        'from core.task_snapshot_review.hook_entrypoint import handle_hook; '
        'print(json.dumps(handle_hook({"cwd":sys.argv[1],"session_id":"restarted-session"}, '
        'event_name="Stop",host_adapter=resolve_hook_host_adapter(sys.argv[2]))))'
    )
    result = subprocess.run([sys.executable, '-c', program, str(task['repo']), host],
                            cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def create_completed_task_snapshot_with_real_git_evidence(task, run, *, linked=True, background=False, fixed=False):
    """只替代评审结果，实际创建原始观察、最终候选、提交与验证树。"""
    resolve(task)
    (task['repo'] / 'task.py').write_text('value = 2\n')
    if background:
        (task['repo'] / 'background.txt').write_text('暂存背景\n')
        git(task['repo'], 'add', 'background.txt')
        (task['repo'] / 'background.txt').write_text('未暂存背景\n')
        (task['repo'] / 'background-untracked.txt').write_text('未跟踪背景\n')
    evidence = resolve(task, scope_paths=['task.py']) if linked else None
    record = create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'],
                             requirements='隔离回归需求'.encode(), run_directory=run,
                             starting_point_evidence=evidence)
    if linked:
        starting_points.attach_task_review_run(task['repo'], 'task-1', run)
    if fixed:
        (task['repo'] / 'task.py').write_text('value = 3\n')
        tree = capture_worktree_tree(task['repo'], ['task.py'], run)
        record['validated_candidate_commit'] = git_text(task['repo'], 'commit-tree', tree,
            '-p', record['original_head'], input_bytes=b'RVF repaired candidate\n')
        record['effective_scope_paths'] = ['task.py']
    git(task['repo'], 'commit', '--only', '-qm', 'fix: 提交隔离任务', '--', 'task.py')
    record.update(status='completed', commit=git_text(task['repo'], 'rev-parse', 'HEAD'))
    write_json(run / 'run.json', record)
    write_json(run / 'validation.json', {'status': 'passed',
        'candidate_commit': record.get('validated_candidate_commit', record['candidate_commit']),
        'tested_head': record['commit'], 'tested_tree': git_text(task['repo'], 'rev-parse', 'HEAD^{tree}')})
    return record


@pytest.mark.parametrize('baseline', ['matching', 'different', 'kanban'])
@pytest.mark.parametrize('binding_source', ['environment', 'cli_argument'])
def test_cli_associates_only_the_verified_fixed_baseline(kanban_task, tmp_path, monkeypatch, baseline, binding_source):
    task = kanban_task
    own = commit_task(task)
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('原始需求')
    run = tmp_path / 'cli-snapshot-run'
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    selection = ['--base-from-kanban-task', 'task-1'] if baseline == 'kanban' else [
        '--base-ref', own if baseline == 'different' else task['initial']]
    if binding_source == 'cli_argument':
        monkeypatch.delenv('KANBAN_TASK_ID')
        selection += ['--kanban-task-id', 'task-1']
    result = subprocess.run([sys.executable, str(entry), 'review-task-snapshot', '--repo', str(task['repo']),
        *selection, '--scope', '.', '--requirements-file', str(requirements),
        '--run-directory', str(run), '--prepare-only'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    record = read_json(run / 'run.json')
    assert record['base_commit'] == (own if baseline == 'different' else task['initial'])
    assert ('starting_point_evidence' in record) == (baseline != 'different')
    evidence = resolve(task)
    assert evidence['base_commit'] == task['initial']
    if baseline != 'different':
        assert evidence['fixed_run_directory'] == str(run)


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_same_content_is_not_a_new_decision_after_staging_or_commit(kanban_task, host):
    task = kanban_task
    (task['repo'] / 'task.py').write_text('value = 2\n')
    (task['repo'] / 'new-task-path.txt').write_text('新内容\n')
    assert task_stop_hook_payload(task, host)
    git(task['repo'], 'add', '.')
    assert task_stop_hook_payload(task, host, session='another-session') is None
    git(task['repo'], 'commit', '-qm', 'feat: 提交相同内容')
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
    (task['repo'] / 'task.py').write_text('value = 4\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('fixed', [False, True])
def test_completed_run_preserves_unchanged_background_wip(kanban_task, tmp_path, host, fixed):
    task = kanban_task
    create_completed_task_snapshot_with_real_git_evidence(task, tmp_path / 'linked-completed-run', background=True, fixed=fixed)
    before = canary(task['repo'])
    assert task_stop_hook_payload(task, host) is None
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
    assert resolve(task)['current_scope_reviewed']
    assert canary(task['repo']) == before
    (task['repo'] / 'background-untracked.txt').write_text('真正新增的范围外改动\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None


@pytest.mark.parametrize('background', [False, True])
def test_missing_association_recovers_without_rewriting_frozen_run(kanban_task, tmp_path, background):
    task = kanban_task
    run = tmp_path / 'unassociated-completed-run'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False, background=background, fixed=True)
    original_run = (run / 'run.json').read_bytes()
    original_validation = (run / 'validation.json').read_bytes()
    assert task_stop_hook_payload_from_fresh_process(task) is None
    evidence = resolve(task)
    assert evidence['current_scope_reviewed'], evidence
    assert evidence['fixed_run_directory'] == str(run)
    assert evidence['completion_evidence_status'] == 'verified'
    assert Path(evidence['completed_run_association_path']).is_file()
    assert (run / 'run.json').read_bytes() == original_run
    assert (run / 'validation.json').read_bytes() == original_validation
    assert evidence['base_commit'] == task['initial']
    (task['repo'] / 'task.py').write_text('value = 5\n')
    assert task_stop_hook_payload(task)
    assert task_stop_hook_payload(task) is None


@pytest.mark.parametrize('damage', ['validation_tree', 'original_observation', 'task_identity', 'baseline',
                                  'worktree', 'incarnation', 'scope', 'ancestry'])
def test_unprovable_completion_is_durable_unknown_without_agent_output(kanban_task, tmp_path, damage, capsys):
    task = kanban_task
    run = tmp_path / 'unprovable-completed-run'
    record = create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False)
    if damage == 'validation_tree':
        validation = read_json(run / 'validation.json')
        validation['tested_tree'] = '0' * 40
        write_json(run / 'validation.json', validation)
    elif damage == 'original_observation':
        record['original_worktree_observation_commit'] = record['candidate_commit']
        write_json(run / 'run.json', record)
    elif damage == 'baseline':
        record['base_commit'] = record['commit']
        write_json(run / 'run.json', record)
    elif damage == 'worktree':
        record['repo'] = str(tmp_path / 'different-worktree')
        write_json(run / 'run.json', record)
    elif damage == 'incarnation':
        path = task['workspace'] / 'task-commit-integration-provenance.json'
        document = read_json(path)
        document['tasks']['task-1']['incarnations'][0].update(
            startedAt=3000, incarnationId=task['initial'] + ':3000')
        write_json(path, document)
    elif damage == 'scope':
        record['scope_paths'] = ['background.txt']
        write_json(run / 'run.json', record)
    elif damage == 'ancestry':
        record['commit'] = git_text(task['repo'], 'commit-tree', 'HEAD^{tree}', input_bytes=b'unrelated history\n')
        write_json(run / 'run.json', record)
    else:
        board = task['workspace'] / 'board.json'
        document = read_json(board)
        document['columns'][0]['cards'][0]['createdAt'] = record['created_at'] * 1000 + 1000
        write_json(board, document)
    assert task_stop_hook_payload(task) is None
    evidence = resolve(task)
    assert evidence.get('completion_evidence_status') == 'unknown' or evidence['status'] == 'unknown'
    assert not evidence.get('current_scope_reviewed')
    assert evidence['recovery_condition']
    attempts = evidence['resolution_attempt_count']
    assert task_stop_hook_payload_from_fresh_process(task) is None
    assert resolve(task)['resolution_attempt_count'] == attempts
    assert not capsys.readouterr().out


@pytest.mark.parametrize('interrupted_artifact', ['supplemental_association', 'fixed_run_pointer'])
def test_interrupted_association_write_recovers_on_next_real_prompt(kanban_task, tmp_path, monkeypatch, interrupted_artifact):
    task = kanban_task
    run = tmp_path / 'interrupted-association-run'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False)
    original_run = (run / 'run.json').read_bytes()
    original_write = starting_points.write_json

    def interrupted_write(path, document):
        if ((interrupted_artifact == 'supplemental_association' and path.parent.name == 'completed-run-associations')
                or (interrupted_artifact == 'fixed_run_pointer' and path.name == 'starting-point.json'
                    and document.get('fixed_run_directory') == str(run))):
            raise OSError('隔离注入的关联写入中断')
        return original_write(path, document)

    monkeypatch.setattr(starting_points, 'write_json', interrupted_write)
    assert task_stop_hook_payload(task) is None
    unknown = resolve(task)
    assert unknown['recovery_condition']
    monkeypatch.setattr(starting_points, 'write_json', original_write)
    assert resolve(task)['resolution_attempt_count'] == unknown['resolution_attempt_count']
    recovered = resolve(task, event={'prompt': '继续', 'turn_id': 'next-real-prompt'})
    assert recovered['current_scope_reviewed'], recovered
    assert task_stop_hook_payload_from_fresh_process(task) is None
    assert (run / 'run.json').read_bytes() == original_run


def test_missing_git_object_is_retried_once_per_real_prompt(kanban_task, tmp_path):
    task = kanban_task
    run = tmp_path / 'missing-observation-object-run'
    record = create_completed_task_snapshot_with_real_git_evidence(task, run)
    common = Path(record['git_common_directory'])
    object_id = record['original_worktree_observation_commit']
    object_path = common / 'objects' / object_id[:2] / object_id[2:]
    object_bytes = object_path.read_bytes()
    object_path.unlink()
    try:
        assert task_stop_hook_payload(task) is None
        unknown = resolve(task)
        assert unknown['completion_evidence_status'] == 'unknown'
    finally:
        object_path.write_bytes(object_bytes)
    assert resolve(task)['completion_evidence_status'] == 'unknown'
    recovered = resolve(task, event={'prompt': '继续', 'turn_id': 'new-native-message'})
    assert recovered['current_scope_reviewed'], recovered
    assert resolve(task)['resolution_attempt_count'] == recovered['resolution_attempt_count']
    assert task_stop_hook_payload(task) is None


def test_committing_unchanged_background_observation_does_not_create_a_decision(kanban_task, tmp_path):
    task = kanban_task
    create_completed_task_snapshot_with_real_git_evidence(task, tmp_path / 'completed-with-background-observation', background=True)
    assert task_stop_hook_payload(task) is None
    git(task['repo'], 'add', 'background.txt', 'background-untracked.txt')
    git(task['repo'], 'commit', '-qm', 'test: 原样提交已观察的背景内容')
    assert task_stop_hook_payload(task) is None


def test_newer_registered_completion_recovers_when_old_run_pointer_remains(kanban_task, tmp_path):
    task = kanban_task
    first = tmp_path / 'previously-linked-completed-run'
    create_completed_task_snapshot_with_real_git_evidence(task, first)
    run = tmp_path / 'newer-unassociated-completed-run'
    (task['repo'] / 'task.py').write_text('value = 4\n')
    record = create_snapshot(task['repo'], base_ref=task['initial'], scope_paths=['task.py'],
                             requirements='新一轮实现'.encode(), run_directory=run)
    git(task['repo'], 'commit', '--only', '-qm', 'fix: 新一轮已评审内容', '--', 'task.py')
    record.update(status='completed', commit=git_text(task['repo'], 'rev-parse', 'HEAD'))
    write_json(run / 'run.json', record)
    write_json(run / 'validation.json', {'status': 'passed',
        'tested_tree': git_text(task['repo'], 'rev-parse', 'HEAD^{tree}')})
    assert task_stop_hook_payload_from_fresh_process(task) is None
    evidence = resolve(task)
    assert evidence['fixed_run_directory'] == str(run), evidence
    assert evidence['current_scope_reviewed']


def test_explicit_scope_expansion_remains_a_new_decision(kanban_task, tmp_path):
    task = kanban_task
    create_completed_task_snapshot_with_real_git_evidence(task, tmp_path / 'completed-narrow-scope-run')
    assert task_stop_hook_payload(task) is None
    starting_points.record_task_review_start(task['repo'], 'task-1', base_ref=task['initial'],
                                            scope_paths=['task.py', 'background.txt'])
    assert not resolve(task)['current_scope_reviewed']
    assert task_stop_hook_payload(task)
    assert task_stop_hook_payload(task) is None


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
@pytest.mark.parametrize('completion_provable', [True, False])
def test_real_hook_entrypoint_keeps_completion_and_diagnostics_off_stdout(kanban_task, tmp_path, host, completion_provable):
    task = kanban_task
    run = tmp_path / 'entrypoint-completed-run'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False, background=True, fixed=True)
    validation_bytes = (run / 'validation.json').read_bytes()
    if not completion_provable:
        validation = read_json(run / 'validation.json')
        validation['tested_tree'] = '0' * 40
        write_json(run / 'validation.json', validation)
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/task_snapshot_review_hook.py'

    def invoke():
        return subprocess.run([sys.executable, str(entry), '--host', host],
            input=json.dumps({'cwd': str(task['repo'])}), text=True, capture_output=True)

    first = invoke()
    assert first.returncode == 0
    assert not first.stdout
    if not completion_provable:
        assert '诊断' in first.stderr
        unknown = resolve(task)
        (run / 'validation.json').write_bytes(validation_bytes)
        assert not invoke().stdout
        assert resolve(task)['resolution_attempt_count'] == unknown['resolution_attempt_count'] + 1
        assert resolve(task)['current_scope_reviewed']
    (task['repo'] / 'new-unreviewed-path.txt').write_text('真正新增的范围\n')
    decision = invoke()
    assert decision.returncode == 0
    payload = json.loads(decision.stdout)
    if host == 'codex':
        assert payload['decision'] == 'block'
    elif host == 'claude_code':
        assert payload['hookSpecificOutput']['additionalContext']
    else:
        assert payload['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert not invoke().stdout


@pytest.mark.parametrize('host', ['codex', 'claude_code', 'kimi_code'])
def test_unproven_historical_completion_does_not_suppress_later_real_content(kanban_task, tmp_path, host):
    task = kanban_task
    run = tmp_path / 'unproven-history-with-new-content'
    create_completed_task_snapshot_with_real_git_evidence(task, run, linked=False)
    validation = read_json(run / 'validation.json')
    validation['tested_tree'] = '0' * 40
    write_json(run / 'validation.json', validation)
    assert task_stop_hook_payload(task, host) is None
    assert resolve(task)['completion_evidence_status'] == 'unknown'
    (task['repo'] / 'task.py').write_text('value = 8\n')
    assert task_stop_hook_payload(task, host)
    assert task_stop_hook_payload(task, host) is None
    assert resolve(task)['completion_evidence_status'] == 'unknown'
    git(task['repo'], 'commit', '-qam', 'fix: 历史缺证后的独立新内容')
    assert task_stop_hook_payload_from_fresh_process(task, host) is None
