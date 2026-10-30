"""补验授权、树归属、自动接续与崩溃恢复的独立回归。"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.task_snapshot_review.machine_evidence import collect_incomplete_fixer_machine_validation, execute_registered_commands
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.trajectory_analysis import analyze_runs
from core.task_snapshot_review.workflow import resume_review
from test_incomplete_fixer_machine_validation_recovery import machine_blocked_fixer, review_with_registered_validation
from test_task_snapshot_review_workflow import repository


def test_changed_frozen_plan_cannot_authorize_host_command(repository, tmp_path):
    run, _ = review_with_registered_validation(repository, tmp_path)
    record = read_json(run / 'run.json')
    record['semantic_repair_rounds_used'] = 1
    marker = tmp_path / 'unauthorized-host-command-marker'
    plan = read_json(run / 'execution-plan.json')
    command = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
    plan['validation_commands'] = [command]
    write_json(run / 'execution-plan.json', plan)
    with pytest.raises(ValueError, match='冻结启动材料摘要'):
        collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    with pytest.raises(ValueError, match='冻结启动材料摘要'):
        execute_registered_commands(run, repository, [command], run / 'replaced-command', 'incomplete_fixer_machine_validation')
    assert not marker.exists()


def test_independent_supervisor_cleans_late_orphan_and_releases_inherited_lock(tmp_path):
    import fcntl
    from core.task_snapshot_review import retained_command_supervisor
    run = tmp_path / 'independent-cancellable-supervision'
    run.mkdir()
    write_json(run / 'run.json', {})
    artifacts = run / 'retained-command'
    artifacts.mkdir()
    child_marker = run / 'late-descendant-started'
    command = [sys.executable, '-c',
        "import os,time; from pathlib import Path; "
        "child=os.fork(); "
        f"Path({str(child_marker)!r}).write_text(str(os.getpid())) if child==0 else None; "
        "time.sleep(30) if child==0 else None"]
    with (run / 'collection.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deadline = time.time() + 1.5
        process = subprocess.Popen([sys.executable, retained_command_supervisor.__file__, str(run), str(artifacts),
            str([lock.fileno()]), __import__('json').dumps(command)],
            start_new_session=True, pass_fds=(lock.fileno(),))
        try:
            while not child_marker.exists() and time.time() < deadline:
                time.sleep(0.01)
            assert child_marker.exists()
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=2)
            assert process.returncode == -signal.SIGKILL
            assert time.time() < deadline + 0.8
            assert read_json(artifacts / 'command-supervision.json')['status'] == 'termination_requested'
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    with (run / 'collection.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_explicit_context_is_imported_once_before_automatic_machine_resume(repository, tmp_path):
    run, reviewer = review_with_registered_validation(repository, tmp_path)
    context = tmp_path / 'explicit-context-response.md'
    context.write_text('补证：仅修复当前已授权任务。')
    result = resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=machine_blocked_fixer(tmp_path), context_response_file=context)
    assert result['status'] == 'ready_to_commit'
    requests = [read_json(path) for path in sorted(run.glob('fixer/attempt-*/request.json'))]
    assert len(requests) == 2
    assert requests[0]['context_response_sha256'] != requests[1]['context_response_sha256']
    assert requests[1]['incomplete_fixer_machine_validation_evidence']


def test_each_validation_command_is_bound_to_actual_source_tree(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'source-drift-machine-validation'
    rewrite = [sys.executable, '-c', "from pathlib import Path; Path('task.py').write_text('value = 99\\n')"]
    restore = [sys.executable, '-c', "from pathlib import Path; assert Path('task.py').read_text() == 'value = 99\\n'; Path('task.py').write_text('value = 2\\n')"]
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'validate exact source', run_directory=run,
        execution_plan={'fix': True, 'validation_commands': [rewrite, restore]})
    record['semantic_repair_rounds_used'] = 1
    evidence = read_json(collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit']))
    assert evidence['status'] == 'candidate_changed'
    assert len(evidence['commands']) == 1
    assert evidence['commands'][0]['source_tree_before'] == evidence['tested_tree']
    assert evidence['commands'][0]['source_tree_after'] != evidence['tested_tree']
    assert not (run / 'incomplete-fixer-machine-validation/round-001/attempt-001/commands/command-002').exists()


def test_analyze_includes_incomplete_fixer_machine_validation(repository, tmp_path):
    run, reviewer = review_with_registered_validation(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=machine_blocked_fixer(tmp_path))['status'] == 'ready_to_commit'
    before = {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}
    result = analyze_runs([run])
    assert result['metrics']['machine_evidence_collections'] == 1
    assert result['metrics']['validation_attempts'] == 2
    assert len(result['runs'][0]['incomplete_fixer_machine_validations']) == 1
    assert before == {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}


def test_restarted_collection_waits_for_live_command_until_persistent_stop(repository, tmp_path):
    run = tmp_path / 'interrupted-machine-collection'
    marker = tmp_path / 'retained-validation-process-id'
    command = [sys.executable, '-c', f'import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'cancellable recovery', run_directory=run,
        execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    write_json(run / 'run.json', record)
    script = ('import json,sys; from pathlib import Path; '
              'from core.task_snapshot_review.machine_evidence import collect_incomplete_fixer_machine_validation; '
              'r=Path(sys.argv[1]); record=json.loads((r/"run.json").read_text()); '
              'collect_incomplete_fixer_machine_validation(r,record,record["candidate_commit"])')
    controller = subprocess.Popen([sys.executable, '-c', script, str(run)])
    restarted = None
    try:
        limit = time.monotonic() + 5
        execution = run / 'incomplete-fixer-machine-validation/round-001/attempt-001/commands/command-001/execution.json'
        while not (marker.exists() and execution.exists() and 'process_identity' in read_json(execution)):
            assert time.monotonic() < limit
            time.sleep(0.02)
        controller.kill()
        controller.wait(timeout=2)
        restarted = subprocess.Popen([sys.executable, '-c', script, str(run)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        assert restarted.poll() is None
        assert len(list(run.glob('incomplete-fixer-machine-validation/round-*/attempt-*'))) == 1
        write_json(run / 'execution-revocation.json', {'reason': '测试持久撤销'})
        assert restarted.wait(timeout=3) == 1
        import fcntl
        with (run / 'incomplete-fixer-machine-validation/round-001/collection.lock').open('a+') as lock:
            limit = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < limit
                    time.sleep(0.02)
    finally:
        write_json(run / 'execution-revocation.json', {'reason': '测试清理'})
        for process in (controller, restarted):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.parametrize('identity_matches', [True, False])
def test_retained_command_is_signalled_only_with_matching_start_identity(tmp_path, monkeypatch, identity_matches):
    from core.task_snapshot_review import machine_evidence
    directory = tmp_path / 'retained-machine-validation'
    identity = {'pid': 12345, 'start_and_command': 'Wed Oct 28 12:00:00 2026 12345 registered-worker'}
    write_json(directory / 'commands/command-001/execution.json', {'status': 'running', 'process_identity': identity})
    monkeypatch.setattr(machine_evidence, 'process_start_identity', lambda pid, **kwargs: identity if identity_matches else {**identity, 'start_and_command': 'Wed Oct 28 12:00:01 2026 12345 reused-worker'})
    signals = []
    monkeypatch.setattr(machine_evidence.os, 'killpg', lambda pid, selected_signal: signals.append((pid, selected_signal)))
    machine_evidence._terminate_retained_collection_commands(directory, 'execution_revoked')
    assert signals == ([(12345, signal.SIGKILL)] if identity_matches else [])
    assert read_json(directory / 'collection-recovery.json')['processes'][0]['status'] == (
        'termination_requested' if identity_matches else 'identity_changed_no_signal')


def test_collection_waits_for_healthy_command_without_total_deadline(repository, tmp_path):
    run = tmp_path / 'unlimited-machine-validation'
    command = [sys.executable, '-c', 'import time; time.sleep(0.6)']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'complete registered validation', run_directory=run,
        execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    evidence = read_json(collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit']))
    assert evidence['status'] == 'passed'
    assert evidence['commands'][0]['returncode'] == 0


def test_command_launch_rejects_tree_drift_after_collection_snapshot(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import machine_evidence
    run = tmp_path / 'command-boundary-tree-drift'
    marker = tmp_path / 'validation-command-executed'
    command = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'bind each command', run_directory=run,
        execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    capture = machine_evidence.capture_worktree_tree
    captures = []
    def drift_at_command_boundary(workspace, scope, artifacts, **kwargs):
        captures.append(workspace)
        if len(captures) == 2:
            (workspace / 'task.py').write_text('value = 99\n')
        return capture(workspace, scope, artifacts, **kwargs)
    monkeypatch.setattr(machine_evidence, 'capture_worktree_tree', drift_at_command_boundary)
    evidence = read_json(collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit']))
    assert evidence['status'] == 'candidate_changed'
    assert evidence['commands'][0]['execution_started'] is False
    assert not marker.exists()


def test_command_dispatch_rechecks_revocation_after_snapshot(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import machine_evidence
    from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked
    run, _ = review_with_registered_validation(repository, tmp_path)
    command = read_json(run / 'execution-plan.json')['validation_commands'][0]
    capture = machine_evidence.capture_worktree_tree
    def revoke_after_snapshot(*arguments, **kwargs):
        tree = capture(*arguments, **kwargs)
        write_json(run / 'execution-revocation.json', {'reason': '快照期间撤销'})
        return tree
    monkeypatch.setattr(machine_evidence, 'capture_worktree_tree', revoke_after_snapshot)
    with pytest.raises(ExecutionRevoked):
        execute_registered_commands(run, repository, [command], run / 'dispatch', 'shared_machine_evidence')
    assert 'process_identity' not in read_json(run / 'dispatch/command-001/execution.json')
    assert not (run / 'owned-processes.json').exists()


def test_corrected_incomplete_fixer_tree_gets_next_bounded_validation_round(repository, tmp_path):
    run, reviewer = review_with_registered_validation(repository, tmp_path, expected_value=4)
    fixer = machine_blocked_fixer(tmp_path, mode='always_machine', final_value=4)
    script = Path(fixer['command'][1])
    script.write_text(script.read_text().replace(
        "assert pathlib.Path(proof['workspace']).joinpath('task.py').read_text() == 'value = 3\\n'",
        "assert pathlib.Path(proof['workspace']).joinpath('task.py').read_text() in ('value = 3\\n','value = 4\\n')"
    ).replace("status='incomplete' if mode=='always_machine' else 'completed'",
              "status='completed' if proof['status']=='passed' else 'incomplete'"))
    assert resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=fixer)['status'] == 'ready_to_commit'
    assert read_json(run / 'run.json')['semantic_repair_rounds_used'] == 2
    assert len(list(run.glob('fixer/attempt-*/request.json'))) == 3


def test_interrupted_collection_preserves_each_attempt_and_analysis_count(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import machine_evidence
    run, _ = review_with_registered_validation(repository, tmp_path, expected_value=2)
    record = read_json(run / 'run.json')
    record['semantic_repair_rounds_used'] = 1
    original_write = machine_evidence.write_json
    def crash_before_accepting_evidence(path, value):
        if path.name == 'collection-intent.json' and value.get('evidence_sha256'):
            raise RuntimeError('模拟证据已保存但索引未提交')
        original_write(path, value)
    monkeypatch.setattr(machine_evidence, 'write_json', crash_before_accepting_evidence)
    with pytest.raises(RuntimeError, match='索引未提交'):
        collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    originals = {path: path.read_bytes() for path in run.glob('incomplete-fixer-machine-validation/round-*/attempt-*/**/*') if path.is_file()}
    assert originals
    monkeypatch.setattr(machine_evidence, 'write_json', original_write)
    collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert all(path.read_bytes() == content for path, content in originals.items())
    analysis = analyze_runs([run])
    assert analysis['metrics']['machine_evidence_collections'] == 2
    assert analysis['metrics']['validation_attempts'] == 2


def test_final_validation_rejects_source_rewrite_before_restoration_command(repository, tmp_path):
    from core.task_snapshot_review.workflow import _validate
    from core.task_snapshot_review.persistent_artifacts import exclusive_lock
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'authoritative-source-drift'
    commands = [
        [sys.executable, '-c', "from pathlib import Path; Path('task.py').write_text('value = 99\\n')"],
        [sys.executable, '-c', "from pathlib import Path; Path('task.py').write_text('value = 2\\n')"],
    ]
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'exact final tree',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': commands})
    with exclusive_lock(run / 'workflow.lock') as lock:
        validation = _validate(run, record, commands, lock)
    assert validation['status'] == 'candidate_changed'
    assert not (Path(validation['attempt_directory']) / 'command-002').exists()
