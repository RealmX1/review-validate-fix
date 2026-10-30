"""受控后台命令、后代缺证重采与 Linux 查询范围的回归。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import execution_lifecycle, machine_evidence, native_owned_process_observation as observation
from core.task_snapshot_review import retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from test_task_snapshot_review_workflow import repository


def test_clean_environment_detached_descendant_never_yields_premature_completion(tmp_path):
    run = tmp_path / 'clean-environment-descendant-supervision'
    command_directory = run / 'command-observations'
    command_directory.mkdir(parents=True)
    write_json(run / 'run.json', {})
    child_pid_file, side_effect = run / 'child-pid', run / 'late-side-effect'
    child_code = (f'from pathlib import Path; import os,time; Path({str(child_pid_file)!r}).write_text(str(os.getpid())); '
                  f'time.sleep(1.5); Path({str(side_effect)!r}).touch()')
    command = [sys.executable, '-B', '-c',
               f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],start_new_session=True,env={{}}).wait()']
    supervisor = subprocess.Popen([sys.executable, '-B', retained_command_supervisor.__file__, str(run),
        str(command_directory), '[]', json.dumps(command)], start_new_session=True)
    try:
        supervisor.wait(timeout=4)
        facts = read_json(command_directory / 'command-supervision.json')
        assert child_pid_file.exists()
        assert facts['status'] != 'completed' or side_effect.exists()
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait()
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_recollection_waits_for_prior_unproven_descendants(repository, tmp_path, monkeypatch):
    run = tmp_path / 'recollection-descendant-exit-gate'
    command = [sys.executable, '-B', '-c', 'pass']
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'single flight',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    record['semantic_repair_rounds_used'] = 1
    collection = run / 'incomplete-fixer-machine-validation/round-001'
    command_directory = collection / 'attempt-001/commands/command-001'
    command_directory.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], start_new_session=True)
    child_identity = observation.native_process_identity(child.pid)
    write_json(run / 'registered-supervised-command-directories.json', {'command_directories': [str(command_directory)]})
    write_json(command_directory / 'command-supervision.json', {
        'execution_token': 'retained-execution', 'supervisor_pid': 99999999,
        'supervisor_native_identity': {**observation.native_process_identity(os.getpid()), 'pid': 99999999},
        'process_ownership_scope': observation.REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
        'command_process_lifetime_contract': observation.REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        'status': 'descendant_exit_unproven', 'owned_processes': [child_identity]})
    from core.task_snapshot_review.persistent_artifacts import git_text
    write_json(collection / 'collection-intent.json', {
        'tested_tree': git_text(repository, 'rev-parse', record['candidate_commit'] + '^{tree}'),
        'tested_commit': record['candidate_commit'], 'collection_attempt_count': 1})
    dispatched = []
    monkeypatch.setattr(machine_evidence, 'execute_registered_commands', lambda *args, **kwargs: dispatched.append(args) or {'status': 'passed', 'commands': []})
    try:
        with pytest.raises(machine_evidence.WorkspacePreparationFailed) as pending:
            machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
        assert pending.value.recovery['trigger'] == 'previous_supervisor_and_descendants_exit_proven'
        assert dispatched == []
        assert read_json(collection / 'collection-intent.json')['collection_attempt_count'] == 1
        assert not (collection / 'attempt-002').exists()
        child.kill()
        child.wait()
        result = machine_evidence.collect_incomplete_fixer_machine_validation(run, record, record['candidate_commit'])
        assert read_json(result)['status'] == 'passed'
        assert len(dispatched) == 1
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.parametrize(('started_at', 'parent_pid'), [(100, 1), (201, 800001), (201, 1)])
def test_linux_never_reads_unrelated_process_environment(tmp_path, monkeypatch, started_at, parent_pid):
    proc_root = tmp_path / 'owned-process-fixture'
    supervisor_pid, unrelated_pid = 800001, 800777
    for pid, process_started_at, process_parent_pid in ((supervisor_pid, 200, 1), (unrelated_pid, started_at, parent_pid)):
        directory = proc_root / str(pid)
        task = directory / 'task' / str(pid)
        task.mkdir(parents=True)
        fields = ['S', str(process_parent_pid), str(pid), *(['0'] * 16), str(process_started_at)]
        (directory / 'stat').write_text(f'{pid} (fixture) ' + ' '.join(fields))
        (task / 'children').write_text(str(unrelated_pid) if pid == supervisor_pid and parent_pid == supervisor_pid else '')
    def scoped_path(value):
        assert value.startswith('/proc/')
        assert 'environ' not in value
        if parent_pid != supervisor_pid:
            assert str(unrelated_pid) not in value
        return proc_root / value.removeprefix('/proc/')
    monkeypatch.setattr(observation, 'Path', scoped_path)
    monkeypatch.setattr(observation.sys, 'platform', 'linux')
    def denied_environment(path):
        pytest.fail('不得读取进程环境')
    monkeypatch.setattr(Path, 'read_bytes', denied_environment)
    owned = observation.owned_process_snapshot(supervisor_pid, {},
        supervisor_native_identity={'pid': supervisor_pid, 'start_identity': '200'})
    assert set(owned) == ({unrelated_pid} if parent_pid == supervisor_pid else set())


def test_final_validation_cannot_pass_before_clean_environment_descendant_exits(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review.workflow import _validate
    monkeypatch.setattr(execution_lifecycle, 'process_start_identity', lambda *args, **kwargs: None)
    monkeypatch.setattr(execution_lifecycle, 'observe_owned_process_group_descendants', lambda *args: None)
    (repository / 'task.py').write_text('value = 2\n')
    child_pid_file = tmp_path / 'validation-descendant-pid'
    child_code = (f'from pathlib import Path; import os,time; Path({str(child_pid_file)!r}).write_text(str(os.getpid())); '
                  'time.sleep(1.5); Path("task.py").write_text("value = 99\\n")')
    command = [sys.executable, '-B', '-c',
        f'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],start_new_session=True,env={{}}).wait()']
    run = tmp_path / 'authoritative-clean-environment-validation'
    record = create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'exact validation tree',
        run_directory=run, execution_plan={'fix': True, 'validation_commands': [command]})
    try:
        with exclusive_lock(run / 'workflow.lock') as lock:
            try:
                result = _validate(run, record, [command], lock)
                assert result['status'] == 'candidate_changed'
            except RuntimeError as error:
                assert '后代退出证据未知' in str(error)
                assert read_json(run / 'validation.json')['status'] == 'machine_evidence_pending'
        assert child_pid_file.exists()
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_unknown_ownership_preserves_proven_owned_descendants_for_cancellation(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], start_new_session=True)
    identity = observation.native_process_identity(child.pid)
    error = RuntimeError('已登记后代的原生查询失败')
    error.observed_owned_processes = {child.pid: identity}
    write_json(tmp_path / 'command-supervision.json', {'execution_token': 'owned-execution',
        'supervisor_pid': 99999999, 'supervisor_native_identity': {'pid': 99999999, 'start_identity': [1, 0]},
        'owned_processes': []})
    def unavailable_observation(*args, **kwargs):
        raise error
    monkeypatch.setattr(retained_command_supervisor, 'owned_process_snapshot', unavailable_observation)
    try:
        result = retained_command_supervisor.terminate_supervised_command_descendants(tmp_path)
        assert child.wait(timeout=1) == -signal.SIGKILL
        assert result[0]['pid'] == child.pid
        assert result[-1]['cancellation_status'] == 'unknown'
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
