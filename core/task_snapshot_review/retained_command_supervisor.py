"""独立监督所属命令及跨 session 后代，保留进程归属与持久撤销。"""
from __future__ import annotations

import json
import hashlib
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
    ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT, native_host_boot_identity,
    native_process_identity, native_process_start_identity_matches, owned_process_snapshot, terminate_observed_owned_processes,
    recorded_native_process_identity_is_valid,
)
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json


def terminate_supervised_command_descendants(command_directory: Path) -> list[dict]:
    facts = read_json(command_directory / 'command-supervision.json')
    if facts.get('command_process_lifetime_contract') == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
        from core.task_snapshot_review.role_owned_process_cleanup import cleanup_role_owned_processes
        cleanup = cleanup_role_owned_processes(command_directory, cancellation_requested=True)
        observations = cleanup.get('cancellation_observations', [])
        supervisor = facts.get('supervisor_native_identity')
        if (cleanup['status'] == 'pending' and supervisor and facts.get('host_boot_identity') == native_host_boot_identity()
                and native_process_start_identity_matches(native_process_identity(supervisor['pid']), supervisor)):
            try:
                os.kill(supervisor['pid'], signal.SIGTERM)
                observations = [*observations, {**supervisor, 'cancellation_status': 'termination_requested'}]
            except ProcessLookupError:
                pass
        if cleanup['status'] not in {'pending', 'exited'}:
            observations = [*observations, {'cancellation_status': 'unknown', 'error': cleanup.get('error', '角色资源退出尚未证明')}]
        return observations
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
                if not recorded_native_process_identity_is_valid(item):
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


def supervise_registered_command(run_directory: Path, command_directory: Path, inherited_descriptors: tuple[int, ...], command: list[str],
                                 *, command_process_lifetime_contract: str = REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT) -> int:
    process_group_id = os.getpgrp()
    if process_group_id != os.getpid():
        raise ValueError('独立监督者必须拥有自己的进程组')
    if command_process_lifetime_contract not in {REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT}:
        raise ValueError('未知的命令资源生命周期契约')
    execution_token = os.urandom(24).hex()
    supervision_file = command_directory / 'command-supervision.json'
    facts = {'status': 'starting', 'supervisor_pid': os.getpid(), 'process_group_id': process_group_id,
             'run_directory': str(run_directory.resolve()), 'command_directory': str(command_directory.resolve()),
             'argv': command, 'execution_token': execution_token,
             'process_ownership_scope': REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE,
             'command_process_lifetime_contract': command_process_lifetime_contract,
             'ownership_observation_coverage_status': 'complete', 'owned_process_exit_status': 'pending'}
    write_json(supervision_file, facts)
    child = None
    owned = {}
    next_ownership_observation = 0
    ownership_observation_failures = 0
    last_checkpoint_material = None
    next_checkpoint = 0
    termination_requested = False

    def observe_owned_processes():
        for attempt in range(3):
            try:
                if child is not None and child.poll() is None:
                    direct_identity = facts.get('command_native_identity') or native_process_identity(child.pid)
                    if direct_identity is not None:
                        owned[child.pid] = direct_identity
                        facts['command_native_identity'] = direct_identity
                return owned_process_snapshot(os.getpid(), owned,
                    supervisor_native_identity=facts['supervisor_native_identity'])
            except (OSError, RuntimeError) as error:
                owned.update(getattr(error, 'observed_owned_processes', {}))
                facts['owned_processes'] = list(owned.values())
                if child is not None:
                    facts['ownership_observation_coverage_status'] = 'unknown'
                    facts.setdefault('ownership_observation_gap_started_at', time.time())
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def request_termination(*_):
        # ExecutionRevoked 也是 OSError；异步抛出会被查询或 Popen 的错误处理吞掉。
        nonlocal termination_requested
        termination_requested = True

    def ensure_command_not_cancelled():
        if termination_requested:
            raise ExecutionRevoked('监督者收到取消信号')
        ensure_execution_not_revoked(run_directory)

    def terminate_owned_processes():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if command_process_lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
            from core.task_snapshot_review.role_owned_process_cleanup import cleanup_role_owned_processes
            facts['owned_processes'] = list(owned.values())
            write_json(supervision_file, facts)
            cleanup = cleanup_role_owned_processes(command_directory, direct_command=child)
            facts.update(owned_process_exit_status=cleanup['status'], role_owned_process_cleanup_status=cleanup['status'])
            write_json(supervision_file, facts)
            return
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
        ensure_command_not_cancelled()
        # 启动前确认等价原生查询可用，查询失败保存恢复条件。
        facts['supervisor_native_identity'] = native_process_identity(os.getpid())
        facts['host_boot_identity'] = native_host_boot_identity()
        write_json(supervision_file, facts)
        observe_owned_processes()
        with exclusive_lock(run_directory / 'execution-revocation.lock'):
            ensure_command_not_cancelled()
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
            ensure_command_not_cancelled()
            returncode = child.poll()
            if time.monotonic() >= next_ownership_observation or returncode is not None:
                try:
                    owned = observe_owned_processes()
                    ownership_observation_failures = 0
                    facts['ownership_observation_status'] = 'available'
                except (OSError, RuntimeError) as error:
                    if child.poll() is not None:
                        raise
                    ownership_observation_failures = min(4, ownership_observation_failures + 1)
                    facts['ownership_observation_status'] = 'unknown'
                    facts['last_ownership_observation_error'] = str(error)
                next_ownership_observation = time.monotonic() + (min(5, 2 ** (ownership_observation_failures - 1)) if ownership_observation_failures else 0.05)
                if ownership_observation_failures:
                    facts['ownership_observation_retry_not_before'] = time.time() + min(5, 2 ** (ownership_observation_failures - 1))
                else:
                    facts.pop('ownership_observation_retry_not_before', None)
            ensure_command_not_cancelled()
            facts.update({'command_returncode': returncode, 'owned_processes': list(owned.values())})
            material = json.dumps(facts, sort_keys=True)
            if material != last_checkpoint_material or time.monotonic() >= next_checkpoint:
                facts['last_observed_at'] = time.time()
                write_json(supervision_file, facts)
                last_checkpoint_material = json.dumps(facts, sort_keys=True)
                next_checkpoint = time.monotonic() + 1
            if returncode is not None and command_process_lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                from core.task_snapshot_review.role_owned_process_cleanup import cleanup_role_owned_processes
                cleanup = cleanup_role_owned_processes(command_directory, direct_command=child)
                facts.update(status='completed' if cleanup['status'] == 'exited' else 'descendant_exit_unproven',
                    returncode=returncode, owned_process_exit_status=cleanup['status'], role_owned_process_cleanup_status=cleanup['status'])
                write_json(supervision_file, facts)
                return returncode
            if returncode is not None and not owned:
                if facts['ownership_observation_coverage_status'] != 'complete':
                    raise RuntimeError('原生进程观测曾中断；空快照不能证明缺口期间的后代均已退出')
                execution_file = command_directory / "execution.json"
                execution = read_json(execution_file) if execution_file.exists() else {}
                if execution.get("source_tree_before"):
                    from core.task_snapshot_review.task_snapshot import capture_worktree_tree
                    facts["source_tree_after"] = capture_worktree_tree(Path.cwd(), ["."], command_directory,
                        excluded_preparation_paths=execution.get("excluded_preparation_paths"))
                facts.update({stream + "_sha256": hashlib.sha256((command_directory / (stream + ".txt")).read_bytes()).hexdigest()
                    for stream in ("stdout", "stderr") if (command_directory / (stream + ".txt")).exists()})
                facts.update({'status': 'completed', 'returncode': returncode, 'owned_process_exit_status': 'exited'})
                write_json(supervision_file, facts)
                return returncode
            # EXPERIMENT_NODE: 用短寿命跨 session 链路量测 50 毫秒观测间隔的发现率与 CPU 权衡。
            time.sleep(0.05)
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
            if command_process_lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT and child.poll() is not None:
                # 资源缺证独立于 CLI 业务结果，不能因此重复调用模型。
                return child.returncode
        return 125


if __name__ == '__main__':
    lifetime_contract = sys.argv[5] if len(sys.argv) > 5 else REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT
    if lifetime_contract == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
        invocation_file = Path(sys.argv[4]).resolve()
        if invocation_file != (Path(sys.argv[2]) / 'model-invocation.json').resolve():
            raise ValueError('角色命令必须读取同一次尝试的已登记派发原件')
        command = read_json(invocation_file)['command']
    else:
        command = json.loads(sys.argv[4])
    sys.exit(supervise_registered_command(Path(sys.argv[1]), Path(sys.argv[2]),
        tuple(json.loads(sys.argv[3])), command, command_process_lifetime_contract=lifetime_contract))
