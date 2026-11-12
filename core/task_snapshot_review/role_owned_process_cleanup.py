"""保留角色业务结果，按原生身份有界回收其资源并接续持久恢复。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .native_owned_process_observation import (
    REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
    native_host_boot_identity, native_process_identity, native_process_start_identity_matches, owned_process_snapshot,
    recorded_native_process_identity_is_valid,
)
from .persistent_artifacts import exclusive_lock, read_json, write_json


def role_owned_process_cleanup_observation(attempt_directory: Path) -> dict:
    supervision_file = attempt_directory / 'command-supervision.json'
    if not supervision_file.exists():
        outcome_file = attempt_directory / 'outcome.json'
        if outcome_file.exists() and read_json(outcome_file).get('role_owned_process_cleanup_status') == 'exited':
            return {'status': 'exited', 'source': 'role_command_supervisor_not_dispatched'}
        return {'status': 'unknown', 'recovery_condition': 'registered_role_supervision_evidence_available'}
    facts = read_json(supervision_file)
    cleanup_file = attempt_directory / 'role-owned-process-cleanup.json'
    if cleanup_file.exists():
        observation = read_json(cleanup_file)
        if observation.get('execution_token') == facts.get('execution_token'):
            return observation
    return {'status': 'pending', 'execution_token': facts.get('execution_token')}


def collect_role_owned_process_cleanup_facts(run_directory: Path) -> dict:
    attempts = []
    for request in sorted(run_directory.glob('*/attempt-*/request.json')):
        try:
            observation = role_owned_process_cleanup_observation(request.parent)
        except (OSError, ValueError, TypeError) as error:
            observation = {'status': 'unknown', 'error': str(error)}
        attempts.append({'attempt_directory': str(request.parent),
            'evidence_file': str(request.parent / 'role-owned-process-cleanup.json'),
            **{key: observation[key] for key in ('status', 'ownership_observation_coverage_status',
                'recovery_condition', 'retry_not_before', 'maximum_unchanged_retries') if key in observation}})
    statuses = {item['status'] for item in attempts}
    status = next((candidate for candidate in ('unknown', 'terminating', 'pending') if candidate in statuses),
        'exited' if attempts else 'unknown')
    return {'status': status, 'attempts': attempts}


def cleanup_role_owned_processes(attempt_directory: Path, *, direct_command: subprocess.Popen | None = None,
                                 cancellation_requested: bool = False) -> dict:
    """同一次派发的清理单飞；未知状态只随证据变化或有界健康探针重验。"""
    with exclusive_lock(attempt_directory / 'role-owned-process-cleanup.lock', blocking=False):
        facts = read_json(attempt_directory / 'command-supervision.json')
        if (facts.get('command_process_lifetime_contract') != ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT
                or facts.get('process_ownership_scope') != REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE
                or not facts.get('execution_token') or facts.get('command_directory') != str(attempt_directory.resolve())):
            raise ValueError('缺少精确角色派发的受控资源契约')
        cleanup_file = attempt_directory / 'role-owned-process-cleanup.json'
        previous = role_owned_process_cleanup_observation(attempt_directory)
        if previous['status'] == 'exited':
            return previous
        observation = {'execution_token': facts['execution_token'], 'status': 'terminating',
            'host_boot_identity': facts.get('host_boot_identity'),
            'recovery_condition': 'native_owned_identity_changed_or_host_boot_changed',
            'equivalent_sources': ['registered_native_descendant_identities', 'native_host_boot_identity'],
            'observed_at': time.time()}
        try:
            if facts.get('ownership_observation_coverage_status') == 'unknown':
                from .registered_command_owner_exit_receipt import observe_registered_command_owner_exit_receipt
                observation['ownership_observation_coverage_status'] = 'unknown'
                if facts.get('ownership_observation_gap_started_at') is not None:
                    observation['ownership_observation_gap_started_at'] = facts['ownership_observation_gap_started_at']
                owner_receipt = observe_registered_command_owner_exit_receipt(run_directory=attempt_directory.parents[1], facts=facts)
                observation['owner_exit_receipt'] = owner_receipt
                if owner_receipt['status'] == 'exited':
                    observation.update(status='exited', source=owner_receipt['source'], owned_processes=[],
                        ownership_observation_coverage_status='unknown')
                    write_json(cleanup_file, observation)
                    return observation
            boot_identity = native_host_boot_identity()
            if not facts.get('host_boot_identity'):
                raise RuntimeError('缺少角色派发时的系统启动世代')
            if boot_identity != facts['host_boot_identity']:
                observation.update(status='exited', source='host_boot_changed', owned_processes=[])
                write_json(cleanup_file, observation)
                return observation
            supervisor = facts.get('supervisor_native_identity')
            if not recorded_native_process_identity_is_valid(supervisor):
                raise RuntimeError('缺少角色监督者的原生启动身份')
            if supervisor['pid'] != os.getpid() and native_process_start_identity_matches(
                    native_process_identity(supervisor['pid']), supervisor):
                return previous
            command_identity = facts.get('command_native_identity')
            if command_identity and not recorded_native_process_identity_is_valid(command_identity):
                raise RuntimeError('角色命令的原生启动身份无效')
            if (direct_command is None and not cancellation_requested and command_identity
                    and native_process_start_identity_matches(native_process_identity(command_identity['pid']), command_identity)):
                return previous
            recorded = {item['pid']: item for item in facts.get('owned_processes', [])}
            recorded.update({item['pid']: item for item in previous.get('owned_processes', [])})
            if facts.get('command_native_identity'):
                recorded.setdefault(facts['command_pid'], facts['command_native_identity'])
            if any(not recorded_native_process_identity_is_valid(identity) for identity in recorded.values()):
                raise RuntimeError('角色资源包含无效的原生启动身份')
            current_identities = []
            for pid in sorted(recorded):
                try:
                    current_identities.append(native_process_identity(pid))
                except (OSError, RuntimeError) as error:
                    current_identities.append({'pid': pid, 'error': str(error)})
            recovery_input = json.dumps([boot_identity, current_identities,
                facts.get('ownership_observation_coverage_status'), facts.get('status')], sort_keys=True)
            unchanged_retries = previous.get('unchanged_recovery_attempt_count', 0) if previous.get('recovery_input') == recovery_input else 0
            if direct_command is None and previous['status'] == 'unknown' and previous.get('recovery_input') == recovery_input and (
                    unchanged_retries >= 3 or time.time() < previous.get('retry_not_before', 0)):
                return previous
            observation.update(recovery_input=recovery_input, unchanged_recovery_attempt_count=unchanged_retries + 1)
            coverage_unknown = facts.get('ownership_observation_coverage_status') != 'complete' or (
                supervisor['pid'] != os.getpid() and facts.get('status') != 'completed')
            observation['ownership_observation_coverage_status'] = 'unknown' if coverage_unknown else 'complete'
            write_json(cleanup_file, observation)
            started = time.monotonic()
            signalled = set()
            observations = {}
            signal_requests = []
            last_saved = None
            while True:
                if direct_command is not None:
                    direct_command.poll()
                query_error = None
                try:
                    recorded = owned_process_snapshot(supervisor['pid'], recorded, supervisor_native_identity=supervisor)
                except (OSError, RuntimeError) as error:
                    recorded.update(getattr(error, 'observed_owned_processes', {}))
                    query_error = str(error)
                    coverage_unknown = True
                elapsed = time.monotonic() - started
                requested_signal = signal.SIGTERM if elapsed < 2 else signal.SIGKILL
                live = {}
                for pid, identity in recorded.items():
                    try:
                        current = native_process_identity(pid)
                        if current is None:
                            status = 'exited'
                        elif not native_process_start_identity_matches(current, identity):
                            status = 'identity_changed_no_signal'
                        elif pid == os.getpid():
                            raise RuntimeError('不能向清理器自身发送角色资源信号')
                        else:
                            live[pid] = identity
                            key = (pid, json.dumps(identity['start_identity']), requested_signal)
                            if key not in signalled:
                                os.kill(pid, requested_signal)
                                signalled.add(key)
                                signal_requests.append({'pid': pid, 'start_identity': identity['start_identity'],
                                    'signal': requested_signal.name})
                            status = 'termination_requested'
                    except ProcessLookupError:
                        status = 'exited'
                        live.pop(pid, None)
                    except (OSError, RuntimeError) as error:
                        live[pid] = identity
                        status, query_error = 'unknown', str(error)
                    observations[pid] = {**identity, 'cancellation_status': status}
                recorded = live
                # 直接 Popen 句柄仍是归属证据；查询故障时也不能放弃明确 Stop 的回收。
                if direct_command is not None and direct_command.poll() is None:
                    try:
                        direct_command.send_signal(requested_signal)
                    except ProcessLookupError:
                        pass
                observation.update(owned_processes=list(recorded.values()),
                    cancellation_observations=list(observations.values()),
                    signal_requests=signal_requests,
                    ownership_observation_coverage_status='unknown' if coverage_unknown else 'complete')
                if query_error:
                    observation['error'] = query_error
                material = json.dumps(observation, sort_keys=True)
                if material != last_saved:
                    write_json(cleanup_file, observation)
                    last_saved = material
                if not recorded and (direct_command is None or direct_command.poll() is not None) and not query_error:
                    break
                if elapsed >= 3:
                    break
                time.sleep(0.05)
            observation.update(status='exited' if not recorded and not query_error and not coverage_unknown else 'unknown',
                observed_at=time.time(), retry_not_before=time.time() + 30, maximum_unchanged_retries=3)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            unchanged_failure = previous['status'] == 'unknown' and previous.get('error') == str(error)
            failed_attempt_count = previous.get('unchanged_recovery_attempt_count', 0) if unchanged_failure else 0
            if unchanged_failure and (failed_attempt_count >= 3 or time.time() < previous.get('retry_not_before', 0)):
                return previous
            observation.update(status='unknown', error=str(error), retry_not_before=time.time() + 30,
                maximum_unchanged_retries=3, unchanged_recovery_attempt_count=failed_attempt_count + 1)
        write_json(cleanup_file, observation)
        return observation


def reconcile_registered_role_owned_process_cleanup(run_directory: Path) -> None:
    registry = run_directory / 'registered-supervised-command-directories.json'
    for registered in read_json(registry).get('command_directories', []) if registry.exists() else []:
        directory = Path(registered).resolve()
        relative = directory.relative_to(run_directory.resolve())
        if not relative.parts or 'workspace' in relative.parts:
            raise ValueError('角色清理目录不能指向候选源码或 run 根目录')
        supervision_file = directory / 'command-supervision.json'
        if not supervision_file.exists() or read_json(supervision_file).get('command_process_lifetime_contract') != ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
            continue
        try:
            cleanup_role_owned_processes(directory)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            # 单飞锁或单项证据缺口不阻断其他已授权角色的资源恢复。
            continue
