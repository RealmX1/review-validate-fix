"""使用原生进程身份和继承的执行归属识别跨 session 后代，不依赖 ps。"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import sys

REGISTERED_COMMAND_OWNERSHIP_ENVIRONMENT_KEY = "RVF_REGISTERED_COMMAND_EXECUTION_OWNERSHIP"
DARWIN_PROCESS_LIST_SELECTION_BY_USER_ID = 4


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


def darwin_process_was_started_by_launchd(library, pid: int) -> bool:
    process, launchd = BsdUniqueProcessInfo(), BsdUniqueProcessInfo()
    # 原始父进程版本在 reparent 后保持不变；仅排除可证明确由 launchd 新启动的服务。
    if (library.proc_pidinfo(pid, 17, 0, ctypes.byref(process), ctypes.sizeof(process)) != ctypes.sizeof(process)
            or library.proc_pidinfo(1, 17, 0, ctypes.byref(launchd), ctypes.sizeof(launchd)) != ctypes.sizeof(launchd)):
        return False
    return process.original_parent_process_id_version == launchd.process_id_version


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
        return {'pid': pid, 'start_identity': [info.start_seconds, info.start_microseconds]}
    if sys.platform.startswith('linux'):
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else {'pid': pid, 'start_identity': fields[19]}
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


def native_process_snapshot(execution_token: str, supervisor_pid: int, previously_observed_owned_processes: dict[int, dict],
                            *, supervisor_native_identity: dict | None = None) -> dict[int, dict]:
    token = execution_token.encode()
    prefix = REGISTERED_COMMAND_OWNERSHIP_ENVIRONMENT_KEY.encode() + b"="

    def belongs(environment: bytes) -> bool:
        return any(token in entry[len(prefix):].split(b":") for entry in environment.split(b"\0") if entry.startswith(prefix))

    records = {}
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        if supervisor_native_identity is None:
            supervisor_native_identity = native_process_identity(supervisor_pid)
        if supervisor_native_identity is None or supervisor_native_identity.get('pid') != supervisor_pid:
            raise RuntimeError("监督者原生启动身份不可用")
        supervisor_start_identity = tuple(supervisor_native_identity['start_identity'])
        system = ctypes.CDLL(None, use_errno=True)
        # 先由内核限定用户，避免把其他用户的权限拒绝误判为所属后代缺证。
        capacity = library.proc_listpids(DARWIN_PROCESS_LIST_SELECTION_BY_USER_ID, os.getuid(), None, 0)
        if capacity <= 0:
            raise RuntimeError("原生进程列表不可用")
        process_ids = (ctypes.c_int * (capacity // ctypes.sizeof(ctypes.c_int) * 2 + 128))()
        observed_bytes = library.proc_listpids(DARWIN_PROCESS_LIST_SELECTION_BY_USER_ID, os.getuid(), process_ids, ctypes.sizeof(process_ids))
        if observed_bytes <= 0 or observed_bytes >= ctypes.sizeof(process_ids) or observed_bytes % ctypes.sizeof(ctypes.c_int):
            raise RuntimeError("原生进程列表不完整")
        count = observed_bytes // ctypes.sizeof(ctypes.c_int)
        process_information = {}
        for pid in process_ids[:count]:
            info = BsdProcessInfo()
            ctypes.set_errno(0)
            if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                observed_errno = ctypes.get_errno()
                if observed_errno not in {errno.ESRCH, errno.ENOENT}:
                    raise RuntimeError(f'原生进程退出未知：pid={pid}, errno={observed_errno}')
                continue
            if info.user_id != os.getuid() or info.process_status == 5:
                continue
            process_information[pid] = info
        supervisor = process_information.get(supervisor_pid)
        supervisor_identity_matches = bool(supervisor and
            (supervisor.start_seconds, supervisor.start_microseconds) == supervisor_start_identity)
        proven_owned_process_ids = {supervisor_pid} if supervisor_identity_matches else set()
        def extend_owned_process_ids_by_parent_chain():
            while True:
                additions = {pid for pid, info in process_information.items() if pid not in proven_owned_process_ids and (
                    supervisor_identity_matches and info.process_group_id == supervisor_pid or info.parent_process_id in proven_owned_process_ids
                    or pid in previously_observed_owned_processes and [info.start_seconds, info.start_microseconds] == previously_observed_owned_processes[pid]['start_identity'])}
                if not additions:
                    return
                proven_owned_process_ids.update(additions)
        def native_identity_record(pid, info):
            return {"pid": pid, "parent_pid": info.parent_process_id, "process_group_id": info.process_group_id,
                    "start_identity": [info.start_seconds, info.start_microseconds], "execution_token_matches": False}
        extend_owned_process_ids_by_parent_chain()
        for pid, info in process_information.items():
            record = native_identity_record(pid, info)
            if pid in proven_owned_process_ids:
                records[pid] = record
                continue
            # 已知父链不属本次执行的进程无需读取环境；孤儿或缺失父链才依赖继承归属补证。
            if ((info.start_seconds, info.start_microseconds) < supervisor_start_identity
                    or info.parent_process_id > 1 and info.parent_process_id in process_information):
                continue
            query = (ctypes.c_int * 3)(1, 49, pid)
            arguments = ctypes.create_string_buffer(262144)
            length = ctypes.c_size_t(len(arguments))
            if system.sysctl(query, 3, arguments, ctypes.byref(length), None, 0):
                observed_errno = ctypes.get_errno()
                # 父进程退出也会使旧表中的子进程跳过查询；必须重采完整父链。
                raise RuntimeError(f"原生执行归属查询不可用：pid={pid}, errno={observed_errno}")
            record['execution_token_matches'] = belongs(arguments.raw[:length.value])
            # 清空环境的脱离 session 后代与无关新孤儿无法区分；缺少 token 不是退出证明。
            record['execution_ownership_unknown'] = (not record['execution_token_matches']
                and not darwin_process_was_started_by_launchd(library, pid))
            records[pid] = record
            if record['execution_token_matches']:
                proven_owned_process_ids.add(pid)
        extend_owned_process_ids_by_parent_chain()
        for pid in proven_owned_process_ids:
            if pid in process_information and pid not in records:
                records[pid] = native_identity_record(pid, process_information[pid])
        return records
    if sys.platform.startswith("linux"):
        process_directories = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z":
                    continue
                pid = int(entry.name)
                records[pid] = {"pid": pid, "parent_pid": int(fields[1]), "process_group_id": int(fields[2]),
                    "start_identity": fields[19], "execution_token_matches": False}
                process_directories[pid] = entry
            except (FileNotFoundError, ProcessLookupError):
                continue
        supervisor_native_identity = supervisor_native_identity or native_process_identity(supervisor_pid)
        if supervisor_native_identity is None or supervisor_native_identity.get('pid') != supervisor_pid:
            raise RuntimeError("监督者原生启动身份不可用")
        supervisor_start_identity = supervisor_native_identity['start_identity']
        supervisor_identity_matches = records.get(supervisor_pid, {}).get('start_identity') == supervisor_start_identity
        proven_owned_process_ids = {supervisor_pid} if supervisor_identity_matches else set()
        while True:
            additions = {pid for pid, item in records.items() if pid not in proven_owned_process_ids and (
                supervisor_identity_matches and item['process_group_id'] == supervisor_pid
                or item['parent_pid'] in proven_owned_process_ids
                or pid in previously_observed_owned_processes and item['start_identity'] == previously_observed_owned_processes[pid]['start_identity'])}
            if not additions:
                break
            proven_owned_process_ids.update(additions)
        for pid, item in records.items():
            if (pid in proven_owned_process_ids or int(item['start_identity']) < int(supervisor_start_identity)
                    or item['parent_pid'] > 1 and item['parent_pid'] in records):
                continue
            try:
                item['execution_token_matches'] = belongs((process_directories[pid] / 'environ').read_bytes())
                item['execution_ownership_unknown'] = not item['execution_token_matches']
            except (FileNotFoundError, ProcessLookupError) as error:
                raise RuntimeError(f'原生进程父链已变化，需重新采集：pid={pid}') from error
            except PermissionError as error:
                raise RuntimeError(f'必要的原生执行归属查询不可用：pid={pid}') from error
        return records
    raise RuntimeError("当前平台没有可用的原生后代归属查询")


def owned_process_snapshot(execution_token: str, supervisor_pid: int, previously_observed_owned_processes: dict[int, dict],
                           *, supervisor_native_identity: dict | None = None) -> dict[int, dict]:
    snapshot = native_process_snapshot(execution_token, supervisor_pid, previously_observed_owned_processes,
        supervisor_native_identity=supervisor_native_identity)
    supervisor_identity_matches = (supervisor_native_identity is None or
        snapshot.get(supervisor_pid, {}).get('start_identity') == supervisor_native_identity['start_identity'])
    owned = {pid: item for pid, item in snapshot.items() if pid != supervisor_pid and (
        item["execution_token_matches"] or supervisor_identity_matches and item["process_group_id"] == supervisor_pid
        or pid in previously_observed_owned_processes and item["start_identity"] == previously_observed_owned_processes[pid]["start_identity"])}
    while True:
        added = {pid: item for pid, item in snapshot.items() if pid != supervisor_pid and pid not in owned
                 and item["parent_pid"] in ({supervisor_pid, *owned} if supervisor_identity_matches else owned)}
        if not added:
            unproven = [item for pid, item in snapshot.items() if pid not in owned and pid != supervisor_pid
                        and item.get('execution_ownership_unknown')]
            if unproven:
                error = RuntimeError(f'新孤儿进程归属未知，不能证明所属后代退出：{unproven}')
                error.observed_owned_processes = owned
                raise error
            return owned
        owned.update(added)
