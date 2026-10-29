#!/usr/bin/env python3
"""供持久 LaunchAgent 到期启动精确清理任务；只依赖 Python 标准库。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time


def write_json(path: Path, value: dict) -> None:
    replacement = path.with_suffix('.atomic-replacement')
    replacement.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    replacement.replace(path)


def run_scheduled_cleanup_start(schedule_file: Path) -> dict:
    directory = schedule_file.parent
    with (directory / 'cleanup-launch.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        schedule = json.loads(schedule_file.read_text())
        state_file = directory / 'cleanup-launch-state.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if state.get('status') == 'launch_verified':
            return state

        def save(status, **details):
            state.update(status=status, observed_at=time.time(), **details)
            write_json(state_file, state)
            return state

        if time.time() < schedule['not_before_epoch_seconds']:
            return save('waiting_for_deadline')

        command_prefix = schedule['kanban_command_prefix']
        executable_observations = []
        for argument in command_prefix:
            path = Path(argument)
            try:
                stat = path.stat()
                executable_observations.append([argument, stat.st_ino, stat.st_mtime_ns, stat.st_size])
            except OSError:
                executable_observations.append([argument, 'unavailable'])
        generations = sorted(path.name for path in Path(schedule['runtime_generation_directory']).glob('*.json'))
        environment_fingerprint = hashlib.sha256(json.dumps([schedule, executable_observations, generations], sort_keys=True).encode()).hexdigest()

        def command(action):
            try:
                result = subprocess.run([*command_prefix, 'task', action, '--task-id', schedule['task_id'],
                    '--project-path', schedule['project_path']], capture_output=True, text=True, timeout=30)
                return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
            except (OSError, subprocess.SubprocessError) as error:
                return {'returncode': None, 'error': str(error)}

        def response(receipt):
            try:
                value = json.loads(receipt.get('stdout', ''))
                return value if receipt['returncode'] == 0 and value.get('ok') is True else None
            except (ValueError, AttributeError):
                return None

        def failure(action, receipt):
            budgets = state.setdefault('failure_retry_budgets_by_action', {})
            budget = budgets.get(action, {})
            count = budget.get('attempt_count', 0) if budget.get('environment_fingerprint') == environment_fingerprint else 0
            if count < 3:
                receipts = directory / 'cleanup-launch-attempt-receipts'
                receipts.mkdir(exist_ok=True)
                write_json(receipts / f'observation-{len(list(receipts.glob("*.json"))) + 1:03d}.json',
                           {'action': action, 'observed_at': time.time(), **receipt})
            budgets[action] = {'environment_fingerprint': environment_fingerprint,
                               'attempt_count': count if action == 'start' else min(3, count + 1)}
            return save('waiting_for_condition_change', last_failure_action=action,
                recovery_condition='运行时重新可用；达到三次启动失败后，CLI、定时配置或 runtime generation 改变才恢复启动预算。相同环境下失败的查询也最多三次。')

        def observe_task(receipt):
            observed = response(receipt)
            if observed is None:
                return None, failure('get', receipt)
            task = observed.get('task') or {}
            if (task.get('id') != schedule['task_id'] or task.get('createdAt') != schedule['task_created_at']
                    or Path(observed.get('workspacePath', '')).resolve() != Path(schedule['project_path']).resolve()):
                return None, save('task_identity_mismatch', recovery_condition='恢复绑定的 task incarnation 和项目身份后重验，不替换任务。')
            session = task.get('session') or {}
            if task.get('column') not in {'backlog', 'in_progress'} or (task.get('taskShelvingState') or {}).get('isShelved') is True:
                return None, save('task_no_longer_eligible', recovery_condition='任务恢复可启动状态后重验；不撤销人工停止或搁置。')
            if session.get('startedAt'):
                if session.get('pid') or session.get('agentNativeConversationSessionIdentity'):
                    return None, save('launch_verified', task_session_observation=session, task_observation_receipt=receipt)
                if session.get('state') in {'stopped', 'interrupted', 'review'}:
                    return None, save('task_no_longer_eligible', recovery_condition='保留既有会话的停止或人工接手状态，不重新启动。')
                if not (session.get('state') in {'error', 'failed'} and state.get('launch_intent_recorded_at')):
                    return None, save('waiting_for_launch_evidence', task_session_observation=session,
                        recovery_condition='等待真实进程或原生会话身份；已记录的启动会话不重复发起。')
            return task, None

        get_budget = state.get('failure_retry_budgets_by_action', {}).get('get', {})
        if (get_budget.get('attempt_count', 0) >= 3
                and get_budget.get('environment_fingerprint') == environment_fingerprint):
            return state
        task, observation_result = observe_task(command('get'))
        if observation_result is not None:
            return observation_result
        start_budget = state.get('failure_retry_budgets_by_action', {}).get('start', {})
        if (start_budget.get('attempt_count', 0) >= 3
                and start_budget.get('environment_fingerprint') == environment_fingerprint):
            return state
        # 先持久记录意图；进程中断后先核对真实会话，避免重复发起已启动的任务。
        prior_start_count = start_budget.get('attempt_count', 0) if start_budget.get('environment_fingerprint') == environment_fingerprint else 0
        state.setdefault('failure_retry_budgets_by_action', {})['start'] = {
            'environment_fingerprint': environment_fingerprint, 'attempt_count': prior_start_count + 1}
        save('launch_intent_recorded', launch_intent_recorded_at=time.time())
        receipt = command('start')
        receipts = directory / 'cleanup-start-command-receipts'
        receipts.mkdir(exist_ok=True)
        write_json(receipts / f'attempt-{len(list(receipts.glob("*.json"))) + 1:03d}.json', receipt)
        write_json(directory / 'cleanup-task-start-receipt.json', receipt)
        task, observation_result = observe_task(command('get'))
        if observation_result is not None:
            return observation_result
        return failure('start', receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schedule-file', type=Path, required=True)
    arguments = parser.parse_args()
    state = run_scheduled_cleanup_start(arguments.schedule_file.expanduser().resolve())
    print(json.dumps({'status': state['status']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
