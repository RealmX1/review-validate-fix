"""监督者崩溃、归属查询故障预算及取消控制证据边界的回归。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from core.task_snapshot_review import execution_lifecycle, native_owned_process_observation, retained_command_supervisor
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.mark.parametrize('exit_recorded_parent', [False, True])
def test_stop_rediscovers_unrecorded_detached_child_after_supervisor_crash(tmp_path, exit_recorded_parent):
    run = tmp_path / 'supervisor-crash-recovery'
    command_directory = run / 'commands/command-001'
    command_directory.mkdir(parents=True)
    write_json(run / 'run.json', {})
    write_json(run / 'registered-supervised-command-directories.json', {'command_directories': [str(command_directory)]})
    release_spawn, child_started = run / 'release-child-spawn', run / 'detached-child-pid'
    child_code = f'from pathlib import Path; import os,time; Path({str(child_started)!r}).write_text(str(os.getpid())); time.sleep(30)'
    parent_code = (
        'from pathlib import Path; import subprocess,sys,time\n'
        f'while not Path({str(release_spawn)!r}).exists(): time.sleep(0.01)\n'
        f'subprocess.Popen([sys.executable,"-B","-c",{child_code!r}],start_new_session=True)\n'
        'time.sleep(30)\n'
    )
    command = [sys.executable, '-B', '-c', parent_code]
    supervisor = subprocess.Popen([sys.executable, '-B', retained_command_supervisor.__file__, str(run),
        str(command_directory), '[]', json.dumps(command)], start_new_session=True)
    facts = {}
    try:
        supervision_file = command_directory / 'command-supervision.json'
        wait_until(lambda: supervision_file.exists() and bool(read_json(supervision_file).get('owned_processes')))
        os.kill(supervisor.pid, signal.SIGSTOP)
        facts = read_json(supervision_file)
        release_spawn.touch()
        wait_until(child_started.exists)
        child_pid = int(child_started.read_text())
        assert child_pid not in {item['pid'] for item in facts['owned_processes']}
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=1)
        if exit_recorded_parent:
            os.kill(facts['command_pid'], signal.SIGKILL)
            wait_until(lambda: native_owned_process_observation.native_process_identity(facts['command_pid']) is None)
        evidence = execution_lifecycle.revoke_execution(run, '监督者崩溃后取消未登记后代')
        wait_until(lambda: native_owned_process_observation.native_process_identity(child_pid) is None, timeout=1)
        assert any(item.get('pid') == child_pid and item['cancellation_status'] == 'termination_requested'
                   for item in evidence['supervised_descendants'])
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait()
        identities = [item['pid'] for item in facts.get('owned_processes', [])]
        if child_started.exists():
            identities.append(int(child_started.read_text()))
        for pid in identities:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_persistent_observation_failure_has_cleanup_budget_independent_of_role_deadline(tmp_path):
    run = tmp_path / 'bounded-native-observation-outage'
    command_directory = run / 'commands/command-001'
    command_directory.mkdir(parents=True)
    write_json(run / 'run.json', {})
    script = (
        'from pathlib import Path; from unittest.mock import patch\n'
        'from core.task_snapshot_review import retained_command_supervisor as supervisor\n'
        'attempts=0\n'
        'def observe(*args):\n'
        ' global attempts\n'
        ' attempts+=1\n'
        ' if attempts>1: raise RuntimeError("派发后原生归属查询持续失败")\n'
        ' return {}\n'
        'with patch.object(supervisor,"owned_process_snapshot",side_effect=observe):\n'
        f' supervisor.supervise_registered_command(Path({str(run)!r}),Path({str(command_directory)!r}),'
        f'(),{[sys.executable, "-B", "-c", "pass"]!r})\n'
    )
    started_at = time.monotonic()
    supervisor = subprocess.Popen([sys.executable, '-B', '-c', script], start_new_session=True)
    try:
        supervisor.wait(timeout=1.5)
        facts = read_json(command_directory / 'command-supervision.json')
        assert time.monotonic() - started_at < 1.5
        assert facts['command_returncode'] == 0
        assert facts['cleanup_observation_attempt_count'] <= 10
        assert facts['status'] == 'descendant_exit_unproven'
        assert facts['recovery_condition'] == 'native_process_ownership_observation_available'
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait()


@pytest.mark.parametrize('forged_identity', [False, True])
def test_candidate_source_supervision_file_is_never_cancellation_evidence(tmp_path, forged_identity):
    run = tmp_path / 'candidate-control-evidence-boundary'
    run.mkdir()
    write_json(run / 'run.json', {})
    unrelated = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        facts = {'fixture': True}
        if forged_identity:
            facts = {'execution_token': 'unregistered-execution', 'supervisor_pid': 99999999,
                     'owned_processes': [native_owned_process_observation.native_process_identity(unrelated.pid)]}
        write_json(run / 'reviewer-1/attempt-001/workspace/fixtures/command-supervision.json', facts)
        evidence = execution_lifecycle.revoke_execution(run, '忽略候选源码中的普通 fixture')
        assert evidence['supervised_descendants'] == []
        assert (run / 'cancellation-observations.json').exists()
        assert unrelated.poll() is None
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait()


def test_reused_supervisor_pid_cannot_anchor_foreign_process_group(monkeypatch):
    supervisor_pid, foreign_child_pid, owned_orphan_pid = 800501, 800502, 800503
    snapshot = {
        supervisor_pid: {'pid': supervisor_pid, 'parent_pid': 1, 'process_group_id': supervisor_pid,
            'start_identity': [200, 0], 'execution_token_matches': False},
        foreign_child_pid: {'pid': foreign_child_pid, 'parent_pid': supervisor_pid, 'process_group_id': supervisor_pid,
            'start_identity': [201, 0], 'execution_token_matches': False},
        owned_orphan_pid: {'pid': owned_orphan_pid, 'parent_pid': 1, 'process_group_id': owned_orphan_pid,
            'start_identity': [102, 0], 'execution_token_matches': True},
    }
    monkeypatch.setattr(native_owned_process_observation, 'native_process_snapshot', lambda *args, **kwargs: snapshot)
    owned = native_owned_process_observation.owned_process_snapshot('original-execution', supervisor_pid, {},
        supervisor_native_identity={'pid': supervisor_pid, 'start_identity': [100, 0]})
    assert set(owned) == {owned_orphan_pid}
