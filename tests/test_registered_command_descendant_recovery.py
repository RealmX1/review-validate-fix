"""跨 session 孤儿、原生查询替代和失败补验证据恢复的回归。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import execution_lifecycle, retained_command_supervisor
from core.task_snapshot_review.machine_evidence import collect_incomplete_fixer_machine_validation
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import _validate, resume_review
from test_incomplete_fixer_machine_validation_recovery import review_with_registered_validation, machine_blocked_fixer
from test_task_snapshot_review_workflow import repository


@pytest.fixture(autouse=True)
def isolate_unavailable_host_process_queries(monkeypatch):
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: None)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)


def launch_supervisor(run, command, deadline):
    run.mkdir()
    write_json(run / 'run.json', {})
    artifacts = run / 'registered-command-observations'
    artifacts.mkdir()
    process = subprocess.Popen([sys.executable, '-B', retained_command_supervisor.__file__, str(run), str(artifacts),
        str(deadline), '[]', json.dumps(command)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, artifacts


@pytest.mark.parametrize('revoke', [False, True])
def test_detached_descendant_cannot_write_after_original_deadline_or_stop(tmp_path, revoke):
    run = tmp_path / 'detached-session-supervision'
    started = tmp_path / 'detached-process-started'
    side_effect = tmp_path / 'forbidden-late-side-effect'
    detached_code = f"from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(1.3); Path({str(side_effect)!r}).touch()"
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{detached_code!r}],start_new_session=True)']
    process, artifacts = launch_supervisor(run, command, time.time() + 0.8)
    try:
        limit = time.monotonic() + 0.7
        while not started.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        assert started.exists()
        if revoke:
            write_json(run / 'execution-revocation.json', {'reason': '自动化 Stop 验证'})
        process.wait(timeout=1.4)
        facts = read_json(artifacts / 'command-supervision.json')
        assert facts['status'] == 'termination_requested'
        assert process.returncode == -signal.SIGKILL
        time.sleep(0.7)
        assert not side_effect.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_ps_failure_uses_native_evidence_without_waiting_for_deadline(tmp_path, monkeypatch):
    executable_directory = tmp_path / 'unavailable-process-query-executables'
    executable_directory.mkdir()
    ps = executable_directory / 'ps'
    ps.write_text('#!/bin/sh\nexit 1\n')
    ps.chmod(0o755)
    monkeypatch.setenv('PATH', str(executable_directory))
    run = tmp_path / 'native-query-fallback'
    process, artifacts = launch_supervisor(run, [sys.executable, '-B', '-c', 'pass'], time.time() + 10)
    process.wait(timeout=2)
    facts = read_json(artifacts / 'command-supervision.json')
    assert process.returncode == 0
    assert facts['status'] == 'completed'
    assert facts['command_returncode'] == 0


def test_unavailable_native_query_retains_unknown_without_running_command(tmp_path):
    run = tmp_path / 'unavailable-native-process-evidence'
    run.mkdir()
    artifacts = run / 'command-evidence'
    artifacts.mkdir()
    write_json(run / 'run.json', {})
    marker = tmp_path / 'command-should-not-start'
    script = (
        'import os; from pathlib import Path; from unittest.mock import patch; '
        'from core.task_snapshot_review import retained_command_supervisor as s; '
        f'command={[sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]!r}; '
        'patcher=patch.object(s,"owned_process_snapshot",side_effect=RuntimeError("原生查询不可用")); patcher.start(); '
        f'raise SystemExit(s.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),{time.time()+10},(),command))'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    process.wait(timeout=2)
    assert process.returncode == 125
    facts = read_json(artifacts / 'command-supervision.json')
    assert facts['status'] == 'descendant_exit_unproven'
    assert facts['command_returncode'] is None
    assert not marker.exists()


def test_authoritative_validation_waits_for_late_fork_and_rejects_source_drift(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    finished = tmp_path / 'late-validation-descendant-finished'
    code = f"import os,time; from pathlib import Path; time.sleep(0.2); child=os.fork(); time.sleep(0.3) if child==0 else None; Path('task.py').write_text('value = 99\\n') if child==0 else None; Path({str(finished)!r}).touch() if child==0 else None"
    commands = [[sys.executable, '-B', '-c', code]]
    run = tmp_path / 'authoritative-descendant-completion'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'exact source tree',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': commands})
    with exclusive_lock(run / 'workflow.lock') as lock:
        result = _validate(run, record, commands, lock)
    assert finished.exists()
    assert result['status'] == 'candidate_changed'
    facts = read_json(Path(result['attempt_directory']) / 'command-001/command-supervision.json')
    assert facts['status'] == 'completed'


def test_failed_same_tree_validation_retries_when_environment_recovers(repository, tmp_path, monkeypatch):
    run = tmp_path / 'same-tree-environment-recovery'
    command = [sys.executable, '-B', '-c', 'import os; assert os.environ.get("RVF_TEST_SERVICE_AVAILABLE")=="yes"']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'bounded recovery',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    deadline = time.time() + 15
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'no')
    failed_path = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    failed = read_json(failed_path)
    failed_original = Path(failed['original']).read_bytes()
    assert failed['status'] == 'failed'
    assert collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline) == failed_path
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'yes')
    recovered_path = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    recovered = read_json(recovered_path)
    assert recovered_path != failed_path
    assert recovered['status'] == 'passed'
    assert recovered['tested_tree'] == failed['tested_tree']
    assert Path(failed['original']).read_bytes() == failed_original
    assert recovered['collection_attempt'] == 2


def test_workflow_resumes_original_fixer_after_same_tree_environment_recovery(repository, tmp_path, monkeypatch):
    run, reviewer = review_with_registered_validation(repository, tmp_path)
    plan = read_json(run / 'execution-plan.json')
    plan['validation_commands'][0][-1] += '; import os; assert os.environ.get("RVF_TEST_SERVICE_AVAILABLE")=="yes"'
    import hashlib
    write_json(run / 'execution-plan.json', plan)
    record = read_json(run / 'run.json')
    record['execution_plan_sha256'] = hashlib.sha256((run / 'execution-plan.json').read_bytes()).hexdigest()
    write_json(run / 'run.json', record)
    fixer = machine_blocked_fixer(tmp_path, mode='always_machine')
    script = Path(fixer['command'][1])
    script.write_text(script.read_text().replace("status='incomplete' if mode=='always_machine' else 'completed'", "status='completed' if proof['status']=='passed' else 'incomplete'"))
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'no')
    assert resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=fixer)['status'] == 'fix_incomplete'
    original_deadline = read_json(run / 'fixer/role-deadline.json')
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'yes')
    assert resume_review(run)['status'] == 'ready_to_commit'
    assert read_json(run / 'fixer/role-deadline.json') == original_deadline
    assert len(list(run.glob('reviewer-*/attempt-*/request.json'))) == 2
    assert len(list(run.glob('fixer/attempt-*/request.json'))) == 3


def test_service_recovery_without_environment_change_gets_one_scheduled_probe(repository, tmp_path):
    service_available = tmp_path / 'registered-service-availability-proof'
    command = [sys.executable, '-B', '-c', f'from pathlib import Path; assert Path({str(service_available)!r}).exists()']
    run = tmp_path / 'scheduled-service-recovery'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'bounded service probe',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    deadline = time.time() + 15
    initial = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    initial_bytes = initial.read_bytes()
    intent_path = run / 'incomplete-fixer-machine-validation/round-001/collection-intent.json'
    intent = read_json(intent_path)
    assert intent['retry_not_before'] > time.time()
    intent['retry_not_before'] = time.time() - 1
    write_json(intent_path, intent)
    service_available.touch()
    recovered = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    assert recovered != initial
    assert read_json(recovered)['status'] == 'passed'
    assert read_json(intent_path)['unchanged_recovery_probe_used'] is True
    assert initial.read_bytes() == initial_bytes
    refreshed = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    assert refreshed != recovered
    assert read_json(refreshed)['status'] == 'passed'
    assert read_json(refreshed)['collection_attempt'] == 3
    assert collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline) is None


def test_passed_retained_validation_is_recollected_after_environment_changes(repository, tmp_path, monkeypatch):
    command = [sys.executable, '-B', '-c', 'import os; assert os.environ["RVF_TEST_REQUIRED_SERVICE"]=="ready"']
    run = tmp_path / 'passed-evidence-environment-drift'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'fresh environment evidence',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    deadline = time.time() + 15
    monkeypatch.setenv('RVF_TEST_REQUIRED_SERVICE', 'ready')
    original = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    original_bytes = original.read_bytes()
    assert read_json(original)['status'] == 'passed'
    monkeypatch.setenv('RVF_TEST_REQUIRED_SERVICE', 'unavailable')
    current = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'], deadline)
    assert current != original
    assert read_json(current)['status'] == 'failed'
    assert original.read_bytes() == original_bytes


def test_transient_native_observation_recovers_before_command_dispatch(tmp_path):
    run = tmp_path / 'transient-native-observation-recovery'
    run.mkdir()
    artifacts = run / 'command-observations'
    artifacts.mkdir()
    write_json(run / 'run.json', {})
    marker = tmp_path / 'command-started-after-evidence-recovered'
    script = (
        'import sys; from pathlib import Path; from unittest.mock import patch\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'attempts=0\n'
        'def observe(*args):\n'
        '    global attempts\n'
        '    attempts+=1\n'
        '    if attempts<=2: raise RuntimeError("原生查询瞬时不可用")\n'
        '    return {}\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=observe):\n'
        f'    raise SystemExit(supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),'
        f'{time.time()+10},(),{[sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]!r}))\n'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    process.wait(timeout=2)
    assert process.returncode == 0
    assert marker.exists()
    assert read_json(artifacts / 'command-supervision.json')['status'] == 'completed'


def test_unreadable_foreign_sibling_does_not_block_owned_orphan_observation(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from core.task_snapshot_review import native_owned_process_observation as observation
    supervisor_pid = os.getpid()
    foreign_parent, foreign_sibling, owned_child, owned_orphan, orphan_child = 800001, 800002, 800003, 800004, 800005
    identities = {
        supervisor_pid: (foreign_parent, supervisor_pid, 100),
        foreign_parent: (1, foreign_parent, 99),
        foreign_sibling: (foreign_parent, foreign_parent, 101),
        owned_child: (supervisor_pid, supervisor_pid, 101),
        owned_orphan: (1, owned_orphan, 101),
        orphan_child: (owned_orphan, orphan_child, 102),
    }
    queried_environment_pids = []
    def process_info(pid, flavor, argument, pointer, size):
        info = pointer._obj
        info.process_id = pid
        info.parent_process_id, info.process_group_id, info.start_seconds = identities[pid]
        info.user_id = os.getuid()
        info.process_status = 2
        return size
    def process_list(selection, user_id, destination, size):
        assert selection == 4 and user_id == os.getuid()
        if destination is not None:
            for index, pid in enumerate(identities):
                destination[index] = pid
        return len(identities) * ctypes.sizeof(ctypes.c_int)
    def environment_query(query, count, arguments, length, replacement, replacement_length):
        pid = query[2]
        queried_environment_pids.append(pid)
        if pid == foreign_sibling:
            ctypes.set_errno(5)
            return -1
        arguments.value = (observation.REGISTERED_COMMAND_OWNERSHIP_ENVIRONMENT_KEY + '=current-execution').encode()
        length._obj.value = len(arguments.value) + 1
        return 0
    library = SimpleNamespace(proc_pidinfo=process_info, proc_listpids=process_list)
    system = SimpleNamespace(sysctl=environment_query)
    monkeypatch.setattr(observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda name, **kwargs: library if name else system)
    owned = observation.owned_process_snapshot('current-execution', supervisor_pid, {})
    assert set(owned) == {owned_child, owned_orphan, orphan_child}
    assert queried_environment_pids == [owned_orphan]
    queried_environment_pids.clear()
    assert set(observation.owned_process_snapshot('current-execution', supervisor_pid, owned)) == {owned_child, owned_orphan, orphan_child}
    assert queried_environment_pids == []
