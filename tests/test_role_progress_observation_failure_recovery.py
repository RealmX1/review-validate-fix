"""辅助观察失败不得改变健康角色结果；失败重试必须有界且可自动恢复。"""
import signal
import sys

import pytest

from core.task_snapshot_review import execution_lifecycle, role_process_execution, workflow
from core.task_snapshot_review.persistent_artifacts import read_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository


@pytest.mark.parametrize('failure_phase', ['cli_registration', 'wait_observation', 'final_observation', 'all_observations'])
def test_progress_write_failure_does_not_terminate_healthy_role(repository, tmp_path, monkeypatch, failure_phase):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'progress-observation-recovery'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'progress is observation', run_directory=run)
    finished = tmp_path / 'healthy-cli-finished'
    script = tmp_path / 'healthy-cli.py'
    script.write_text('import json,time\nfrom pathlib import Path\ntime.sleep(1.2)\n'
        + f'Path({str(finished)!r}).touch()\n'
        + "print(json.dumps({'status':'completed','audit_summary':'健康角色完成','findings':[]}))\n")
    monkeypatch.setattr(workflow, 'launch_role', lambda *args, **kwargs: None)
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    role = run / 'reviewer-1'
    workflow._start_role(run, read_json(run / 'run.json'), role, 'reviewer',
                         [{'command': [sys.executable, str(script)], 'output_format': 'text'}], [])
    original_write = execution_lifecycle.write_json
    failures = []
    def write_with_observation_failure(path, payload):
        selected = (failure_phase == 'all_observations'
            or failure_phase == 'cli_registration' and 'process_identity' in payload
            or failure_phase == 'wait_observation' and payload.get('waiting_reason') == 'owned_process_running'
            or failure_phase == 'final_observation' and payload.get('status') == 'completed')
        if path.name == 'progress.json' and selected and (not failures or failure_phase == 'all_observations'):
            failures.append(str(path))
            raise OSError('模拟进度存储暂时不可用')
        return original_write(path, payload)
    monkeypatch.setattr(execution_lifecycle, 'write_json', write_with_observation_failure)
    monkeypatch.setattr(role_process_execution, 'write_json', write_with_observation_failure)
    attempt = role / 'attempt-001'
    role_process_execution.execute_role(attempt)
    assert failures
    assert finished.exists()
    assert read_json(attempt / 'outcome.json')['status'] == 'completed'
    assert not (run / 'execution-revocation.json').exists()


def test_unchanged_progress_failure_retries_are_bounded_until_process_exit(tmp_path, monkeypatch):
    import itertools
    import subprocess
    from types import SimpleNamespace
    from core.task_snapshot_review.persistent_artifacts import write_json
    run = tmp_path / 'bounded-progress-observation'
    write_json(run / 'run.json', {})
    calls = []
    class HealthyProcess:
        pid = 870900
        def wait(self, timeout):
            calls.append(timeout)
            if len(calls) < 12:
                raise subprocess.TimeoutExpired(['healthy-cli'], timeout)
            return 0
    observations = []
    monkeypatch.setattr(execution_lifecycle, 'record_owned_process_wait_observation',
        lambda *args: observations.append(args) or False)
    monkeypatch.setattr(execution_lifecycle, 'progress_observation_recovery_identity', lambda *args: ('unchanged',))
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    clock = itertools.count(1, 2)
    monkeypatch.setattr(execution_lifecycle, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    assert execution_lifecycle.wait_for_owned_process(HealthyProcess(), run, progress_file=run / 'progress.json') == 0
    assert len(calls) == 12
    assert len(observations) == 4  # 相同故障三次，进程退出这一新事实再补一次。


@pytest.mark.parametrize('operation', ['read', 'output_stat'])
def test_progress_read_or_output_stat_failure_keeps_healthy_role_running(repository, tmp_path, monkeypatch, operation):
    from pathlib import Path
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'progress-read-stat-recovery'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'observation remains optional', run_directory=run)
    started, finished = tmp_path / 'healthy-cli-started', tmp_path / 'healthy-cli-finished'
    script = tmp_path / 'healthy-cli.py'
    script.write_text('import json,time\nfrom pathlib import Path\n'
        + f'Path({str(started)!r}).touch()\ntime.sleep(1.2)\nPath({str(finished)!r}).touch()\n'
        + "print(json.dumps({'status':'completed','audit_summary':'完成语义检查','findings':[]}))\n")
    monkeypatch.setattr(workflow, 'launch_role', lambda *args, **kwargs: None)
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    role = run / 'reviewer-1'
    workflow._start_role(run, read_json(run / 'run.json'), role, 'reviewer', [{'command': [sys.executable, str(script)], 'output_format': 'text'}], [])
    failures = []
    original_read = execution_lifecycle.read_json
    original_stat = Path.stat
    def read_with_failure(path):
        if operation == 'read' and path.name == 'progress.json' and not failures:
            failures.append('read')
            raise OSError('进度读取暂时失败')
        return original_read(path)
    def stat_with_failure(path, *args, **kwargs):
        if operation == 'output_stat' and path.name in {'stdout.txt', 'stderr.txt'} and started.exists() and not failures:
            failures.append('output_stat')
            raise OSError('输出活动查询暂时失败')
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(execution_lifecycle, 'read_json', read_with_failure)
    monkeypatch.setattr(Path, 'stat', stat_with_failure)
    role_process_execution.execute_role(role / 'attempt-001')
    assert failures == [operation]
    assert finished.exists()
    assert read_json(role / 'attempt-001/outcome.json')['status'] == 'completed'
