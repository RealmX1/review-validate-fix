"""只观测已登记身份、自有进程组及其子链，不枚举全局进程。"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import sys

REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE = 'registered_process_identities_owned_groups_and_descendant_chains'
REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT = 'wait_for_background_work_or_register_separate_supervised_commands'
DARWIN_PROCESS_LIST_SELECTION_BY_PROCESS_GROUP_ID = 2
DARWIN_PROCESS_LIST_SELECTION_BY_PARENT_PROCESS_ID = 6


class BsdProcessInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "process_flags", "process_status", "exit_status", "process_id", "parent_process_id",
        "user_id", "group_id", "real_user_id", "real_group_id", "saved_user_id", "saved_group_id", "reserved_header")]
    _fields_ += [("command_name", ctypes.c_char * 16), ("process_name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "open_file_count", "process_group_id", "job_control_count", "terminal_device", "terminal_process_group_id", "nice_priority")]
    _fields_ += [("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


class BsdUniqueProcessInfo(ctypes.Structure):
    _fields_ = [('executable_uuid', ctypes.c_ubyte * 16), ('unique_process_id', ctypes.c_uint64),
        ('parent_unique_process_id', ctypes.c_uint64), ('process_id_version', ctypes.c_int32),
        ('original_parent_process_id_version', ctypes.c_int32), ('reserved_two', ctypes.c_uint64),
        ('reserved_three', ctypes.c_uint64)]


def native_process_identity(pid: int) -> dict | None:
    if sys.platform == 'darwin':
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        info = BsdProcessInfo()
        ctypes.set_errno(0)
        if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            observed_errno = ctypes.get_errno()
            if observed_errno in {errno.ESRCH, errno.ENOENT}:
                return None
            raise RuntimeError(f'原生进程启动身份不可用：pid={pid}, errno={observed_errno}')
        if info.process_status == 5:
            return None
        unique = BsdUniqueProcessInfo()
        ctypes.set_errno(0)
        if library.proc_pidinfo(pid, 17, 0, ctypes.byref(unique), ctypes.sizeof(unique)) != ctypes.sizeof(unique):
            observed_errno = ctypes.get_errno()
            if observed_errno in {errno.ESRCH, errno.ENOENT}:
                return None
            raise RuntimeError(f'原生进程唯一身份不可用：pid={pid}, errno={observed_errno}')
        return {'pid': pid, 'start_identity': [info.start_seconds, info.start_microseconds],
                'unique_process_id': unique.unique_process_id,
                'parent_pid': info.parent_process_id, 'process_group_id': info.process_group_id}
    if sys.platform.startswith('linux'):
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else {'pid': pid, 'start_identity': fields[19],
                'parent_pid': int(fields[1]), 'process_group_id': int(fields[2])}
        except (FileNotFoundError, ProcessLookupError):
            return None
    raise RuntimeError('当前平台没有可用的原生进程身份查询')


def terminate_observed_owned_processes(processes: dict[int, dict]) -> list[dict]:
    observations = []
    for pid, recorded in processes.items():
        try:
            current = native_process_identity(pid)
            if current is None:
                status = 'exited'
            elif current['start_identity'] != recorded['start_identity']:
                status = 'identity_changed_no_signal'
            elif pid == os.getpid():
                status = 'unknown'
            else:
                os.kill(pid, signal.SIGKILL)
                status = 'termination_requested'
        except ProcessLookupError:
            status = 'exited'
        except (OSError, RuntimeError):
            status = 'unknown'
        observations.append({**recorded, 'cancellation_status': status})
    return observations


def darwin_scoped_process_ids(selection: int, scope_id: int) -> list[int]:
    if selection not in {DARWIN_PROCESS_LIST_SELECTION_BY_PROCESS_GROUP_ID,
            DARWIN_PROCESS_LIST_SELECTION_BY_PARENT_PROCESS_ID} or scope_id <= 1:
        raise ValueError('进程查询必须限定到自有进程组或已核验的父 PID')
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    capacity = 256
    for _ in range(3):
        process_ids = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        observed_bytes = library.proc_listpids(selection, scope_id, process_ids, ctypes.sizeof(process_ids))
        observed_errno = ctypes.get_errno()
        if observed_bytes < 0 or observed_bytes == 0 and observed_errno or observed_bytes % ctypes.sizeof(ctypes.c_int):
            raise RuntimeError(f'限定范围的原生进程列表不可用：scope={scope_id}, errno={observed_errno}')
        if observed_bytes < ctypes.sizeof(process_ids):
            return [pid for pid in process_ids[:observed_bytes // ctypes.sizeof(ctypes.c_int)] if pid > 0]
        capacity *= 2
    raise RuntimeError(f'限定范围的原生进程列表不完整：scope={scope_id}')


def linux_owned_process_child_ids(pid: int) -> list[int]:
    # task 子链只用于发现；线程退出或空列表不能抹去已登记身份。
    children = set()
    try:
        for task in Path(f'/proc/{pid}/task').iterdir():
            if not task.name.isdigit():
                continue
            try:
                children.update(int(value) for value in (task / 'children').read_text().split())
            except (FileNotFoundError, ProcessLookupError):
                continue
    except (FileNotFoundError, ProcessLookupError):
        return []
    except OSError as error:
        raise RuntimeError(f'已登记进程的子链查询不可用：pid={pid}') from error
    return sorted(children)


def native_process_start_identity_matches(current: dict | None, recorded: dict) -> bool:
    return bool(current and current['pid'] == recorded['pid'] and current['start_identity'] == recorded['start_identity'])


def native_process_snapshot(supervisor_pid: int, previously_observed_owned_processes: dict[int, dict],
                            *, supervisor_native_identity: dict | None = None) -> dict[int, dict]:
    supervisor_native_identity = supervisor_native_identity or native_process_identity(supervisor_pid)
    if supervisor_native_identity is None or supervisor_native_identity.get('pid') != supervisor_pid:
        raise RuntimeError('监督者原生启动身份不可用')
    records = {}
    try:
        expected = {**previously_observed_owned_processes, supervisor_pid: supervisor_native_identity}
        supervisor_pid_reused = False
        for pid, recorded in expected.items():
            current = native_process_identity(pid)
            if native_process_start_identity_matches(current, recorded):
                records[pid] = current
            elif pid == supervisor_pid and current:
                supervisor_pid_reused = True
        pending = list(records)
        observed_groups = set()
        for pid in pending:
            parent = records[pid]
            if not native_process_start_identity_matches(native_process_identity(pid), parent):
                continue
            discoveries = []
            if sys.platform == 'darwin':
                group_id = parent['process_group_id']
                if (group_id == pid or group_id == supervisor_pid and not supervisor_pid_reused) and group_id not in observed_groups:
                    discoveries.append(('group', group_id, darwin_scoped_process_ids(
                        DARWIN_PROCESS_LIST_SELECTION_BY_PROCESS_GROUP_ID, group_id)))
                    observed_groups.add(group_id)
                discoveries.append(('parent', pid, darwin_scoped_process_ids(
                    DARWIN_PROCESS_LIST_SELECTION_BY_PARENT_PROCESS_ID, pid)))
            elif sys.platform.startswith('linux'):
                discoveries.append(('parent', pid, linux_owned_process_child_ids(pid)))
            else:
                raise RuntimeError('当前平台没有可用的原生后代归属查询')
            if not native_process_start_identity_matches(native_process_identity(pid), parent):
                raise RuntimeError(f'发现子链期间所属父身份变化，需重新采集：pid={pid}')
            for relationship, scope_id, candidates in discoveries:
                for candidate_pid in candidates:
                    if candidate_pid in records or candidate_pid == supervisor_pid:
                        continue
                    current = native_process_identity(candidate_pid)
                    if current is None or candidate_pid in expected and not native_process_start_identity_matches(current, expected[candidate_pid]):
                        continue
                    if current['process_group_id' if relationship == 'group' else 'parent_pid'] == scope_id:
                        records[candidate_pid] = current
                        pending.append(candidate_pid)
        return records
    except (OSError, RuntimeError) as error:
        error.observed_owned_processes = {pid: item for pid, item in records.items() if pid != supervisor_pid}
        raise


def owned_process_snapshot(supervisor_pid: int, previously_observed_owned_processes: dict[int, dict],
                           *, supervisor_native_identity: dict | None = None) -> dict[int, dict]:
    return {pid: item for pid, item in native_process_snapshot(supervisor_pid, previously_observed_owned_processes,
        supervisor_native_identity=supervisor_native_identity).items() if pid != supervisor_pid}
