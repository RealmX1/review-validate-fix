"""原生查询缺证、跨 session 清理和依赖身份失效的独立回归。"""
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import textwrap
from types import SimpleNamespace

import pytest

from core.task_snapshot_review import execution_lifecycle, machine_evidence, native_owned_process_observation
from core.task_snapshot_review import retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository
from test_owned_command_process_scope import darwin_owned_process_queries


@pytest.mark.parametrize('previously_observed', [False, True])
def test_process_info_error_preserves_exit_unknown(darwin_owned_process_queries, previously_observed):
    state = darwin_owned_process_queries
    state.denied.add(state.child_pid)
    with pytest.raises(RuntimeError, match=str(state.child_pid)):
        native_owned_process_observation.owned_process_snapshot(state.supervisor_pid,
            {state.child_pid: {'pid': state.child_pid, 'start_identity': [101, 0]}} if previously_observed else {},
            supervisor_native_identity=state.identity)


def test_changed_external_dependency_invalidates_unproven_pass(repository, tmp_path, monkeypatch):
    dependency = tmp_path / 'external-installed-dependency'
    dependency.write_text('ready')
    command = [sys.executable, '-B', '-c', f'from pathlib import Path; assert Path({str(dependency)!r}).read_text()=="ready"']
    run = tmp_path / 'dependency-identity-validation'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'dependency freshness',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    original_execute = machine_evidence.execute_registered_commands
    def execute_without_native_supervision(*args, **kwargs):
        kwargs.pop('inherited_lock_descriptors', None)
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(machine_evidence, 'execute_registered_commands', execute_without_native_supervision)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    initial = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    initial_bytes = initial.read_bytes()
    assert read_json(initial)['status'] == 'passed'
    dependency.write_text('changed')
    current = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
    assert current != initial
    assert read_json(current)['status'] == 'failed'
    assert read_json(current)['collection_attempt'] == 2
    assert initial.read_bytes() == initial_bytes


def test_stop_during_native_observation_outage_does_not_abandon_known_detached_child(tmp_path):
    run = tmp_path / 'native-query-outage-cleanup'
    run.mkdir()
    write_json(run / 'run.json', {})
    artifacts = run / 'command-observations'
    artifacts.mkdir()
    started, late_effect = run / 'detached-child-pid', run / 'forbidden-late-effect'
    child_code = f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(1.2); Path({str(late_effect)!r}).touch()'
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],start_new_session=True).wait()']
    script = (
        'import time; from pathlib import Path; from unittest.mock import patch\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'from core.task_snapshot_review.persistent_artifacts import write_json\n'
        'original=supervisor.owned_process_snapshot\nobserved_at=None\n'
        'def observe(*args,**kwargs):\n'
        ' global observed_at\n'
        ' if observed_at is not None and time.monotonic()-observed_at<0.57: raise RuntimeError("原生查询暂不可用")\n'
        ' result=original(*args,**kwargs)\n'
        f' if Path({str(started)!r}).exists() and int(Path({str(started)!r}).read_text()) in result and observed_at is None:\n'
        f'  observed_at=time.monotonic(); write_json(Path({str(run / "execution-revocation.json")!r}),{{"reason":"已知后代查询故障时 Stop"}})\n'
        ' return result\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=observe):\n'
        f' raise SystemExit(supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(artifacts)!r}),(),{command!r}))\n'
    )
    process = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    try:
        process.wait(timeout=3)
        assert started.exists()
        time.sleep(1.25)
        assert not late_effect.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_reconciles_detached_child_after_supervisor_exit(tmp_path, monkeypatch):
    run = tmp_path / 'cancel-after-supervisor-exit'
    run.mkdir()
    write_json(run / 'run.json', {})
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        identity = native_owned_process_observation.native_process_identity(child.pid)
        monkeypatch.setattr(retained_command_supervisor, 'native_host_boot_identity', lambda: 'controlled-host-boot')
        execution_lifecycle.register_supervised_command_directory(run, run / 'commands/command-001')
        write_json(run / 'commands/command-001/command-supervision.json', {
            'execution_token': 'persisted-owned-execution', 'supervisor_pid': 99999999,
            'host_boot_identity': 'controlled-host-boot',
            'status': 'descendant_exit_unproven', 'owned_processes': [identity]})
        evidence = execution_lifecycle.revoke_execution(run, '监督者退出后补齐持久撤销')
        assert child.wait(timeout=1) == -signal.SIGKILL
        assert evidence['supervised_descendants'][0]['cancellation_status'] == 'termination_requested'
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_reused_native_owned_pid_does_not_receive_cancellation(monkeypatch):
    monkeypatch.setattr(native_owned_process_observation, 'native_process_identity',
        lambda pid: {'pid': pid, 'start_identity': [102, 0]})
    signals = []
    monkeypatch.setattr(os, 'kill', lambda *args: signals.append(args))
    observations = native_owned_process_observation.terminate_observed_owned_processes({
        800111: {'pid': 800111, 'start_identity': [101, 0]}})
    assert observations[0]['cancellation_status'] == 'identity_changed_no_signal'
    assert signals == []


@pytest.mark.parametrize('case', range(3))
def test_registered_command_waits_for_healthy_detached_descendant_completion(tmp_path, monkeypatch, case):
    run = tmp_path / 'registered-deadline-cleanup'
    run.mkdir()
    write_json(run / 'run.json', {})
    started, late_effect = run / 'child-pid', run / 'forbidden-late-effect'
    code = f'from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(0.85); Path({str(late_effect)!r}).touch()'
    command = [sys.executable, '-B', '-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{code!r}],start_new_session=True).wait()']
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: None)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    with exclusive_lock(run / 'collection.lock') as lock:
        result = machine_evidence.execute_registered_commands(run, tmp_path, [command], run / 'commands', 'workspace_preparation',
            inherited_lock_descriptors=(lock.fileno(),))
    try:
        assert result['commands'][0]['returncode'] == 0
        assert started.exists()
        time.sleep(0.9)
        assert late_effect.exists()
    finally:
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def install_scripted_darwin_identity_queries(monkeypatch, query_responses):
    queried_flavors = []
    remaining_responses = list(query_responses)

    def process_info(pid, flavor, argument, pointer, size):
        queried_flavors.append(flavor)
        expected_flavor, response, observed_errno = remaining_responses.pop(0)
        assert flavor == expected_flavor
        ctypes.set_errno(observed_errno)
        if isinstance(response, dict):
            for name, value in response.items():
                setattr(pointer._obj, name, value)
            return size
        return response

    monkeypatch.setattr(native_owned_process_observation.sys, 'platform', 'darwin')
    monkeypatch.setattr(native_owned_process_observation.ctypes, 'CDLL',
        lambda *args, **kwargs: SimpleNamespace(proc_pidinfo=process_info))
    return queried_flavors


def live_bsd_process_info(start_seconds=100):
    return {'process_id': 800602, 'process_status': 2, 'parent_process_id': 800601,
        'process_group_id': 800601, 'start_seconds': start_seconds, 'start_microseconds': 123}


@pytest.mark.parametrize('incomplete_flavor', [3, 17])
def test_ambiguous_empty_native_query_rechecks_confirmed_exit(monkeypatch, incomplete_flavor):
    responses = [(3, live_bsd_process_info(), 0)] if incomplete_flavor == 17 else []
    responses += [(incomplete_flavor, 0, 0), (3, 0, errno.ESRCH)]
    queried = install_scripted_darwin_identity_queries(monkeypatch, responses)

    assert native_owned_process_observation.native_process_identity(800602) is None
    assert queried == [response[0] for response in responses]


def test_ambiguous_unique_identity_query_rechecks_zombie_exit(monkeypatch):
    responses = [(3, live_bsd_process_info(), 0), (17, 0, 0),
        (3, {**live_bsd_process_info(), 'process_status': 5}, 0)]
    queried = install_scripted_darwin_identity_queries(monkeypatch, responses)

    assert native_owned_process_observation.native_process_identity(800602) is None
    assert queried == [3, 17, 3]


@pytest.mark.parametrize('incomplete_flavor', [3, 17])
def test_ambiguous_native_query_retries_the_complete_current_identity(monkeypatch, incomplete_flavor):
    responses = [(3, live_bsd_process_info(), 0)] if incomplete_flavor == 17 else []
    responses += [(incomplete_flavor, 0, 0), (3, live_bsd_process_info(200), 0),
        (17, {'unique_process_id': 777}, 0)]
    queried = install_scripted_darwin_identity_queries(monkeypatch, responses)

    assert native_owned_process_observation.native_process_identity(800602) == {'pid': 800602,
        'start_identity': [200, 123], 'unique_process_id': 777,
        'parent_pid': 800601, 'process_group_id': 800601}
    assert queried == [response[0] for response in responses]


@pytest.mark.parametrize('incomplete_flavor', [3, 17])
def test_repeated_incomplete_native_queries_remain_unknown_and_bounded(monkeypatch, incomplete_flavor):
    responses = ([(3, live_bsd_process_info(), 0)] if incomplete_flavor == 17 else [])
    responses = (responses + [(incomplete_flavor, 0, 0)]) * 2
    queried = install_scripted_darwin_identity_queries(monkeypatch, responses)

    with pytest.raises(RuntimeError, match='原生进程.*身份不可用'):
        native_owned_process_observation.native_process_identity(800602)
    assert queried == [response[0] for response in responses]


@pytest.mark.parametrize('denied_flavor', [3, 17])
@pytest.mark.parametrize('denied_errno', [errno.EACCES, errno.EPERM, errno.EIO])
def test_explicit_native_query_errors_remain_unknown_without_exit_inference(
        monkeypatch, denied_flavor, denied_errno):
    responses = [(3, live_bsd_process_info(), 0)] if denied_flavor == 17 else []
    responses += [(denied_flavor, 0, denied_errno)]
    queried = install_scripted_darwin_identity_queries(monkeypatch, responses)

    with pytest.raises(RuntimeError, match=f'errno={denied_errno}'):
        native_owned_process_observation.native_process_identity(800602)
    assert queried == [response[0] for response in responses]


@pytest.mark.skipif(sys.platform != 'darwin', reason='需要真实 macOS libproc 原生退出查询')
def test_real_process_natural_exit_between_native_identity_queries(monkeypatch, tmp_path):
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.buffer.read()'],
        stdin=subprocess.PIPE)
    original_load_library = native_owned_process_observation.ctypes.CDLL
    native_query_receipts = []

    def load_library_with_process_exit_boundary(*args, **kwargs):
        library = original_load_library(*args, **kwargs)
        native_process_info = library.proc_pidinfo

        def process_info(pid, flavor, argument, pointer, size):
            result = native_process_info(pid, flavor, argument, pointer, size)
            observed_errno = ctypes.get_errno()
            native_query_receipts.append({'pid': pid, 'flavor': flavor,
                'returned_bytes': result, 'errno': observed_errno})
            if pid == child.pid and flavor == 3 and result == size and child.poll() is None:
                child.stdin.close()
                assert child.wait(timeout=5) == 0
            ctypes.set_errno(observed_errno)
            return result

        return SimpleNamespace(proc_pidinfo=process_info)

    try:
        monkeypatch.setattr(native_owned_process_observation.ctypes, 'CDLL', load_library_with_process_exit_boundary)
        assert native_owned_process_observation.native_process_identity(child.pid) is None
        assert child.returncode == 0
        assert [item['flavor'] for item in native_query_receipts][:2] == [3, 17]
        (tmp_path / 'native-natural-exit-query-receipts.json').write_text(
            json.dumps(native_query_receipts, ensure_ascii=False, indent=2))
    finally:
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        assert native_owned_process_observation.native_process_identity(child.pid) is None


@pytest.mark.skipif(sys.platform != 'darwin', reason='需要真实 macOS 命令监督及原生身份查询')
def test_single_ambiguous_native_result_does_not_create_permanent_supervision_gap(tmp_path):
    run_directory = tmp_path / 'native-query-recovery-run'
    command_directory = run_directory / 'registered-commands/command-001'
    command_directory.mkdir(parents=True)
    (run_directory / 'run.json').write_text('{}')
    supervisor_script = textwrap.dedent(f'''
        import ctypes
        import os
        from pathlib import Path
        import sys
        from types import SimpleNamespace
        from core.task_snapshot_review import retained_command_supervisor as supervisor

        original_load_library = ctypes.CDLL
        incomplete_query_injected = False
        def load_library(*args, **kwargs):
            library = original_load_library(*args, **kwargs)
            if not args or args[0] != '/usr/lib/libproc.dylib':
                return library
            native_process_info = library.proc_pidinfo
            def process_info(pid, flavor, argument, pointer, size):
                global incomplete_query_injected
                result = native_process_info(pid, flavor, argument, pointer, size)
                if pid != os.getpid() and flavor == 17 and result == size and not incomplete_query_injected:
                    incomplete_query_injected = True
                    ctypes.set_errno(0)
                    return 0
                return result
            return SimpleNamespace(proc_pidinfo=process_info, proc_listpids=library.proc_listpids)
        ctypes.CDLL = load_library
        supervisor.native_host_boot_identity = lambda: 'controlled-native-query-test-boot'
        sys.exit(supervisor.supervise_registered_command(Path({str(run_directory)!r}), Path({str(command_directory)!r}), (),
            [sys.executable, '-B', '-c', 'import time; time.sleep(0.25)']))
    ''')
    supervisor_process = subprocess.Popen([sys.executable, '-B', '-c', supervisor_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    supervision_file = command_directory / 'command-supervision.json'
    try:
        _, supervisor_stderr = supervisor_process.communicate(timeout=10)
        facts = json.loads(supervision_file.read_text())
        assert facts['command_returncode'] == 0
        assert facts['ownership_observation_coverage_status'] == 'complete'
        assert 'ownership_observation_gap_started_at' not in facts
        assert facts['status'] == 'completed'
        assert facts.get('command_native_identity')
        assert facts.get('supervisor_native_identity')
        assert supervisor_process.returncode == 0, supervisor_stderr
    finally:
        if supervisor_process.poll() is None:
            supervisor_process.kill()
            supervisor_process.communicate(timeout=5)
        facts = json.loads(supervision_file.read_text()) if supervision_file.exists() else {}
        command_identity = facts.get('command_native_identity')
        if command_identity:
            current_identity = native_owned_process_observation.native_process_identity(command_identity['pid'])
            if native_owned_process_observation.native_process_start_identity_matches(current_identity, command_identity):
                os.kill(command_identity['pid'], signal.SIGKILL)
                for _ in range(100):
                    current_identity = native_owned_process_observation.native_process_identity(command_identity['pid'])
                    if not native_owned_process_observation.native_process_start_identity_matches(current_identity, command_identity):
                        break
                    time.sleep(0.01)
            assert not native_owned_process_observation.native_process_start_identity_matches(current_identity, command_identity)
        supervisor_identity = facts.get('supervisor_native_identity')
        supervisor_current_identity = native_owned_process_observation.native_process_identity(supervisor_process.pid)
        assert not native_owned_process_observation.native_process_start_identity_matches(supervisor_current_identity,
            supervisor_identity or {'pid': supervisor_process.pid, 'start_identity': None})
        (tmp_path / 'native-query-test-resource-exit-receipts.json').write_text(json.dumps({
            'supervisor_native_identity': supervisor_identity, 'command_native_identity': command_identity,
            'supervisor_native_identity_exited': True,
            'command_native_identity_exited': True if command_identity else None,
            'bound_ports': []}, ensure_ascii=False, indent=2))
