"""定时启动在重启、未知回执和相同故障下保持绑定身份及防重复。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/start_temporary_codex_reviewer_cleanup_task.py'


@pytest.fixture
def scheduled_cleanup(tmp_path):
    executable = tmp_path / 'kanban-cli-substitute.py'
    observation = tmp_path / 'kanban-observation.json'
    observation.write_text(json.dumps({'mode': 'available', 'start_calls': 0, 'get_calls': 0,
        'task': {'id': 'cleanup', 'createdAt': 123, 'column': 'backlog', 'updatedAt': 1, 'session': None}}))
    executable.write_text('import json,pathlib,sys\n'
        f'path=pathlib.Path({str(observation)!r})\n'
        'state=json.loads(path.read_text())\n'
        "action=sys.argv[2]\n"
        "if action=='get':\n"
        " state['get_calls']+=1; path.write_text(json.dumps(state))\n"
        " if state['mode']=='unavailable' or state['mode']=='post-start-get-failure' and state['get_calls']%2==0: print('runtime unavailable',file=sys.stderr); sys.exit(1)\n"
        " print(json.dumps({'ok':True,'workspacePath':sys.argv[-1],'task':state['task']}))\n"
        "else:\n"
        " state['start_calls']+=1\n"
        " if state['mode'] in ('start-failure','start-failure-session','post-start-get-failure'):\n"
        "  if state['mode']=='start-failure-session': state['task'].update({'column':'in_progress','session':{'startedAt':2000,'state':'error','pid':None}})\n"
        "  path.write_text(json.dumps(state)); print('agent unavailable',file=sys.stderr); sys.exit(1)\n"
        " state['task'].update({'column':'in_progress','session':{'startedAt':2000,'state':'running','pid':42}})\n"
        " path.write_text(json.dumps(state))\n"
        " if state['mode']=='lost-receipt': print('transport interrupted',file=sys.stderr); sys.exit(1)\n"
        " print(json.dumps({'ok':True}))\n")
    runtime_generations = tmp_path / 'runtime-process-instance-heartbeats'
    runtime_generations.mkdir()
    schedule = tmp_path / 'cleanup-schedule.json'
    schedule.write_text(json.dumps({'task_id': 'cleanup', 'task_created_at': 123,
        'project_path': str(tmp_path), 'not_before_epoch_seconds': 0,
        'kanban_command_prefix': [sys.executable, str(executable)],
        'runtime_generation_directory': str(runtime_generations)}))
    return schedule, observation, runtime_generations


def run_schedule(schedule):
    result = subprocess.run([sys.executable, str(SCRIPT), '--schedule-file', str(schedule)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads((schedule.parent / 'cleanup-launch-state.json').read_text())


def change_observation(path, **changes):
    observation = json.loads(path.read_text())
    observation.update(changes)
    path.write_text(json.dumps(observation))


def test_not_before_deadline_then_restart_catches_up_once(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    config = json.loads(schedule.read_text())
    config['not_before_epoch_seconds'] = 10**12
    schedule.write_text(json.dumps(config))
    assert run_schedule(schedule)['status'] == 'waiting_for_deadline'
    assert json.loads(observation.read_text())['get_calls'] == 0
    config['not_before_epoch_seconds'] = 0
    schedule.write_text(json.dumps(config))
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert json.loads(observation.read_text())['start_calls'] == 1


def test_runtime_unavailable_keeps_recovery_then_automatically_starts(scheduled_cleanup):
    schedule, observation, generations = scheduled_cleanup
    change_observation(observation, mode='unavailable')
    for _ in range(5):
        state = run_schedule(schedule)
    assert state['status'] == 'waiting_for_condition_change'
    assert state['failure_retry_budgets_by_action']['get']['attempt_count'] == 3
    assert json.loads(observation.read_text())['get_calls'] == 3
    assert state['recovery_condition']
    assert json.loads(observation.read_text())['start_calls'] == 0
    change_observation(observation, mode='available')
    (generations / 'restarted-runtime.json').write_text('{}')
    assert run_schedule(schedule)['status'] == 'launch_verified'


def test_start_failure_has_three_attempt_budget_and_runtime_change_restores_it(scheduled_cleanup):
    schedule, observation, generations = scheduled_cleanup
    change_observation(observation, mode='start-failure')
    for _ in range(5):
        assert run_schedule(schedule)['status'] == 'waiting_for_condition_change'
    assert json.loads(observation.read_text())['start_calls'] == 3
    change_observation(observation, mode='unavailable')
    assert run_schedule(schedule)['status'] == 'waiting_for_condition_change'
    change_observation(observation, mode='available')
    assert run_schedule(schedule)['status'] == 'waiting_for_condition_change'
    assert json.loads(observation.read_text())['start_calls'] == 3
    (generations / 'restarted-runtime.json').write_text('{}')
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert json.loads(observation.read_text())['start_calls'] == 4


def test_lost_start_receipt_is_reconciled_without_restarting(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    change_observation(observation, mode='lost-receipt')
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert json.loads(observation.read_text())['start_calls'] == 1


def test_start_attempt_budget_survives_post_start_observation_failure(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    change_observation(observation, mode='post-start-get-failure')
    for _ in range(6):
        run_schedule(schedule)
    assert json.loads(observation.read_text())['start_calls'] == 3


def test_failed_start_session_retries_without_claiming_a_native_launch(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    change_observation(observation, mode='start-failure-session')
    assert run_schedule(schedule)['status'] == 'waiting_for_condition_change'
    change_observation(observation, mode='available')
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert json.loads(observation.read_text())['start_calls'] == 2


def test_running_session_without_native_evidence_is_only_observed(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    state = json.loads(observation.read_text())
    state['task']['session'] = {'startedAt': 2000, 'state': 'running', 'pid': None, 'runtimeSessionIncarnationId': 'allocated-only'}
    observation.write_text(json.dumps(state))
    assert run_schedule(schedule)['status'] == 'waiting_for_launch_evidence'
    assert run_schedule(schedule)['status'] == 'waiting_for_launch_evidence'
    assert json.loads(observation.read_text())['start_calls'] == 0


def test_explicitly_stopped_session_is_never_restarted(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    state = json.loads(observation.read_text())
    state['task']['session'] = {'startedAt': 2000, 'state': 'stopped', 'pid': None}
    observation.write_text(json.dumps(state))
    assert run_schedule(schedule)['status'] == 'task_no_longer_eligible'
    assert json.loads(observation.read_text())['start_calls'] == 0


@pytest.mark.parametrize('field,value,status', [('createdAt', 999, 'task_identity_mismatch'),
    ('column', 'done', 'task_no_longer_eligible'), ('taskShelvingState', {'isShelved': True}, 'task_no_longer_eligible')])
def test_identity_and_explicit_task_state_prevent_launch(scheduled_cleanup, field, value, status):
    schedule, observation, _ = scheduled_cleanup
    state = json.loads(observation.read_text())
    state['task'][field] = value
    observation.write_text(json.dumps(state))
    assert run_schedule(schedule)['status'] == status
    assert json.loads(observation.read_text())['start_calls'] == 0


def test_unshelved_task_recovers_and_launches(scheduled_cleanup):
    schedule, observation, _ = scheduled_cleanup
    state = json.loads(observation.read_text())
    state['task']['taskShelvingState'] = {'isShelved': True, 'lastChangedAt': 123}
    observation.write_text(json.dumps(state))
    assert run_schedule(schedule)['status'] == 'task_no_longer_eligible'
    state['task']['taskShelvingState'] = {'isShelved': False, 'lastChangedAt': 124}
    observation.write_text(json.dumps(state))
    assert run_schedule(schedule)['status'] == 'launch_verified'
    assert json.loads(observation.read_text())['start_calls'] == 1
