"""健康角色不受墙钟或静默时长影响；观察和显式取消保持独立。"""
import itertools
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from core.task_snapshot_review import role_process_execution
from core.task_snapshot_review.persistent_artifacts import git, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review, _start_role
from test_task_snapshot_review_workflow import repository


@pytest.mark.parametrize('role', ['reviewer', 'fixer'])
@pytest.mark.parametrize('heartbeats', [False, True])
def test_healthy_role_completes_after_virtual_900_and_1800_second_thresholds(repository, tmp_path, monkeypatch, role, heartbeats):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'unlimited-agent-execution'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'complete full role', run_directory=run)
    script = tmp_path / 'healthy-role.py'
    script.write_text(
        'import json,sys,time\n'
        + ('print("持续输出", file=sys.stderr, flush=True)\n' if heartbeats else '')
        + 'time.sleep(0.3)\n'
        + 'print(json.dumps(' + repr({'status': 'completed', 'findings': [], 'audit_summary': '完成审阅'} if role == 'reviewer'
                                    else {'status': 'completed', 'dispositions': []}) + '))\n')
    role_directory = run / ('reviewer-1' if role == 'reviewer' else 'fixer')
    monkeypatch.setattr('core.task_snapshot_review.workflow.launch_role', lambda *args, **kwargs: None)
    _start_role(run, read_json(run / 'run.json'), role_directory, role,
                [{'command': [sys.executable, str(script)], 'output_format': 'text'}], [])
    attempt = role_directory / 'attempt-001'
    wall_clock = itertools.count(time.time(), 2000)
    monkeypatch.setattr(role_process_execution, 'time', SimpleNamespace(time=lambda: next(wall_clock)))
    # 只模拟角色的墙钟前进；真实子进程与等待仍按真实时间运行。
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    role_process_execution.execute_role(attempt)
    outcome = read_json(attempt / 'outcome.json')
    assert outcome['status'] == 'completed'
    assert outcome['returncode'] == 0
    assert outcome['finished_at'] - outcome['started_at'] > 1800
    assert not (role_directory / 'role-deadline.json').exists()
    assert '收束时间' not in (attempt / 'prompt.md').read_text()
    progress = read_json(attempt / 'progress.json')
    assert progress['last_observed_at']
    assert progress['process_identity']['pid'] == progress['process_pid']
    assert progress['status'] == 'completed'
    assert progress['last_output_activity_at'] is not None


def test_legacy_timeout_configuration_cannot_reenable_total_runtime_cap(repository, tmp_path):
    from test_task_snapshot_review_workflow import fake_config
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'removed-timeout-configuration'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'no runtime cap', run_directory=run)
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[{**config, 'timeout_seconds': None}, {**config, 'timeout_seconds': 0}])['status'] == 'ready_for_validation'
    assert len(list(run.glob('reviewer-*/attempt-*'))) == 2
