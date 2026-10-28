"""用事故捕获来源验收新版自动入口；不读取真实 tracker 或修改来源仓库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

INITIAL = 'fc25ab76176ad34865cbbacfe421b83577a75606'
CANDIDATE = '55ea63d9b218449378dd2b1f0a9167fc04287ac7'
CAPTURED_PROVENANCE_SHA256 = '13bd9f598e79a32fcf676b92fa900a50a0d1579bdc305a53bba7919f50a34455'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-repository', type=Path, required=True)
    parser.add_argument('--captured-provenance', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw = args.captured_provenance.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CAPTURED_PROVENANCE_SHA256
    captured = json.loads(raw)
    assert captured['incarnations'][-1]['initialBaseCommit'] == INITIAL
    (output / 'unaltered-source-provenance.json').write_bytes(raw)
    product = Path(__file__).resolve().parents[3]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('KANBAN_', 'CLINE_KANBAN_', 'RVF_', 'GIT_'))}
    environment.update(HOME=str(output / 'isolated-home'), RVF_USER_LOCAL_DATA_ROOT=str(output / 'isolated-rvf-materials'))
    commands = []

    def execute(argv: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> str:
        result = subprocess.run(argv, cwd=cwd, input=input_text, env=environment, capture_output=True, text=True)
        commands.append({'argv': argv, 'cwd': str(cwd) if cwd else None, 'exit_code': result.returncode,
                         'stdout': result.stdout, 'stderr': result.stderr})
        (output / 'command-results.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(commands[-1])
        return result.stdout.strip()

    def git(repo: Path, *arguments: str) -> str:
        return execute(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *arguments])

    repo = output / 'isolated-main-repository'; repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'RVF 事故隔离验收')
    git(repo, 'config', 'user.email', 'incident-replay@example.invalid')
    git(repo, 'fetch', '--quiet', '--no-tags', str(args.source_repository.resolve()), CANDIDATE)
    git(repo, 'reset', '--hard', INITIAL)
    task = output / 'isolated-task-worktree'
    git(repo, 'worktree', 'add', '--detach', str(task), INITIAL)
    git(repo, 'reset', '--hard', CANDIDATE)
    git(task, 'reset', '--hard', CANDIDATE)
    workspace = output / 'isolated-kanban-state/workspaces/incident-project'; workspace.mkdir(parents=True)
    entry = json.loads(raw); entry['lastWorkspaceGitSnapshot']['path'] = str(task)
    (workspace / 'task-commit-integration-provenance.json').write_text(json.dumps({'schemaVersion': 1, 'tasks': {'9895b': entry}}))
    (workspace / 'sessions.json').write_text(json.dumps({'9895b': {'taskId': '9895b', 'workspacePath': str(task)}}))
    (workspace / 'board.json').write_text(json.dumps({'columns': [{'id': 'in_progress', 'cards': [{
        'id': '9895b', 'baseRef': 'main', 'createdAt': captured['incarnations'][0]['startedAt']}]}]}))
    environment.update(KANBAN_TASK_ID='9895b', KANBAN_PROJECT_PATH=str(repo),
                       RVF_CLINE_KANBAN_STATE_DIRECTORY=str(workspace.parents[1]))
    scripts = product / 'plugins/review-validate-fix/skills/review-validate-fix/scripts'
    requirements = output / 'requirements.md'
    requirements.write_text('原事故自动起点验收：已提交且进入 main 的 10 个任务文件仍可见。不启动 reviewer。\n')
    expected = git(task, 'diff', '--name-only', INITIAL, CANDIDATE).splitlines()
    assert len(expected) == 10

    def prepare(name: str, baseline_arguments: list[str]) -> dict:
        run = output / name
        argv = [sys.executable, str(scripts / 'rvf_command_line_shortcut_entrypoint.py'), 'review-task-snapshot',
                '--repo', str(task), *baseline_arguments, '--requirements-file', str(requirements),
                '--run-directory', str(run), '--prepare-only']
        for path in expected:
            argv += ['--scope', path]
        execute(argv)
        return json.loads((run / 'run.json').read_text())

    control = prepare('explicit-live-main-control', ['--base-ref', 'main'])
    automatic = prepare('automatic-kanban-start', ['--base-from-kanban-task', '9895b'])
    assert control['changed_paths'] == []
    assert automatic['base_commit'] == INITIAL and automatic['changed_paths'] == expected
    decision = json.loads(execute([sys.executable, str(scripts / 'task_snapshot_review_hook.py'), '--host', 'codex'],
        input_text=json.dumps({'cwd': str(task), 'session_id': 'isolated-session-1'})))
    assert decision['decision'] == 'block'
    repeated = execute([sys.executable, str(scripts / 'task_snapshot_review_hook.py'), '--host', 'codex'],
        input_text=json.dumps({'cwd': str(task), 'session_id': 'isolated-session-2'}))
    assert not repeated
    frozen = (output / 'automatic-kanban-start/run.json').read_bytes()
    (repo / 'external-after-incident.txt').write_text('外部工作\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'test: base 继续前移')
    git(task, 'merge', '--ff-only', 'main')
    advanced = prepare('automatic-start-after-base-advance', ['--base-from-kanban-task', '9895b'])
    assert advanced['base_commit'] == INITIAL and advanced['changed_paths'] == expected
    assert (output / 'automatic-kanban-start/run.json').read_bytes() == frozen
    summary = {'assertion_kind': 'post_overhaul_fix_acceptance', 'passed': True,
               'captured_provenance_sha256': CAPTURED_PROVENANCE_SHA256,
               'source_window': {'initial': INITIAL, 'candidate': CANDIDATE},
               'identity_rebinding': '只重建隔离 board/session/worktree 路径；提交身份和捕获起点逐字保留',
               'live_main_control_count': len(control['changed_paths']), 'automatic_candidate_count': len(automatic['changed_paths']),
               'changed_paths': automatic['changed_paths'], 'automatic_base_commit': automatic['base_commit'],
               'clean_stop_decision': decision['decision'], 'repeated_stop_output': repeated,
               'frozen_run_unchanged_after_base_advance': True, 'external_review_dispatched': False,
               'original_source_refs_modified': False, 'baseline_override_used_for_automatic_entry': False}
    (output / 'fix-acceptance-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
