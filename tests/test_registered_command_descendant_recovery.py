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
from test_owned_command_process_scope import darwin_owned_process_queries


@pytest.fixture(autouse=True)
def isolate_unavailable_host_process_queries(monkeypatch):
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: None)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)


def launch_supervisor(run, command):
    run.mkdir()
    write_json(run / 'run.json', {})
    artifacts = run / 'registered-command-observations'
    artifacts.mkdir()
    process = subprocess.Popen([sys.executable, '-B', retained_command_supervisor.__file__, str(run), str(artifacts),
        '[]', json.dumps(command)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, artifacts


@pytest.mark.parametrize('revoke', [False, True])
def test_detached_descendant_cannot_write_after_explicit_signal_or_persistent_stop(tmp_path, revoke):
    run = tmp_path / 'detached-session-supervision'
    started = tmp_path / 'detached-process-started'
    side_effect = tmp_path / 'forbidden-late-side-effect'
    release = tmp_path / 'release-late-side-effect'
    detached_code = (f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid()))\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(0.01)\n'
        f'Path({str(side_effect)!r}).touch()')
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{detached_code!r}],start_new_session=True).wait()']
    process, artifacts = launch_supervisor(run, command)
    try:
        limit = time.monotonic() + 5
        while not started.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        assert started.exists()
        while int(started.read_text()) not in {item['pid'] for item in read_json(artifacts / 'command-supervision.json').get('owned_processes', [])}:
            assert time.monotonic() < limit
            time.sleep(0.01)
        if revoke:
            write_json(run / 'execution-revocation.json', {'reason': '自动化 Stop 验证'})
        else:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)
        facts = read_json(artifacts / 'command-supervision.json')
        assert facts['status'] == 'termination_requested'
        assert process.returncode == -signal.SIGKILL
        from core.task_snapshot_review.native_owned_process_observation import native_process_identity
        while native_process_identity(int(started.read_text())) is not None:
            assert time.monotonic() < limit
            time.sleep(0.01)
        release.touch()
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


@pytest.mark.parametrize('injection_phase', ['before_launch', 'running'])
def test_sigterm_during_owned_query_is_not_swallowed_as_observation_error(tmp_path, injection_phase):
    run = tmp_path / 'signal-during-owned-query'
    artifacts = run / 'command-evidence'
    artifacts.mkdir(parents=True)
    write_json(run / 'run.json', {})
    command_started = tmp_path / 'command-started'
    signal_injected = tmp_path / 'signal-injected'
    command = [sys.executable, '-B', '-c',
        f'from pathlib import Path; import time; Path({str(command_started)!r}).touch(); time.sleep(30)']
    script = f'''
import os, signal
from pathlib import Path
from core.task_snapshot_review import retained_command_supervisor as supervisor
original_snapshot = supervisor.owned_process_snapshot
injected = False
def snapshot(supervisor_pid, owned, **kwargs):
    global injected
    if not injected and ({injection_phase!r} == 'before_launch' or owned):
        injected = True
        Path({str(signal_injected)!r}).touch()
        os.kill(os.getpid(), signal.SIGTERM)
    return original_snapshot(supervisor_pid, owned, **kwargs)
supervisor.owned_process_snapshot = snapshot
raise SystemExit(supervisor.supervise_registered_command(
    Path({str(run)!r}), Path({str(artifacts)!r}), (), {command!r}))
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=3)
        assert signal_injected.exists()
        facts = read_json(artifacts / 'command-supervision.json')
        assert facts['status'] == 'termination_requested'
        assert process.returncode == -signal.SIGKILL
        if injection_phase == 'before_launch':
            assert not facts.get('command_execution_started')
            assert not command_started.exists()
        else:
            assert facts['command_execution_started']
            from core.task_snapshot_review.native_owned_process_observation import native_process_identity
            limit = time.monotonic() + 3
            while native_process_identity(facts['command_pid']) is not None:
                assert time.monotonic() < limit
                time.sleep(0.01)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize('command_exit_code', [0, 7])
def test_ps_failure_uses_native_evidence_without_waiting_for_deadline(tmp_path, monkeypatch, command_exit_code):
    executable_directory = tmp_path / 'unavailable-process-query-executables'
    executable_directory.mkdir()
    ps = executable_directory / 'ps'
    ps.write_text('#!/bin/sh\nexit 1\n')
    ps.chmod(0o755)
    monkeypatch.setenv('PATH', str(executable_directory))
    run = tmp_path / 'native-query-fallback'
    process, artifacts = launch_supervisor(run, [sys.executable, '-B', '-c', f'raise SystemExit({command_exit_code})'])
    process.wait(timeout=2)
    facts = read_json(artifacts / 'command-supervision.json')
    assert process.returncode == command_exit_code
    assert facts['status'] == 'completed'
    assert facts['command_returncode'] == command_exit_code
    assert facts['owned_process_exit_status'] == 'exited'
    assert facts['process_ownership_scope'] == 'registered_process_identities_owned_groups_and_descendant_chains'
    assert facts['command_process_lifetime_contract'] == 'wait_for_background_work_or_register_separate_supervised_commands'


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
        f'raise SystemExit(s.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),(),command))'
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
    code = f"import os,time; from pathlib import Path; time.sleep(0.2); child=os.fork(); time.sleep(0.3) if child==0 else os.waitpid(child,0); Path('task.py').write_text('value = 99\\n') if child==0 else None; Path({str(finished)!r}).touch() if child==0 else None"
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
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'no')
    failed_path = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    failed = read_json(failed_path)
    failed_original = Path(failed['original']).read_bytes()
    assert failed['status'] == 'failed'
    assert collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit']) == failed_path
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'yes')
    recovered_path = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
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
    monkeypatch.setenv('RVF_TEST_SERVICE_AVAILABLE', 'yes')
    assert resume_review(run)['status'] == 'ready_to_commit'
    assert not (run / 'fixer/role-deadline.json').exists()
    assert len(list(run.glob('reviewer-*/attempt-*/request.json'))) == 2
    assert len(list(run.glob('fixer/attempt-*/request.json'))) == 3


def test_service_recovery_without_environment_change_gets_one_scheduled_probe(repository, tmp_path):
    service_available = tmp_path / 'registered-service-availability-proof'
    command = [sys.executable, '-B', '-c', f'from pathlib import Path; assert Path({str(service_available)!r}).exists()']
    run = tmp_path / 'scheduled-service-recovery'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'bounded service probe',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    initial = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    initial_bytes = initial.read_bytes()
    intent_path = run / 'incomplete-fixer-machine-validation/round-001/collection-intent.json'
    intent = read_json(intent_path)
    assert intent['retry_not_before'] > time.time()
    intent['retry_not_before'] = time.time() - 1
    write_json(intent_path, intent)
    service_available.touch()
    recovered = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert recovered != initial
    assert read_json(recovered)['status'] == 'passed'
    assert read_json(intent_path)['unchanged_recovery_probe_used'] is True
    assert initial.read_bytes() == initial_bytes
    refreshed = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert refreshed != recovered
    assert read_json(refreshed)['status'] == 'passed'
    assert read_json(refreshed)['collection_attempt'] == 3
    assert collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit']) is None


def test_passed_retained_validation_is_recollected_after_environment_changes(repository, tmp_path, monkeypatch):
    command = [sys.executable, '-B', '-c', 'import os; assert os.environ["RVF_TEST_REQUIRED_SERVICE"]=="ready"']
    run = tmp_path / 'passed-evidence-environment-drift'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'fresh environment evidence',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    monkeypatch.setenv('RVF_TEST_REQUIRED_SERVICE', 'ready')
    original = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    original_bytes = original.read_bytes()
    assert read_json(original)['status'] == 'passed'
    monkeypatch.setenv('RVF_TEST_REQUIRED_SERVICE', 'unavailable')
    current = collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
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
        'def observe(*args,**kwargs):\n'
        '    global attempts\n'
        '    attempts+=1\n'
        '    if attempts<=2: raise RuntimeError("原生查询瞬时不可用")\n'
        '    return {}\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=observe):\n'
        f'    raise SystemExit(supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),'
        f'(),{[sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]!r}))\n'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    process.wait(timeout=2)
    assert process.returncode == 0
    assert marker.exists()
    assert read_json(artifacts / 'command-supervision.json')['status'] == 'completed'


def test_unreadable_foreign_sibling_does_not_block_registered_orphan_observation(darwin_owned_process_queries):
    from core.task_snapshot_review import native_owned_process_observation as observation
    state = darwin_owned_process_queries
    state.processes[800604] = (state.orphan_pid, 800604, 103)
    previous = {state.orphan_pid: {'pid': state.orphan_pid, 'start_identity': [102, 0]}}
    owned = observation.owned_process_snapshot(state.supervisor_pid, previous,
        supervisor_native_identity=state.identity)
    assert set(owned) == {state.child_pid, state.orphan_pid, 800604}
    assert state.unrelated_pid not in state.inspected
    assert set(observation.owned_process_snapshot(state.supervisor_pid, owned,
        supervisor_native_identity=state.identity)) == set(owned)


@pytest.mark.parametrize('stop', [False, True])
def test_observed_child_remains_owned_after_session_detach_and_direct_command_exit(tmp_path, stop):
    from core.task_snapshot_review.native_owned_process_observation import native_process_identity
    run = tmp_path / 'observed-before-session-detach'
    supervision_file = run / 'registered-command-observations/command-supervision.json'
    detached, release, side_effect = run / 'detached-child-pid', run / 'release-child', run / 'late-side-effect'
    child_code = (
        'import os,json,time; from pathlib import Path\n'
        f'facts=Path({str(supervision_file)!r})\n'
        'deadline=time.monotonic()+5\n'
        'while os.getpid() not in {item["pid"] for item in json.loads(facts.read_text()).get("owned_processes",[])}:\n'
        ' assert time.monotonic()<deadline\n'
        ' time.sleep(0.01)\n'
        f'os.setsid(); Path({str(detached)!r}).write_text(str(os.getpid()))\n'
        f'while not Path({str(release)!r}).exists():\n'
        ' assert time.monotonic()<deadline\n'
        ' time.sleep(0.01)\n'
        f'Path({str(side_effect)!r}).touch()\n'
    )
    parent_code = (
        'import subprocess,sys,time; from pathlib import Path\n'
        f'subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],env={{}})\n'
        f'while not Path({str(detached)!r}).exists(): time.sleep(0.01)\n'
    )
    supervisor, _ = launch_supervisor(run, [sys.executable, '-B', '-c', parent_code])
    child_identity = None
    try:
        deadline = time.monotonic() + 4
        while not detached.exists() or read_json(supervision_file).get('command_returncode') is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child_identity = native_process_identity(int(detached.read_text()))
        facts = read_json(supervision_file)
        assert supervisor.poll() is None
        assert facts['command_returncode'] == 0
        assert facts['owned_process_exit_status'] == 'pending'
        assert child_identity['process_group_id'] == child_identity['pid']
        assert not side_effect.exists()
        if stop:
            write_json(run / 'execution-revocation.json', {'reason': '取消已观察后脱离的子进程'})
        else:
            release.touch()
        supervisor.wait(timeout=3)
        if stop:
            assert not side_effect.exists()
            assert read_json(supervision_file)['owned_process_exit_status'] != 'exited'
            while native_process_identity(child_identity['pid']) is not None:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        else:
            assert supervisor.returncode == 0
            assert side_effect.exists()
            assert read_json(supervision_file)['owned_process_exit_status'] == 'exited'
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait()
        if child_identity and native_process_identity(child_identity['pid']) is not None:
            try:
                os.kill(child_identity['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
