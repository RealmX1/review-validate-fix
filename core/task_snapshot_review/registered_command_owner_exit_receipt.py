"""核验同一所属执行的封闭原生退出回执，不改写历史观测事实。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from .native_owned_process_observation import recorded_native_process_identity_is_valid
from .persistent_artifacts import read_json


def _material_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _normalized_boot_identity(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('所有者回执缺少系统启动世代')
    normalized = value.split(':', 1)[-1]
    return str(uuid.UUID(normalized))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_inner_identity(progress: dict, facts: dict, process_kind: str) -> None:
    pid = facts.get(process_kind + '_pid')
    if pid is None:
        _require(process_kind == 'command' and facts.get('command_execution_started') is not True
            and facts.get('command_returncode') is None,
            f'所有者回执缺少 RVF {process_kind} 的派发 PID')
        return
    identity = facts.get(process_kind + '_native_identity')
    _require(recorded_native_process_identity_is_valid(identity) and identity['pid'] == pid,
        f'所有者回执缺少 RVF {process_kind} 的完整原生身份')
    _require(isinstance(identity['start_identity'], str),
        'Linux 内核退出回执只接受启动 tick 原生身份')
    generation = 'linux:' + identity['start_identity']
    matches = [item for item in progress['ownedProcesses']
        if item.get('pid') == pid and item.get('nativeProcessGenerationIdentity') == generation]
    _require(len(matches) == 1, f'所有者回执未唯一绑定 RVF {process_kind}')


def observe_registered_command_owner_exit_receipt(run_directory: Path, facts: dict) -> dict:
    material = {'run_directory': str(run_directory)}
    try:
        run_directory = run_directory.resolve()
        record = read_json(run_directory / 'run.json')
        execution_identity = record.get('command_execution_identity') or {}
        execution_id = execution_identity.get('execution_id')
        revocation_file = record.get('command_execution_revocation_file')
        _require(isinstance(execution_id, str) and bool(execution_id), 'run 未登记所属执行身份')
        _require(isinstance(revocation_file, str) and bool(revocation_file), 'run 未登记所属执行撤销原件')
        _require(facts.get('run_directory') == str(run_directory), '命令监督记录与 run 不匹配')
        command_directory = facts.get('command_directory')
        _require(isinstance(command_directory, str) and bool(command_directory), '命令监督记录缺少控制目录')
        command_directory = Path(command_directory).resolve()
        _require(command_directory.is_relative_to(run_directory)
            and 'workspace' not in command_directory.relative_to(run_directory).parts,
            '命令控制目录未绑定该 run')

        start_receipt_file = run_directory / 'kanban-command-execution-start-receipt.json'
        start_receipt = read_json(start_receipt_file)
        _require(start_receipt.get('returncode') == 0, '所属执行启动回执不可用')
        start_payload = json.loads(start_receipt['stdout'])
        _require(start_payload.get('ok') is True, '所属执行启动未获确认')
        execution = start_payload['execution']
        launch_nonce = execution.get('launchNonce')
        _require(execution.get('executionId') == execution_id, '所有者回执 executionId 不匹配')
        _require(execution.get('revocationFile') == revocation_file, '所有者回执撤销路径不匹配')
        _require(execution.get('launchAttemptCount') == 1 and execution.get('previousLaunchNonces') == [],
            '所有者回执不是 run 首次登记的唯一派发世代')
        _require(isinstance(launch_nonce, str) and bool(launch_nonce), '所有者回执缺少 launchNonce')
        task_binding = read_json(run_directory / 'kanban-command-execution-binding.json')
        _require(isinstance(task_binding.get('task_id'), str) and bool(task_binding['task_id'])
            and execution.get('taskId') == task_binding['task_id'], '所有者回执任务身份不匹配')

        owner_directory = Path(revocation_file).resolve().parent
        _require(not owner_directory.is_relative_to(run_directory)
            and not owner_directory.is_relative_to(Path(record['repo']).resolve()),
            '不能从 run 或候选源码内接受所有者退出回执')
        paths = {kind: owner_directory / f'{kind}-{launch_nonce}.json' for kind in (
            'launch-materials', 'supervisor-identity', 'kernel-child-subreaper-containment', 'supervisor-progress')}
        launch = read_json(paths['launch-materials'])
        owner_identity = read_json(paths['supervisor-identity'])
        containment = read_json(paths['kernel-child-subreaper-containment'])
        progress = read_json(paths['supervisor-progress'])
        material = {
            'run_directory': str(run_directory),
            'execution_id': execution_id,
            'launch_nonce': launch_nonce,
            'launch': launch,
            'owner_identity': owner_identity,
            'containment': containment,
            'progress': {key: progress.get(key) for key in (
                'launchNonce', 'status', 'returncode', 'signal', 'kernelChildExitObservation',
                'linuxKernelContainmentVerified', 'linuxKernelContainmentContinuityLost',
                'nativeOwnershipUnproven', 'unprovenOwnershipProcesses', 'ownedProcesses')},
        }

        _require(launch.get('directory') == str(owner_directory), '所有者回执控制目录不匹配')
        _require(launch.get('executionId') == execution_id and launch.get('launchNonce') == launch_nonce,
            '所有者 launch 世代不匹配')
        _require(launch.get('revocationFile') == revocation_file, '所有者 launch 撤销路径不匹配')
        _require(launch.get('workingDirectory') == str(Path(record['repo']).resolve()),
            '所有者 launch 工作区不匹配')
        command = launch.get('command')
        _require(isinstance(command, list) and command.count('--run-dir') == 1
            and command[-3:] == ['resume-task-review', '--run-dir', str(run_directory)],
            '所有者 launch 未精确绑定该 run 的恢复命令')
        boot = _normalized_boot_identity(facts.get('host_boot_identity'))
        _require(facts['host_boot_identity'].startswith('linux:'), 'Linux 封闭回执与原命令平台不匹配')
        _require(_normalized_boot_identity(launch.get('systemBootIdentity')) == boot
            and _normalized_boot_identity(containment.get('systemBootIdentity')) == boot,
            '所有者回执 boot 世代不匹配')
        _require(launch.get('superviseProcessGroup') is True
            and launch.get('linuxKernelChildSubreaperContainmentRequired') is True,
            '所有者派发前未启用完整原生进程组与 Linux 内核子进程封闭')
        _require(containment.get('launchNonce') == launch_nonce
            and containment.get('mode') == 'linux_child_subreaper_established_before_script_dispatch'
            and isinstance(containment.get('pidNamespaceIdentity'), str)
            and bool(containment['pidNamespaceIdentity']),
            '缺少派发前建立的 Linux 内核子进程封闭原件')
        _require(owner_identity.get('launchNonce') == launch_nonce
            and type(owner_identity.get('pid')) is int and 0 < owner_identity['pid'] <= 2**31 - 1
            and owner_identity.get('pid') == containment.get('pid')
            and isinstance(owner_identity.get('nativeProcessGenerationIdentity'), str)
            and owner_identity['nativeProcessGenerationIdentity'].startswith('linux:')
            and owner_identity['nativeProcessGenerationIdentity'][6:].isascii()
            and owner_identity['nativeProcessGenerationIdentity'][6:].isdigit()
            and owner_identity.get('nativeProcessGenerationIdentity')
                == containment.get('nativeProcessGenerationIdentity'),
            '所有者监督者完整原生身份不匹配')
        _require(progress.get('launchNonce') == launch_nonce and progress.get('status') == 'exited'
            and (isinstance(progress.get('returncode'), int) or isinstance(progress.get('signal'), str)),
            '所有者根命令尚未取得终态')
        _require(progress.get('linuxKernelContainmentVerified') is True
            and progress.get('linuxKernelContainmentContinuityLost') is False
            and type(progress.get('kernelChildExitObservation')) is int
            and progress['kernelChildExitObservation'] == 0
            and progress.get('nativeOwnershipUnproven') is False
            and not progress.get('unprovenOwnershipProcesses'),
            '所有者终态没有证明内核封闭范围内所有子进程退出')
        _require(isinstance(progress.get('ownedProcesses'), list)
            and all(isinstance(item, dict) for item in progress['ownedProcesses']),
            '所有者回执缺少有效的所属进程历史')
        _require_inner_identity(progress, facts, 'supervisor')
        _require_inner_identity(progress, facts, 'command')
        return {'status': 'exited', 'source': 'linux_kernel_child_subreaper_exit_receipt',
            'execution_id': execution_id, 'launch_nonce': launch_nonce,
            'evidence_files': {kind: str(path) for kind, path in paths.items()},
            'evidence_sha256': _material_sha256(material)}
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        return {'status': 'unknown', 'source': 'registered_owner_exit_receipt',
            'reason': str(error), 'evidence_sha256': _material_sha256({**material, 'rejection': str(error)})}
