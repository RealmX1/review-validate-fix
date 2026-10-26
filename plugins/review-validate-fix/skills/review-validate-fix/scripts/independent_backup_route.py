#!/usr/bin/env python3
"""独立备用路线的协调原语；仅依赖标准库与 Git，不签发 RVF 资格或成功回执。

正常写入先登记 reservation 再检查接管目录。备用方先原子创建接管目录，
再等待已有 reservation 结束；这两个顺序共同关闭 check-then-write 竞态。
目录存在即阻止正常写入，连损坏或手工创建的记录也不能静默放行。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from collections.abc import Mapping
import ctypes
import errno
import hashlib
import functools
import inspect
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import uuid

CONTROL_DIRECTORY_NAME = "rvf-independent-backup-route"
STAGES = ("review", "fix_validate", "land", "base_sync")
CURRENT_NORMAL_MUTATION_RESERVATIONS = ContextVar("current_normal_mutation_reservations", default=())
NORMAL_MUTATION_RESERVATION_UPDATE_LOCK = threading.Lock()
STOP_COMPLETION_RECOVERY_OPERATION_LIMIT = 32


class BackupRouteOwned(RuntimeError):
    """当前 worktree 已接管或仍有必须对账的写入。"""


class NormalMutationCommandFailed(RuntimeError):
    def __init__(self, returncode: int):
        super().__init__(f"正常命令退出 {returncode}；保留 reservation 与实际 Git 状态供对账")
        self.returncode = returncode


def repository_git_environment() -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items()
               if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}},
            "GIT_OPTIONAL_LOCKS": "0"}


def validate_operation_id(operation_id: str) -> None:
    if not operation_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", operation_id):
        raise ValueError("operation ID 只能包含字母、数字、下划线、点与短横线")


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *arguments], text=True, capture_output=True,
                               env=repository_git_environment())
    if completed.returncode:
        raise ValueError(f"Git 核验失败：{' '.join(arguments[:3])}：{completed.stderr.strip()}")
    return completed.stdout.strip()


def guard_normal_mutation(repository_argument: str, operation: str, run_argument: str | None = None):
    """让公开写入函数与备用接管共享 reservation 协议。"""
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            if callable(repository_argument):
                repo = repository_argument(bound.arguments)
            else:
                repository_keys = repository_argument.split(".")
                repo = bound.arguments.get(repository_keys[0])
                for key in repository_keys[1:]:
                    repo = repo.get(key) if isinstance(repo, Mapping) else getattr(repo, key, None)
            run = bound.arguments.get(run_argument) if run_argument else None
            if repo is None:
                return function(*args, **kwargs)
            with normal_worktree_mutation(Path(repo), operation, Path(run) if run else None):
                return function(*args, **kwargs)
        return guarded
    return decorate


def control_directory(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve() / CONTROL_DIRECTORY_NAME


def resolve_worktree_root(repo: Path) -> Path:
    """仓内子目录与符号链接入口共享真实 worktree 根，独立 worktree 仍各自隔离。"""
    return Path(git(repo, "rev-parse", "--show-toplevel")).resolve()


def repository_for_normal_metadata_mutation(repo: Path | str | None) -> Path | None:
    """纯元数据 API 可无 Git；已存在的 Git 归属损坏不能被当作无仓库。"""
    if repo is None:
        return None
    path = Path(repo).resolve()
    try:
        control_directory(path)
    except ValueError:
        if any(marker_present(parent / ".git") for parent in (path, *path.parents)):
            raise
        return None
    return resolve_worktree_root(path)


def marker_present(path: Path) -> bool:
    return os.path.lexists(path)


def optional_head(repo: Path) -> str | None:
    completed = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, env=repository_git_environment())
    return completed.stdout.strip() if completed.returncode == 0 else None


def current_boot_session_id() -> str:
    """读取内核启动代际，不把时长或墙钟近似值当成重启身份。"""
    if sys.platform.startswith("linux"):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip().lower()
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        size = ctypes.c_size_t(256)
        value = ctypes.create_string_buffer(size.value)
        if library.sysctlbyname(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), "无法读取内核启动代际")
        return value.value.decode().lower()
    raise OSError(errno.ENOSYS, "当前平台缺少原生进程代际读取器")


def read_process_identity(pid: int) -> dict:
    """PID、启动代际和内核开始时间共同绑定真实进程，权限失败保留未知。"""
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("进程 PID 必须为正整数")
    boot = current_boot_session_id()
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        except FileNotFoundError as exc:
            raise ProcessLookupError(errno.ESRCH, "进程已退出", pid) from exc
        return {"pid": pid, "boot_session_id": boot, "process_start_id": "linux-proc-start:" + fields[19],
                "process_group_id": int(fields[2]), "parent_process_id": int(fields[1]), "process_state": fields[0]}
    if sys.platform == "darwin":
        # Darwin SDK 的 proc_bsdinfo 为 136 字节，末尾两个 uint64 是开始时间的秒与微秒。
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        value = ctypes.create_string_buffer(136)
        if library.proc_pidinfo(pid, 3, 0, value, len(value)) != len(value):
            error = ctypes.get_errno()
            os.kill(pid, 0)
            raise OSError(error or errno.EIO, "无法读取原生进程开始时间")
        seconds, microseconds = struct.unpack_from("=QQ", value.raw, 120)
        status, parent = struct.unpack_from("=I", value.raw, 4)[0], struct.unpack_from("=I", value.raw, 16)[0]
        return {"pid": pid, "boot_session_id": boot,
                "process_start_id": f"darwin-proc-start:{seconds}:{microseconds}",
                "process_group_id": struct.unpack_from("=I", value.raw, 100)[0],
                "parent_process_id": parent, "process_state": "Z" if status == 5 else "running"}
    raise OSError(errno.ENOSYS, "当前平台缺少原生进程代际读取器")


def capture_process_identity(pid: int) -> dict:
    try:
        return read_process_identity(pid)
    except OSError as exc:
        # 采集受限不阻断正常写入；之后的接管须补齐身份或使用内核退出事实。
        identity = {"pid": pid, "identity_read_error": str(exc)}
        try:
            identity["boot_session_id"] = current_boot_session_id()
        except OSError:
            pass
        return identity


def original_process_is_running(pid: int, identity: dict | None) -> bool:
    if identity and identity.get("boot_session_id"):
        try:
            if identity["boot_session_id"] != current_boot_session_id():
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise BackupRouteOwned("原进程状态未知；权限错误不能替代退出证据") from exc
    if identity and identity.get("process_start_id"):
        try:
            current = read_process_identity(pid)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise BackupRouteOwned("进程代际暂不可读；补证后自动重验，不按 PID 偷取") from exc
        if identity.get("boot_session_id") == current["boot_session_id"] and identity["process_start_id"] != current["process_start_id"]:
            return False
    return True


def durable_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writing = path.with_name(f".{path.name}.{uuid.uuid4().hex}.writing")
    try:
        with writing.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(writing, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        writing.unlink(missing_ok=True)


def evidence_copy(path: Path, destination: Path) -> dict:
    source = path.resolve(strict=True)
    if not source.is_file() or not source.stat().st_size:
        raise ValueError("证据必须是非空普通文件")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{digest}{source.suffix}"
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    elif target.read_bytes() != content:
        raise ValueError("内容寻址证据已改变；保留原件并补证，不能覆盖")
    return {"path": str(target), "sha256": digest, "source_path": str(source)}


def operation_identity(repo: Path, operation_id: str) -> dict:
    validate_operation_id(operation_id)
    marker = control_directory(repo) / "active-owner"
    try:
        identity = json.loads((marker / "identity.json").read_text())
    except (OSError, ValueError) as exc:
        raise BackupRouteOwned("接管记录不可读；保留现场并从持久记录补证") from exc
    if identity.get("operation_id") != operation_id or identity.get("worktree_git_directory") != git(repo, "rev-parse", "--absolute-git-dir"):
        raise BackupRouteOwned("接管身份不匹配；不得换 operation ID 重试")
    return identity


def consumed_run_record(repo: Path, run_dir: Path | None) -> Path | None:
    if run_dir is None:
        return None
    key = hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()
    return control_directory(repo) / "consumed-original-runs" / f"{key}.json"


def source_task_worktrees(repo: Path, tip: str, explicit_worktree: Path | None) -> list[Path]:
    """来源身份由 worktree 的 Git 目录固定，提交仅用于核验调用候选。"""
    canonical_tip = git(repo, "rev-parse", "--verify", tip + "^{commit}")
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if explicit_worktree is not None:
        if git(explicit_worktree, "rev-parse", "--path-format=absolute", "--git-common-dir") != common:
            raise BackupRouteOwned("来源 worktree 不属于同一 Git 仓库；先核对归属")
        if optional_head(explicit_worktree) != canonical_tip:
            raise BackupRouteOwned("来源 worktree 候选已变化；先核对当前提交再落地")
        return [explicit_worktree]
    candidates = []
    current = None
    for field in git(repo, "worktree", "list", "--porcelain", "-z").split("\0"):
        if field.startswith("worktree "):
            current = Path(field[9:])
        elif field == "HEAD " + canonical_tip and current is not None:
            candidates.append(current)
    if not candidates:
        raise BackupRouteOwned("无法把来源提交绑定到现存 worktree；补充明确的 source-task-worktree 后重试")
    return candidates


@contextmanager
def normal_worktree_mutation(repo: Path, operation: str, run_dir: Path | None = None,
                            source_task_tip: str | None = None, source_task_worktree: Path | None = None):
    """正常写入必须覆盖完整副作用区间；异常结果保留供接管对账。"""
    repo = resolve_worktree_root(repo)
    root = control_directory(repo)
    reservation = root / "normal-mutations" / f"{uuid.uuid4().hex}.json"
    value = {"pid": os.getpid(), "process_identity": capture_process_identity(os.getpid()),
             "reservation_kind": "python_mutation_scope", "operation_state": "running",
             "operation": operation, "worktree": str(repo.resolve()),
             "original_run_directory": str(run_dir.resolve()) if run_dir else None,
             "head_before": optional_head(repo), "outcome": "in_flight"}
    durable_json(reservation, value)
    work_started = False
    try:
        consumed = consumed_run_record(repo, run_dir)
        if marker_present(root / "active-owner") or (consumed is not None and marker_present(consumed)):
            reservation.unlink()
            raise BackupRouteOwned("独立备用路线已接管或已消费原 run；读取持久接续记录，不重复执行")
        with ExitStack() as source_reservations:
            if source_task_tip:
                for source in source_task_worktrees(repo, source_task_tip, source_task_worktree):
                    if control_directory(source) != root:
                        source_reservations.enter_context(normal_worktree_mutation(source, operation + ":source-task"))
            context_token = CURRENT_NORMAL_MUTATION_RESERVATIONS.set((*CURRENT_NORMAL_MUTATION_RESERVATIONS.get(), reservation))
            try:
                work_started = True
                yield reservation
                assert_child_process_groups_stopped(json.loads(reservation.read_text()))
            finally:
                CURRENT_NORMAL_MUTATION_RESERVATIONS.reset(context_token)
    except BaseException:
        if not work_started:
            reservation.unlink(missing_ok=True)
        elif reservation.exists():
            value.update(json.loads(reservation.read_text()))
            value.update(outcome="unknown", operation_state="settled", head_after=optional_head(repo))
            durable_json(reservation, value)
        raise
    else:
        reservation.unlink(missing_ok=True)


def inspect_backup_route(repo: Path) -> dict:
    root = control_directory(repo)
    marker = root / "active-owner"
    if not marker_present(marker):
        return {"status": "normal", "control_directory": str(root)}
    try:
        identity = json.loads((marker / "identity.json").read_text())
    except (OSError, ValueError):
        return {"status": "ownership_evidence_missing", "control_directory": str(root)}
    pending = sorted(str(path) for path in (root / "normal-mutations").glob("*.json"))
    return {**identity, "status": "waiting_for_normal_mutations" if pending else "backup_active",
            "normal_mutation_reservations": pending}


def assert_backup_route_ready(repo: Path, operation_id: str) -> dict:
    identity = operation_identity(repo, operation_id)
    if list((control_directory(repo) / "normal-mutations").glob("*.json")):
        raise BackupRouteOwned("原写入尚未结束或结果未知；先对账，不开始备用写入")
    return identity


def prepare_backup_route_identity(repo: Path, operation_id: str, artifacts: Path, authorization: Path,
                                  reason: str, original_run: Path | None, delivery_stage: str) -> dict:
    repo = resolve_worktree_root(repo)
    validate_operation_id(operation_id)
    consumed = consumed_run_record(repo, original_run)
    if consumed is not None and marker_present(consumed):
        raise BackupRouteOwned("原 run 已被备用路线消费；复用原完成事实，不创建新的 operation")
    destination = (artifacts / operation_id).resolve()
    authorization_evidence = evidence_copy(authorization, destination / "raw-evidence")
    identity = {"operation_id": operation_id, "worktree": str(repo.resolve()),
                "worktree_git_directory": git(repo, "rev-parse", "--absolute-git-dir"),
                "git_common_directory": git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                "task_id": os.environ.get("KANBAN_TASK_ID"),
                "artifacts_directory": str(destination), "authorization_evidence": authorization_evidence,
                "reason": reason, "original_run_directory": str(original_run.resolve()) if original_run else None,
                "delivery_stage": delivery_stage, "head_at_claim": git(repo, "rev-parse", "HEAD")}
    return identity


def claim_backup_route(repo: Path, operation_id: str, artifacts: Path, authorization: Path,
                       reason: str, original_run: Path | None, delivery_stage: str) -> dict:
    validate_operation_id(operation_id)
    root = control_directory(repo)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "active-owner"
    recover_completed_active_owner(repo)
    if marker_present(marker):
        identity = operation_identity(repo, operation_id)
        if identity["artifacts_directory"] != str((artifacts / operation_id).resolve()):
            raise BackupRouteOwned("重启须复用原持久目录")
        return inspect_backup_route(repo)
    completed = root / "completed-operations" / f"{operation_id}.json"
    if completed.exists():
        value = read_completed_operation_result(repo, operation_id)
        if value.get("content_fingerprint") != worktree_content_fingerprint(repo):
            raise BackupRouteOwned("原 operation 已完成；新候选须使用新的独立 operation")
        return value
    identity = prepare_backup_route_identity(repo, operation_id, artifacts, authorization, reason, original_run, delivery_stage)
    destination = Path(identity["artifacts_directory"])
    # 先准备完整身份，再用不可覆盖的 symlink 原子发布；中断不会发布半份接管记录。
    durable_json(destination / "operation-identity.json", identity)
    generation = root / "ownership-identity-generations" / uuid.uuid4().hex
    durable_json(generation / "identity.json", identity)
    try:
        marker.symlink_to(generation, target_is_directory=True)
    except FileExistsError as exc:
        raise BackupRouteOwned("另一执行者已先接管；读取状态，不覆盖") from exc
    (destination / "operation.md").write_text(
        f"# 独立备用路线接续记录\n\n- operation：`{operation_id}`\n- 原 run：`{original_run}`\n"
        f"- 故障：{reason}\n- 授权来源：`{identity['authorization_evidence']['path']}`\n\n"
        "本记录不签发正常 RVF receipt；原失败与 evidence 必须保留。\n", encoding="utf-8")
    return inspect_backup_route(repo)


def adopt_manual_ownership(repo: Path, operation_id: str, artifacts: Path, authorization: Path,
                           reason: str, original_run: Path | None, delivery_stage: str, journal: Path) -> dict:
    validate_operation_id(operation_id)
    marker = control_directory(repo) / "active-owner"
    if not marker.is_dir() or marker.is_symlink():
        raise BackupRouteOwned("只能补录已核对归属的手工接管目录，不能覆盖正常 generation")
    if (marker / "identity.json").exists():
        operation_identity(repo, operation_id)
        return inspect_backup_route(repo)
    if (marker / "manual-operation-id").read_text().strip() != operation_id:
        raise BackupRouteOwned("手工接管目录的原 operation ID 不匹配；保留现场补证")
    identity = prepare_backup_route_identity(repo, operation_id, artifacts, authorization, reason, original_run, delivery_stage)
    destination = Path(identity["artifacts_directory"])
    identity["manual_ownership_reconciliation_evidence"] = evidence_copy(journal, destination / "raw-evidence")
    identity["ownership_origin"] = "agent_reconciled_manual_ownership"
    durable_json(destination / "operation-identity.json", identity)
    durable_json(marker / "identity.json", identity)
    return inspect_backup_route(repo)


def child_process_group_stop_evidence(group: int, identity: dict | None) -> str:
    if identity and identity.get("boot_session_id"):
        try:
            if identity["boot_session_id"] != current_boot_session_id():
                return "kernel_boot_session_changed"
        except OSError:
            pass
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return "kernel_process_group_absent"
    except PermissionError as exc:
        raise BackupRouteOwned("下属命令的进程组状态未知；权限错误不能视为死亡") from exc
    if identity and identity.get("process_start_id"):
        try:
            current = read_process_identity(group)
        except ProcessLookupError:
            current = None
        except OSError:
            current = None
        if (current and current["process_group_id"] == group
                and current["boot_session_id"] == identity.get("boot_session_id")
                and current["process_start_id"] != identity["process_start_id"]):
            return "kernel_process_group_generation_changed"
    # 已回收的普通命令在上面的内核查询即完成核验；仅剩组员或僵尸时才需要枚举。
    try:
        processes = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BackupRouteOwned("下属命令的进程组仍存在且无法枚举；保留未知，待原生退出事实后重验") from exc
    if any(int(parts[0]) == group and not parts[1].startswith("Z")
           for line in processes.splitlines() if len(parts := line.split()) >= 2):
        raise BackupRouteOwned("下属命令的进程组仍在运行；父进程退出不能替代完整对账")
    return "process_listing_has_no_live_group_members"


def assert_child_process_groups_stopped(value: dict) -> None:
    groups = set(value.get("child_process_group_ids", []))
    if isinstance(value.get("child_process_group_id"), int):
        groups.add(value["child_process_group_id"])
    for group in groups:
        completion = value.get("child_process_group_completion_facts", {}).get(str(group))
        if (completion and completion.get("process_group_stopped") is True
                and completion.get("leader_process_identity") == value.get("child_process_group_identities", {}).get(str(group))):
            continue
        child_process_group_stop_evidence(group, value.get("child_process_group_identities", {}).get(str(group)))


def native_operation_quiescence_is_verified(value: dict) -> bool:
    """长期 Node 服务只凭精确局部操作与原生控制面对账，不能凭服务 PID 放行。"""
    proof, context = value.get("native_outcome_evidence"), value.get("reconciliation_context")
    if value.get("operation_state") != "settled" or not isinstance(proof, dict) or not isinstance(context, dict):
        return False
    if (proof.get("schema") != "kanban-normal-closeout-quiescence/v1" or not value.get("operation_id")
            or proof.get("operation_id") != value.get("operation_id")):
        return False
    for name in ("workspace_id", "task_id", "decision_token", "run_id", "run_directory"):
        if not isinstance(proof.get(name), str) or not proof[name] or proof[name] != context.get(name):
            return False
    if (proof.get("queued_delivery_absent") is not True or proof.get("terminal_delivery_pending") is not False
            or proof.get("native_control_verified_idle") is not True):
        return False
    for name in ("observed_at", "conversation_session_id", "native_session_id", "runtime_session_incarnation_id", "delivery_idempotency_key"):
        if not isinstance(proof.get(name), str) or not proof[name]:
            return False
    return all(isinstance(proof.get(name), str) and re.fullmatch(r"[0-9a-fA-F]{64}", proof[name])
               for name in ("native_turns_sha256", "delivery_ledger_sha256"))


def reconcile_normal_mutation(repo: Path, operation_id: str, reservation_name: str, evidence: Path) -> None:
    identity = operation_identity(repo, operation_id)
    if Path(reservation_name).name != reservation_name:
        raise ValueError("reservation 必须是单个文件名")
    path = control_directory(repo) / "normal-mutations" / reservation_name
    value = json.loads(path.read_text())
    assert_child_process_groups_stopped(value)
    if not isinstance(value.get("pid"), int) or value["pid"] <= 0:
        raise ValueError("reservation 的进程身份无效，先补证")
    local_python_scope_settled = (value.get("reservation_kind") == "python_mutation_scope"
                                  and value.get("operation_state") == "settled" and value.get("outcome") == "unknown")
    native_operation_verified = native_operation_quiescence_is_verified(value)
    if "reconciliation_context" in value and not native_operation_verified:
        raise BackupRouteOwned("局部原生操作尚缺身份绑定的控制面完成事实；自动补证后重验")
    if not local_python_scope_settled and not native_operation_verified and original_process_is_running(value["pid"], value.get("process_identity")):
        raise BackupRouteOwned("原进程仍在执行或局部原生操作尚未对账；保留 reservation，待精确完成事实后重验")
    destination = Path(identity["artifacts_directory"])
    proof = evidence_copy(evidence, destination / "raw-evidence")
    durable_json(destination / "reconciled-normal-mutations" / reservation_name,
                 {**value, "reconciliation_evidence": proof, "head_at_reconciliation": git(repo, "rev-parse", "HEAD")})
    path.unlink()


def tracked_subprocess_run(command, *, input=None, capture_output=False, timeout=None, check=False,
                           reservation: Path | None = None, **options):
    """在正常写入作用域中登记每个下属进程组，刷盘后才放行真实命令。"""
    reservations = CURRENT_NORMAL_MUTATION_RESERVATIONS.get()
    if reservation is not None and reservation not in reservations:
        reservations = (*reservations, reservation)
    if not reservations:
        return subprocess.run(command, input=input, capture_output=capture_output, timeout=timeout, check=check, **options)
    if isinstance(command, (str, bytes)) or options.get("shell") or options.get("executable"):
        raise ValueError("受保护命令必须使用明确的 argv，不支持 shell 或 executable 覆盖")
    if input is not None:
        if options.get("stdin") is not None:
            raise ValueError("input 与 stdin 不能同时使用")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        if options.get("stdout") is not None or options.get("stderr") is not None:
            raise ValueError("capture_output 与 stdout/stderr 不能同时使用")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    read_descriptor, write_descriptor = os.pipe()
    bootstrap = ("import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); "
                 "sys.exit(73) if ready!=b'1' else os.execvpe(sys.argv[2],sys.argv[2:],os.environ)")
    supplied_environment = options.pop("env", None)
    environment = dict(os.environ if supplied_environment is None else supplied_environment)
    environment["RVF_INDEPENDENT_BACKUP_ROUTE_PARENT_RESERVATION"] = str(reservations[-1])
    options["start_new_session"] = True
    options["pass_fds"] = (*options.get("pass_fds", ()), read_descriptor)
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(read_descriptor), *command], env=environment, **options)
        os.close(read_descriptor)
        read_descriptor = -1
        with NORMAL_MUTATION_RESERVATION_UPDATE_LOCK:
            for owned_reservation in reservations:
                value = json.loads(owned_reservation.read_text())
                groups = value.setdefault("child_process_group_ids", [])
                groups.append(process.pid)
                value.setdefault("child_process_group_identities", {})[str(process.pid)] = capture_process_identity(process.pid)
                value.setdefault("child_process_group_completion_facts", {}).pop(str(process.pid), None)
                value.update(child_process_group_id=process.pid, child_process_id=process.pid)
                durable_json(owned_reservation, value)
        os.write(write_descriptor, b"1")
    except BaseException:
        # 未放行的 bootstrap 会读到 EOF；回收它，避免测试和常驻调用者留下僵尸。
        os.close(write_descriptor)
        write_descriptor = -1
        if process is not None:
            process.wait()
        raise
    finally:
        if read_descriptor >= 0:
            os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    try:
        group_stop_evidence = child_process_group_stop_evidence(process.pid, None)
    except BackupRouteOwned:
        pass
    else:
        with NORMAL_MUTATION_RESERVATION_UPDATE_LOCK:
            for owned_reservation in reservations:
                value = json.loads(owned_reservation.read_text())
                value.setdefault("child_process_group_completion_facts", {})[str(process.pid)] = {
                    "process_group_stopped": True, "verification_source": group_stop_evidence,
                    "leader_returncode": process.returncode,
                    "leader_process_identity": value["child_process_group_identities"][str(process.pid)]}
                durable_json(owned_reservation, value)
    if check:
        result.check_returncode()
    return result


def run_reserved_command(repo: Path, reservation: Path, command: list[str]) -> int:
    code = tracked_subprocess_run(command, cwd=repo, reservation=reservation).returncode
    if code:
        raise NormalMutationCommandFailed(code)
    return code


def assert_git_ancestor(repo: Path, ancestor: str, descendant: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
                               capture_output=True, text=True, env=repository_git_environment())
    if completed.returncode:
        raise ValueError(f"当前 worktree 的 Git 祖先关系未成立：{ancestor} -> {descendant}")


def verified_base_sync_snapshot(repo: Path, head: str, base_ref: str) -> dict:
    if not base_ref or not base_ref.startswith("refs/heads/"):
        raise ValueError("必须给出完整本地 base ref")
    base_tip = git(repo, "rev-parse", "--verify", base_ref + "^{commit}")
    assert_git_ancestor(repo, head, base_tip)
    task_tree, base_tree = git(repo, "rev-parse", head + "^{tree}"), git(repo, "rev-parse", base_tip + "^{tree}")
    if task_tree != base_tree:
        raise ValueError("base 与 task 提交 tree 不一致；同步尚未完成")
    previous_base_tip = base_tip
    if base_tip != head:
        parents = git(repo, "show", "-s", "--format=%P", base_tip).split()
        if not parents:
            raise ValueError("base 缺少可核对的落地前 first parent")
        previous_base_tip = parents[0]
        assert_git_ancestor(repo, previous_base_tip, head)
    return {"base_ref": base_ref, "base_tip": base_tip, "base_tree": base_tree,
            "task_head": head, "task_tree": task_tree, "base_previous_tip": previous_base_tip}


def verified_base_sync_continuation(repo: Path, fact: dict) -> dict:
    """同步快照之后允许 base 单调前进；当前资格不能丢失原快照与候选。"""
    current_base_tip = git(repo, "rev-parse", "--verify", fact["base_ref"] + "^{commit}")
    try:
        assert_git_ancestor(repo, fact["base_tip"], current_base_tip)
        assert_git_ancestor(repo, fact["task_head"], current_base_tip)
    except ValueError as exc:
        raise ValueError("base 不再保留已登记同步快照／候选；历史成功事实保留，当前依赖动作待补证") from exc
    return {"base_ref": fact["base_ref"], "recorded_base_tip": fact["base_tip"],
            "recorded_base_tree": fact["base_tree"], "recorded_task_head": fact["task_head"],
            "current_base_tip": current_base_tip, "current_base_tree": git(repo, "rev-parse", current_base_tip + "^{tree}"),
            "base_advanced_after_stage": current_base_tip != fact["base_tip"], "recorded_snapshot_preserved": True}


def record_stage_fact(repo: Path, operation_id: str, stage: str, evidence: Path,
                      commit: str | None, base_ref: str | None) -> dict:
    identity = assert_backup_route_ready(repo, operation_id)
    if stage not in STAGES:
        raise ValueError("未知阶段")
    destination = Path(identity["artifacts_directory"])
    result = {"stage": stage, "evidence": evidence_copy(evidence, destination / "raw-evidence"),
              "attestation_origin": "executing_agent", "head": git(repo, "rev-parse", "HEAD"),
              "content_fingerprint": worktree_content_fingerprint(repo)}
    if stage in {"land", "base_sync"}:
        if not commit:
            raise ValueError("必须给出实际提交")
        result["commit"] = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        assert_git_ancestor(repo, result["commit"], result["head"])
        if stage == "base_sync":
            if result["commit"] != result["head"]:
                raise ValueError("base 同步必须核验当前 task HEAD，不能用旧祖先提交代替")
            result.update(verified_base_sync_snapshot(repo, result["head"], base_ref))
    digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    durable_json(destination / "stage-fact-history" / stage / f"{digest}.json", result)
    durable_json(destination / "stage-facts" / f"{stage}.json", result)
    return result


def freeze_candidate(repo: Path, operation_id: str, paths: list[str], comparison_base: str) -> dict:
    repo = resolve_worktree_root(repo)
    identity = assert_backup_route_ready(repo, operation_id)
    if not paths:
        raise ValueError("必须显式给出候选与必要上下文路径，不能默认全量 dirty")
    for name in paths:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path == Path(".") or ".git" in path.parts or (repo / path).is_dir():
            raise ValueError("候选路径必须是明确的仓内相对文件路径")
        if not (repo / path).resolve().is_relative_to(repo.resolve()):
            raise ValueError("候选链接不能逃出仓库")
    before = worktree_content_fingerprint(repo)
    destination = Path(identity["artifacts_directory"])
    with tempfile.TemporaryDirectory(prefix="backup-route-snapshot-index-") as isolated:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment["GIT_INDEX_FILE"] = str(Path(isolated) / "candidate-index")
        def snapshot_git(*arguments):
            completed = subprocess.run(["git", "-C", str(repo), *arguments], env=environment, capture_output=True, text=True)
            if completed.returncode:
                raise ValueError(completed.stderr.strip())
            return completed.stdout.strip()
        base = snapshot_git("rev-parse", "--verify", comparison_base + "^{commit}")
        snapshot_git("read-tree", "HEAD")
        for name in paths:
            path = repo / name
            if not marker_present(path):
                snapshot_git("update-index", "--force-remove", "--", name)
                continue
            content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--no-filters", "--stdin"],
                                  input=content, capture_output=True, env=environment, check=True).stdout.decode().strip()
            mode = "120000" if path.is_symlink() else ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644")
            snapshot_git("update-index", "--add", "--cacheinfo", mode, blob, name)
        tree = snapshot_git("write-tree")
        commit = snapshot_git("-c", "user.name=RVF Candidate Snapshot", "-c", "user.email=rvf-snapshot@localhost",
                              "commit-tree", tree, "-p", "HEAD", "-m", "独立备用路线不可变候选快照")
        snapshot_git("update-ref", "refs/rvf/independent-backup-route/" + operation_id + "/" + commit, commit)
    if before != worktree_content_fingerprint(repo):
        raise ValueError("冻结期间内容变化；保留快照但不能作为当前候选使用")
    value = {"candidate_commit": commit, "candidate_tree": tree, "comparison_base_tip": base,
             "selected_paths": paths, "content_fingerprint": before}
    durable_json(destination / "candidate-snapshots" / (commit + ".json"), value)
    durable_json(destination / "current-candidate.json", value)
    return value


def worktree_content_fingerprint(repo: Path) -> str:
    repo = resolve_worktree_root(repo)
    environment = repository_git_environment()
    digest = hashlib.sha256(b"rvf-independent-backup-route-content-v2")
    def append_field(label: bytes, content: bytes):
        digest.update(len(label).to_bytes(8, "big") + label + len(content).to_bytes(8, "big") + content)
    append_field(b"head", (optional_head(repo) or "unborn").encode())
    append_field(b"index", subprocess.check_output(["git", "-C", str(repo), "ls-files", "--stage", "-z"], env=environment))
    names = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], env=environment)
    for name in sorted(set(filter(None, names.split(b"\0")))):
        path = repo / os.fsdecode(name)
        append_field(b"path", name)
        if not marker_present(path):
            append_field(b"absent", b"")
        elif path.is_symlink():
            append_field(b"symlink", os.fsencode(os.readlink(path)))
        elif path.is_dir():
            append_field(b"directory", b"")
            if marker_present(path / ".git"):
                append_field(b"nested-worktree", worktree_content_fingerprint(path).encode())
        else:
            append_field(b"file-executable-mode", str(path.stat().st_mode & 0o111).encode())
            append_field(b"file-content", path.read_bytes())
    return digest.hexdigest()


def read_completed_operation_result(repo: Path, operation_id: str) -> dict:
    validate_operation_id(operation_id)
    result = json.loads((control_directory(repo) / "completed-operations" / f"{operation_id}.json").read_text())
    return validate_completed_operation_result(repo, operation_id, result)


def validate_completed_operation_result(repo: Path, operation_id: str, result: dict) -> dict:
    if (not isinstance(result, dict) or result.get("status") != "backup_closed"
            or result.get("operation_id") != operation_id
            or result.get("worktree_git_directory") != str(control_directory(repo).parent)
            or not isinstance(result.get("final_head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result["final_head"])
            or not isinstance(result.get("content_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["content_fingerprint"])):
        raise BackupRouteOwned("已完成记录的 worktree／operation 身份不一致")
    return result


def restore_completed_operation_bookkeeping(repo: Path, result: dict) -> None:
    """恢复历史完成事实的消费与归档，不以当前新候选内容否定原完成。"""
    root, operation_id = control_directory(repo), result["operation_id"]
    if result.get("original_run_directory"):
        consumed = consumed_run_record(repo, Path(result["original_run_directory"]))
        if consumed.is_file() and consumed.stat().st_size:
            try:
                existing = json.loads(consumed.read_text())
            except (OSError, ValueError):
                existing = None
            if existing != result:
                evidence_copy(consumed, root / "recovered-consumption-record-originals")
        durable_json(consumed, result)
    marker = root / "active-owner"
    if marker_present(marker):
        try:
            current = json.loads((marker / "identity.json").read_text())
        except (OSError, ValueError) as exc:
            raise BackupRouteOwned("完成事实存在但接管身份不可读；保留现场补证") from exc
        if not isinstance(current, dict):
            raise BackupRouteOwned("完成事实存在但接管身份不是对象；保留现场补证")
        if current.get("operation_id") != operation_id:
            return
        operation_identity(repo, operation_id)
        archive = root / "closed-ownership-markers" / operation_id
        archive.parent.mkdir(parents=True, exist_ok=True)
        if marker_present(archive):
            raise BackupRouteOwned("已存在同名接管归档；核对原持久事实后恢复，不能覆盖")
        os.rename(marker, archive)


def recover_completed_active_owner(repo: Path) -> None:
    marker = control_directory(repo) / "active-owner"
    if not marker_present(marker):
        return
    try:
        operation_id = json.loads((marker / "identity.json").read_text()).get("operation_id")
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(operation_id, str):
        validate_operation_id(operation_id)
        evidence = read_backup_operation_completion_evidence(repo, operation_id)
        if evidence is not None:
            completed = control_directory(repo) / "completed-operations" / f"{operation_id}.json"
            if evidence["completion_record_path"] != str(completed):
                if completed.is_file() and completed.stat().st_size:
                    evidence_copy(completed, control_directory(repo) / "recovered-completion-record-originals")
                durable_json(completed, evidence["result"])
            restore_completed_operation_bookkeeping(repo, evidence["result"])


def finish_backup_route(repo: Path, operation_id: str, report: Path) -> dict:
    validate_operation_id(operation_id)
    recover_completed_active_owner(repo)
    completed = control_directory(repo) / "completed-operations" / f"{operation_id}.json"
    if completed.is_file():
        result = read_completed_operation_result(repo, operation_id)
        restore_completed_operation_bookkeeping(repo, result)
        if result.get("content_fingerprint") != worktree_content_fingerprint(repo):
            raise ValueError("原完成事实已恢复归档；完成后内容已改变，新候选须使用新的 operation")
        return result
    identity = assert_backup_route_ready(repo, operation_id)
    destination = Path(identity["artifacts_directory"])
    required = STAGES[:STAGES.index(identity["delivery_stage"]) + 1]
    base_sync_observation = None
    for stage in required:
        fact = json.loads((destination / "stage-facts" / f"{stage}.json").read_text()) if (destination / "stage-facts" / f"{stage}.json").exists() else None
        if fact is None:
            raise ValueError(f"尚缺 {stage} 的真实结果记录")
        proof = fact["evidence"]
        if hashlib.sha256(Path(proof["path"]).read_bytes()).hexdigest() != proof["sha256"]:
            raise ValueError("阶段原始证据已改变；保留未知状态并重新核验")
        if stage == "base_sync":
            base_sync_observation = verified_base_sync_continuation(repo, fact)
    if fact["content_fingerprint"] != worktree_content_fingerprint(repo):
        raise ValueError("最后阶段记录之后内容已改变，先核验当前内容")
    result = {**identity, "status": "backup_closed", "report": evidence_copy(report, destination / "raw-evidence"),
              "final_head": git(repo, "rev-parse", "HEAD"), "content_fingerprint": worktree_content_fingerprint(repo)}
    if base_sync_observation is not None:
        result["base_sync_completion_observation"] = base_sync_observation
    durable_json(destination / "backup-route-delivery-result.json", result)
    root = control_directory(repo)
    completed = root / "completed-operations" / f"{operation_id}.json"
    durable_json(completed, result)
    original = identity.get("original_run_directory")
    if original:
        durable_json(consumed_run_record(repo, Path(original)), result)
    # 完成事实先于移除接管标记；重启可从已写结果对账，不能先释放再记账。
    archive = root / "closed-ownership-markers" / operation_id
    archive.parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / "active-owner", archive)
    return result


def read_backup_operation_completion_evidence(repo: Path, operation_id: str,
                                            consumption_locator: dict | None = None) -> dict | None:
    """只读：优先核对 Git 内完成记录，缺失时沿同 operation 身份查持久交付原件。"""
    validate_operation_id(operation_id)
    root = control_directory(repo)
    completed = root / "completed-operations" / f"{operation_id}.json"
    failures = []
    try:
        return {"result": read_completed_operation_result(repo, operation_id), "completion_record_path": str(completed)}
    except FileNotFoundError:
        pass
    except (BackupRouteOwned, OSError, ValueError) as exc:
        failures.append(f"{completed}: {exc}")
    locators = [consumption_locator] if consumption_locator else []
    for marker in (root / "active-owner", root / "closed-ownership-markers" / operation_id):
        try:
            identity = json.loads((marker / "identity.json").read_text())
            if not isinstance(identity, dict):
                raise ValueError("接管身份不是对象")
            if identity.get("operation_id") == operation_id:
                locators.append(identity)
            elif marker == root / "closed-ownership-markers" / operation_id:
                raise ValueError("已归档接管身份的 operation 不一致")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            failures.append(f"{marker}: {exc}")
    for identity in locators:
        if (identity.get("operation_id") != operation_id
                or identity.get("worktree_git_directory") != str(root.parent)
                or not isinstance(identity.get("artifacts_directory"), str)
                or not Path(identity["artifacts_directory"]).is_absolute()):
            failures.append("持久交付定位信息的 worktree／operation 身份不一致")
            continue
        delivery = Path(identity["artifacts_directory"]) / "backup-route-delivery-result.json"
        try:
            result = validate_completed_operation_result(repo, operation_id, json.loads(delivery.read_text()))
            if any(result.get(field) != identity.get(field) for field in (
                "original_run_directory", "artifacts_directory", "worktree_git_directory", "operation_id",
            )):
                raise ValueError("持久交付结果与原接管身份不一致")
            return {"result": result, "completion_record_path": str(delivery)}
        except FileNotFoundError as exc:
            if identity is consumption_locator or marker_present(root / "closed-ownership-markers" / operation_id):
                failures.append(f"{delivery}: {exc}")
        except (BackupRouteOwned, OSError, ValueError) as exc:
            failures.append(f"{delivery}: {exc}")
    if failures:
        raise BackupRouteOwned("；".join(failures))
    return None


def backup_completion_recovery_operation_page(root: Path, after_operation: str | None = None,
                                             priority_operation: str | None = None) -> tuple[list[str], str | None]:
    """限制每次补证的 operation 数；游标由调用方持久保存，重启后继续下一页。"""
    operations = {path.stem for path in (root / "completed-operations").glob("*.json")}
    operations.update(path.name for path in (root / "closed-ownership-markers").glob("*"))
    try:
        identity = json.loads((root / "active-owner" / "identity.json").read_text())
        if isinstance(identity, dict) and isinstance(identity.get("operation_id"), str):
            operations.add(identity["operation_id"])
    except (OSError, ValueError):
        pass
    remaining = sorted(name for name in operations if name != priority_operation and (not after_operation or name > after_operation))
    page = remaining[:STOP_COMPLETION_RECOVERY_OPERATION_LIMIT]
    next_operation = page[-1] if len(remaining) > len(page) else None
    return ([priority_operation] if priority_operation else []) + page, next_operation


def backup_operation_identity_excludes_original_run(repo: Path, operation_id: str, original_run: str) -> bool:
    """仅凭同 worktree、同 operation 的完整身份，排除其他 run 的补证缺口。"""
    root, identified_runs = control_directory(repo), set()
    archive = root / "closed-ownership-markers" / operation_id
    for marker in (root / "active-owner", archive):
        try:
            identity = json.loads((marker / "identity.json").read_text())
        except FileNotFoundError:
            if marker_present(marker):
                return False
            continue
        except (OSError, ValueError):
            return False
        if not isinstance(identity, dict):
            return False
        if marker != archive and identity.get("operation_id") != operation_id:
            continue
        if (identity.get("operation_id") != operation_id
                or identity.get("worktree_git_directory") != str(root.parent)
                or not isinstance(identity.get("artifacts_directory"), str)
                or not Path(identity["artifacts_directory"]).is_absolute()
                or "original_run_directory" not in identity):
            return False
        identity_run = identity["original_run_directory"]
        if identity_run is not None and (not isinstance(identity_run, str) or not Path(identity_run).is_absolute()):
            return False
        identified_runs.add(identity_run)
    return len(identified_runs) == 1 and original_run not in identified_runs


def read_original_run_backup_consumption_evidence(repo: Path, run_dir: Path,
                                                after_operation: str | None = None) -> dict:
    """只读历史消费事实；当前 HEAD／候选变化不撤销原 run 已交付的事实。"""
    root, original_run = control_directory(repo), str(run_dir.expanduser().resolve())
    consumed = consumed_run_record(repo, Path(original_run))
    locator, failures = None, []
    try:
        locator = json.loads(consumed.read_text())
        if (not isinstance(locator, dict) or locator.get("original_run_directory") != original_run
                or locator.get("worktree_git_directory") != str(root.parent)):
            raise ValueError("消费记录的精确 run／worktree 身份不一致")
        validate_operation_id(locator.get("operation_id"))
    except FileNotFoundError:
        locator = None
    except (OSError, ValueError, TypeError) as exc:
        locator = None
        failures.append(f"{consumed}: {exc}")
    operations, next_operation = backup_completion_recovery_operation_page(root, after_operation, (locator or {}).get("operation_id"))
    for operation_id in operations:
        try:
            operation_locator = locator if locator and locator["operation_id"] == operation_id else None
            evidence = read_backup_operation_completion_evidence(repo, operation_id, operation_locator)
            if evidence and evidence["result"].get("original_run_directory") == original_run:
                return {"status": "consumed", "original_run_directory": original_run, **evidence}
        except (BackupRouteOwned, OSError, ValueError) as exc:
            if not backup_operation_identity_excludes_original_run(repo, operation_id, original_run):
                failures.append(f"{operation_id}: {exc}")
    if locator is not None:
        failures.append("消费索引存在，但尚缺同 operation、同 run 的持久完成记录")
    return {"status": "unknown" if failures or next_operation else "not_consumed",
            "original_run_directory": original_run, "verification_gaps": failures,
            "recovery_after_operation": next_operation,
            "recovery_condition": "后续 Stop 自动接续下一页补证；同 operation 的完成记录与身份恢复可读后重新核验"}


def preserve_stop_backup_verification_observation(root: Path, name: str, observation: dict) -> None:
    try:
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
        durable_json(root / (name + "-history") / f"{digest}.json", observation)
        durable_json(root / (name + ".json"), observation)
    except OSError:
        pass


def read_stop_backup_verification_observation(root: Path, name: str) -> dict:
    try:
        value = json.loads((root / (name + ".json")).read_text())
        if not isinstance(value, dict):
            return {}
        if not isinstance(value.get("recovery_after_operation"), str):
            value.pop("recovery_after_operation", None)
        value["verification_gaps"] = [gap for gap in value.get("verification_gaps", []) if isinstance(gap, str)] if isinstance(value.get("verification_gaps"), list) else []
        return value
    except (OSError, ValueError):
        return {}


def continue_stop_backup_verification_page(previous: dict, evidence: dict) -> dict:
    """跨页保留未解决缺口；一轮扫描结束后重置游标，下轮重新补证旧缺口。"""
    if previous.get("recovery_after_operation") and evidence["status"] in {"not_consumed", "not_covered", "unknown"}:
        gaps = list(dict.fromkeys([*previous.get("verification_gaps", []), *evidence.get("verification_gaps", [])]))
        if gaps:
            evidence.update(status="unknown", verification_gaps=gaps)
    return evidence


def reconcile_original_run_backup_consumption(repo: Path, run_dir: Path) -> dict:
    """补证只修消费索引与接管归档；失败历史、token 和正常 receipt 都不改写。"""
    root = control_directory(repo)
    consumed = consumed_run_record(repo, run_dir)
    observation_name = "stop-original-run-verification-" + consumed.stem
    previous = read_stop_backup_verification_observation(root, observation_name)
    evidence = continue_stop_backup_verification_page(previous, read_original_run_backup_consumption_evidence(
        repo, run_dir, previous.get("recovery_after_operation")))
    if evidence["status"] == "consumed":
        try:
            result = evidence["result"]
            if not consumed.is_file() or consumed.read_bytes() != (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode():
                restore_completed_operation_bookkeeping(repo, result)
        except (BackupRouteOwned, OSError, ValueError) as exc:
            # 补写失败不能抹去已经核验的历史完成，也不能再催旧 run。
            evidence["bookkeeping_recovery_error"] = str(exc)
    if evidence["status"] != "not_consumed" or previous:
        preserve_stop_backup_verification_observation(root, observation_name, evidence)
    return evidence


def read_current_candidate_backup_coverage_evidence(repo: Path, after_operation: str | None = None,
                                                  *, previous_verification_observation: dict | None = None) -> dict:
    """只读当前覆盖；历史消费既不代替新内容的审查，也不要求新 HEAD 等于历史 HEAD。"""
    root = control_directory(repo)
    if marker_present(root / "active-owner"):
        return {"status": "owned"}
    operations, next_operation = backup_completion_recovery_operation_page(root, after_operation)
    if not operations and not previous_verification_observation:
        return {"status": "not_covered", "verification_gaps": [], "recovery_after_operation": None}
    try:
        fingerprint, head = worktree_content_fingerprint(repo), optional_head(repo)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return {"status": "unknown", "verification_gaps": [str(exc)], "recovery_after_operation": None,
                "recovery_condition": "Git／文件读取恢复后重新核验当前候选，不复用旧分页与覆盖缓存"}
    priority_operation = None
    requested_after_operation = after_operation
    if previous_verification_observation is not None:
        if previous_verification_observation.get("candidate_content_fingerprint") != fingerprint:
            after_operation = None
        elif previous_verification_observation.get("status") == "covered":
            previous_result = previous_verification_observation.get("result")
            if isinstance(previous_result, dict) and isinstance(previous_result.get("operation_id"), str):
                priority_operation = previous_result["operation_id"]
    if after_operation != requested_after_operation or priority_operation is not None:
        operations, next_operation = backup_completion_recovery_operation_page(root, after_operation, priority_operation)
    failures = []
    for operation_id in operations:
        try:
            evidence = read_backup_operation_completion_evidence(repo, operation_id)
            if evidence and evidence["result"].get("final_head") == head:
                if evidence["result"]["content_fingerprint"] == fingerprint:
                    return {"status": "covered", "candidate_content_fingerprint": fingerprint, **evidence}
        except (BackupRouteOwned, OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(f"{operation_id}: {exc}")
    return {"status": "unknown" if failures or next_operation else "not_covered",
            "candidate_content_fingerprint": fingerprint, "verification_gaps": failures,
            "recovery_after_operation": next_operation,
            "recovery_condition": "后续 Stop 自动接续补证；Git／文件读取恢复后重新核验当前候选"}


def should_suppress_normal_stop(repo: Path) -> bool:
    try:
        root = control_directory(repo)
    except (ValueError, OSError, subprocess.CalledProcessError):
        return False
    observation_name = "stop-content-verification-observation"
    try:
        recover_completed_active_owner(repo)
    except (BackupRouteOwned, OSError, ValueError, subprocess.CalledProcessError) as exc:
        preserve_stop_backup_verification_observation(root, observation_name, {
            "status": "unknown", "verification_gaps": [str(exc)],
            "recovery_condition": "同 operation 完成记录与接管身份恢复可读后自动对账归档"})
    previous = read_stop_backup_verification_observation(root, observation_name)
    evidence = read_current_candidate_backup_coverage_evidence(
        repo, previous.get("recovery_after_operation"), previous_verification_observation=previous)
    if (evidence.get("candidate_content_fingerprint") is not None
            and previous.get("candidate_content_fingerprint") == evidence["candidate_content_fingerprint"]):
        evidence = continue_stop_backup_verification_page(previous, evidence)
    if evidence["status"] != "not_covered" or previous:
        preserve_stop_backup_verification_observation(root, observation_name, evidence)
    # 尚有下一页时先等待机器补证；记录损坏但本轮已查尽时仍允许新候选进入正常判断。
    return evidence["status"] in {"owned", "covered"} or (
        evidence["status"] == "unknown" and bool(evidence.get("recovery_after_operation")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "claim", "adopt-manual-ownership", "freeze-candidate", "record-stage", "finish", "reconcile-normal-mutation", "run-normal-mutation", "validate-normal-reservation"])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--artifacts-directory", type=Path)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--original-run-directory", type=Path)
    parser.add_argument("--delivery-stage", choices=STAGES, default="land")
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--base-ref")
    parser.add_argument("--reservation-name")
    parser.add_argument("--source-task-tip")
    parser.add_argument("--source-task-worktree", type=Path)
    parser.add_argument("--candidate-path", action="append", default=[])
    parser.add_argument("--comparison-base-tip")
    args, command = parser.parse_known_args()
    if command and args.action != "run-normal-mutation":
        parser.error("未知参数：" + " ".join(command))
    fields = {"record-stage": ["operation_id", "stage", "evidence_file"],
              "adopt-manual-ownership": ["operation_id", "artifacts_directory", "authorization_file", "reason", "evidence_file"],
              "finish": ["operation_id", "evidence_file"],
              "freeze-candidate": ["operation_id", "comparison_base_tip"],
              "reconcile-normal-mutation": ["operation_id", "reservation_name", "evidence_file"]}
    missing = [name for name in fields.get(args.action, []) if getattr(args, name) is None]
    if missing:
        parser.error("缺少必要参数：" + ", ".join(missing))
    try:
        repo = resolve_worktree_root(args.repo)
        if args.action == "status":
            value = inspect_backup_route(repo)
        elif args.action == "claim":
            if not all([args.operation_id, args.artifacts_directory, args.authorization_file, args.reason]):
                parser.error("claim 需要 operation、持久目录、授权来源与故障原因")
            value = claim_backup_route(repo, args.operation_id, args.artifacts_directory, args.authorization_file,
                                       args.reason, args.original_run_directory, args.delivery_stage)
        elif args.action == "adopt-manual-ownership":
            value = adopt_manual_ownership(repo, args.operation_id, args.artifacts_directory, args.authorization_file,
                                            args.reason, args.original_run_directory, args.delivery_stage, args.evidence_file)
        elif args.action == "record-stage":
            value = record_stage_fact(repo, args.operation_id, args.stage, args.evidence_file, args.commit, args.base_ref)
        elif args.action == "freeze-candidate":
            if not args.comparison_base_tip:
                parser.error("冻结需要明确 comparison base tip")
            value = freeze_candidate(repo, args.operation_id, args.candidate_path, args.comparison_base_tip)
        elif args.action == "finish":
            value = finish_backup_route(repo, args.operation_id, args.evidence_file)
        elif args.action == "reconcile-normal-mutation":
            reconcile_normal_mutation(repo, args.operation_id, args.reservation_name, args.evidence_file)
            value = inspect_backup_route(repo)
        elif args.action == "validate-normal-reservation":
            reservation = Path(os.environ.get("RVF_INDEPENDENT_BACKUP_ROUTE_PARENT_RESERVATION", "/missing"))
            if reservation.parent.resolve() != (control_directory(repo) / "normal-mutations").resolve():
                raise BackupRouteOwned("父 reservation 不属于当前 worktree")
            owner = json.loads(reservation.read_text())
            parent = read_process_identity(os.getppid())
            if (parent["parent_process_id"] != owner["pid"]
                    or not original_process_is_running(owner["pid"], owner.get("process_identity"))):
                raise BackupRouteOwned("内部 launcher 的真实父进程与 reservation 不一致")
            value = {"status": "normal_reservation_verified"}
        else:
            command = command[1:] if command[:1] == ["--"] else command
            if not command:
                parser.error("需要明确命令 argv")
            with normal_worktree_mutation(repo, args.reason or "external-normal-mutation", args.original_run_directory,
                                          args.source_task_tip, args.source_task_worktree) as reservation:
                return run_reserved_command(repo, reservation, command)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except NormalMutationCommandFailed as exc:
        print(str(exc), file=sys.stderr)
        return exc.returncode
    except (BackupRouteOwned, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
