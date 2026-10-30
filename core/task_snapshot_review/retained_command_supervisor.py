"""独立监督所属命令及跨 session 后代，保留进程归属与持久撤销。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.task_snapshot_review.execution_lifecycle import ensure_execution_not_revoked, ExecutionRevoked
from core.task_snapshot_review.native_owned_process_observation import (
    REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
    native_process_identity, native_process_start_identity_matches, owned_process_snapshot, terminate_observed_owned_processes,
)
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json


def _valid_reconciled_process_identity(identity: object) -> bool:
    # macOS/Linux pid_t 为有符号 32 位，超范围整数会被 ctypes 截断。
    if (not isinstance(identity, dict) or type(identity.get('pid')) is not int
            or not 0 < identity['pid'] <= 2**31 - 1):
        return False
    start = identity.get('start_identity')
    return (isinstance(start, str) and start.isascii() and start.isdigit()
        or isinstance(start, list) and len(start) == 2
        and all(type(value) is int and value >= 0 for value in start) and start[1] < 1_000_000)


def terminate_supervised_command_descendants(command_directory: Path) -> list[dict]:
    facts = read_json(command_directory / 'command-supervision.json')
    recorded = {item['pid']: item for item in facts.get('owned_processes', [])}
    if facts.get('command_native_identity'):
        recorded.setdefault(facts['command_pid'], facts['command_native_identity'])
    observation_error = None
    try:
        reconciliation_file = command_directory / 'descendant-exit-reconciliation.json'
        if reconciliation_file.exists():
            reconciled = read_json(reconciliation_file).get('owned_processes', [])
            if not isinstance(reconciled, list):
                raise ValueError('追加对账 owned_processes 必须是身份列表')
            for item in reconciled:
                if not _valid_reconciled_process_identity(item):
                    observation_error = '追加对账包含无效原生进程身份'
                    continue
                previous = recorded.get(item['pid'])
                if previous:
                    if previous['start_identity'] == item['start_identity']:
                        continue
                    # 同 PID 的补证不能抹去原登记；只选择当前仍匹配的启动身份。
                    try:
                        current = native_process_identity(item['pid'])
                    except (OSError, RuntimeError) as error:
                        observation_error = str(error)
                        continue
                    if not native_process_start_identity_matches(current, item):
                        continue
                recorded[item['pid']] = item
        supervisor_identity = facts.get('supervisor_native_identity')
        if supervisor_identity:
            recorded.update(owned_process_snapshot(facts['supervisor_pid'], recorded,
                supervisor_native_identity=supervisor_identity))
        else:
            observation_error = '缺少监督者原生启动身份，无法补查未登记后代'
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        recorded.update(getattr(error, 'observed_owned_processes', {}))
        observation_error = str(error)
    observations = terminate_observed_owned_processes(recorded)
    if observation_error:
        observations.append({'cancellation_status': 'unknown', 'error': observation_error})
    write_json(command_directory / 'descendant-cancellation-observations.json', {
        'execution_token': facts['execution_token'], 'processes': observations, 'observed_at': time.time(),
        'recovery_condition': 'native_process_ownership_observation_available'})
    return observations


def supervise_registered_command(run_directory: Path, command_directory: Path, inherited_descriptors: tuple[int, ...], command: list[str]) -> int:
    process_group_id = os.getpgrp()
    if process_group_id != os.getpid():
        raise ValueError('独立监督者必须拥有自己的进程组')
    execution_token = os.urandom(24).hex()
    supervision_file = command_directory / 'command-supervision.json'
    facts = {'status': 'starting', 'supervisor_pid': os.getpid(), 'process_group_id': process_group_id,
             'argv': command, 'execution_token': execution_token,
             'process_ownership_scope': REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
             'command_process_lifetime_contract': REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
             'owned_process_exit_status': 'pending'}
    write_json(supervision_file, facts)
    child = None
    owned = {}
    ownership_observation_deferred_until_command_exit = False

    def observe_owned_processes():
        for attempt in range(5):
            try:
                if child is not None and child.poll() is None:
                    direct_identity = native_process_identity(child.pid)
                    if direct_identity is not None:
                        owned[child.pid] = direct_identity
                        facts['command_native_identity'] = direct_identity
                return owned_process_snapshot(os.getpid(), owned,
                    supervisor_native_identity=facts['supervisor_native_identity'])
            except (OSError, RuntimeError) as error:
                owned.update(getattr(error, 'observed_owned_processes', {}))
                facts['owned_processes'] = list(owned.values())
                if attempt == 4:
                    raise
                time.sleep(0.05)

    def request_termination(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise ExecutionRevoked('监督者收到取消信号')

    def terminate_owned_processes():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            owned.update(owned_process_snapshot(os.getpid(), owned,
                supervisor_native_identity=facts['supervisor_native_identity']))
        except (OSError, RuntimeError) as error:
            owned.update(getattr(error, 'observed_owned_processes', {}))
            facts['cleanup_error'] = str(error)
        # Popen 的未回收直接子进程句柄是独立归属证据，不依赖全局进程查询。
        if child is not None and child.poll() is None:
            try:
                child.kill()
                facts['direct_command_cancellation_status'] = 'termination_requested'
            except OSError as error:
                facts['direct_command_cancellation_status'] = 'unknown'
                facts['direct_command_cancellation_error'] = str(error)
        # 限定查询失败时先按已保存启动身份清理，不能先杀死持有归属事实的监督者。
        observations = terminate_observed_owned_processes(owned)
        maximum_cleanup_observations = 10
        cleanup_observation_deadline = time.monotonic() + 0.5
        for attempt in range(maximum_cleanup_observations):
            if attempt and time.monotonic() >= cleanup_observation_deadline:
                break
            facts['cleanup_observation_attempt_count'] = attempt + 1
            try:
                current = owned_process_snapshot(os.getpid(), owned,
                    supervisor_native_identity=facts['supervisor_native_identity'])
                observations = terminate_observed_owned_processes(current)
                facts['owned_processes'] = list(current.values())
                facts['descendant_cancellation_observations'] = observations
                write_json(supervision_file, facts)
                if not any(item['cancellation_status'] == 'unknown' for item in observations):
                    os.killpg(process_group_id, signal.SIGKILL)
            except (OSError, RuntimeError) as error:
                owned.update(getattr(error, 'observed_owned_processes', {}))
                observations = terminate_observed_owned_processes(owned)
                facts['owned_processes'] = list(owned.values())
                facts['cleanup_error'] = str(error)
            if attempt < maximum_cleanup_observations - 1:
                time.sleep(min(0.05, max(0, cleanup_observation_deadline - time.monotonic())))
        facts.update({'status': 'descendant_exit_unproven', 'descendant_cancellation_observations': observations,
            'owned_process_exit_status': 'unknown',
            'recovery_condition': 'native_process_ownership_observation_available', 'maximum_unchanged_retries': 1})
        write_json(supervision_file, facts)
        os.killpg(process_group_id, signal.SIGKILL)

    signal.signal(signal.SIGTERM, request_termination)
    try:
        # 启动前确认等价原生查询可用，查询失败保存恢复条件。
        facts['supervisor_native_identity'] = native_process_identity(os.getpid())
        write_json(supervision_file, facts)
        observe_owned_processes()
        with exclusive_lock(run_directory / 'execution-revocation.lock'):
            ensure_execution_not_revoked(run_directory)
            facts['command_launch_intent_recorded'] = True
            write_json(supervision_file, facts)
            try:
                child = subprocess.Popen(command, pass_fds=inherited_descriptors)
            except OSError as error:
                facts.update({'command_execution_started': False, 'command_native_launch_error': {
                    'type': type(error).__name__, 'errno': error.errno, 'message': str(error)}})
                raise
        facts.update({'status': 'running', 'command_pid': child.pid, 'command_execution_started': True})
        write_json(supervision_file, facts)
        while True:
            ensure_execution_not_revoked(run_directory)
            returncode = child.poll()
            if not ownership_observation_deferred_until_command_exit or returncode is not None:
                try:
                    owned = observe_owned_processes()
                    ownership_observation_deferred_until_command_exit = False
                except (OSError, RuntimeError) as error:
                    if child.poll() is not None:
                        raise
                    # 已登记的活跃命令继续执行；子进程退出是重新核验的事实触发条件。
                    ownership_observation_deferred_until_command_exit = True
                    facts['last_ownership_observation_error'] = str(error)
            facts['ownership_observation_deferred_until_command_exit'] = ownership_observation_deferred_until_command_exit
            facts.update({'command_returncode': returncode, 'owned_processes': list(owned.values())})
            write_json(supervision_file, facts)
            if returncode is not None and not ownership_observation_deferred_until_command_exit and not owned:
                facts.update({'status': 'completed', 'returncode': returncode, 'owned_process_exit_status': 'exited'})
                write_json(supervision_file, facts)
                return returncode
            time.sleep(0.02)
    except ExecutionRevoked:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        facts.update({'status': 'termination_requested', 'command_returncode': child.poll() if child else None})
        write_json(supervision_file, facts)
        terminate_owned_processes()
        return 124
    except BaseException as error:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        facts.update({'status': 'descendant_exit_unproven', 'command_returncode': child.poll() if child else None,
            'owned_process_exit_status': 'unknown',
            'error': str(error), 'recovery_condition': 'native_process_ownership_observation_available',
            'maximum_unchanged_retries': 1})
        write_json(supervision_file, facts)
        if child is not None:
            terminate_owned_processes()
        return 125


if __name__ == '__main__':
    sys.exit(supervise_registered_command(Path(sys.argv[1]), Path(sys.argv[2]),
        tuple(json.loads(sys.argv[3])), json.loads(sys.argv[4])))
