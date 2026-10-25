"""实际 dispatcher 自登记的交付证据；外层 launcher/monitor 退出不证明 writer 结束。"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import _rvf_pyroot  # noqa: F401
from core.run_ledger.review_run_lifecycle import atomic_write_json, read_json_object


DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE = "RVF_DISPATCHER_WRITER_EXECUTION_ID"
DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE = "RVF_DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH"
DISPATCHER_WRITER_EXECUTION_IDENTITY_FIELD_NAMES = ("dispatcher_writer_execution_id", "run_id", "run_dir", "repo_realpath", "session_id", "dispatcher_status_path",
                   "tmux_session", "dispatcher_writer_execution_command_sha256", "dispatcher_writer_execution_entrypoint_realpath", "dispatcher_writer_execution_entrypoint_sha256")
DISPATCHER_WRITER_EXECUTION_PRELAUNCH_RECOVERY_ATTEMPTS_PER_INPUT_GENERATION = 3
DISPATCHER_WRITER_EXECUTION_START_OR_ATTEMPT_FACT_FIELD_NAMES = (
    "dispatcher_writer_execution_tmux_attempted_at", "actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at",
    "dispatcher_writer_execution_monitor_pid", "dispatcher_writer_execution_monitor_child_pid", "dispatcher_writer_execution_monitor_command_spawn_failed",
    "dispatcher_writer_execution_tmux_launcher_process_pid", "dispatcher_writer_execution_tmux_launcher_process_started_at",
)


@contextmanager
def dispatcher_writer_execution_terminal_archive_admission_lock(review_run_directory: Path):
    """锁序：archive-attempt → 本许可锁 → run/receipt元数据锁；长finalize在许可锁外。"""
    artifacts_directory = review_run_directory / "artifacts"
    reviewers_directory = artifacts_directory / "reviewers"
    admission_lock_path = reviewers_directory / ".dispatch-thread.lock.launch-metadata.lock"
    if any(path.is_symlink() for path in (review_run_directory, artifacts_directory, reviewers_directory, admission_lock_path)):
        raise ValueError("dispatcher与终态归档的共同许可锁不能经过符号链接")
    reviewers_directory.mkdir(parents=True, exist_ok=True)
    with admission_lock_path.open("a+b") as dispatcher_terminal_archive_admission_owner:
        fcntl.flock(dispatcher_terminal_archive_admission_owner.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(dispatcher_terminal_archive_admission_owner.fileno(), fcntl.LOCK_UN)


def read_bound_terminal_archive_intent_prohibiting_dispatcher_launch(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any]) -> dict[str, Any] | None:
    review_run_directory = Path(dispatcher_status_payload["run_dir"])
    archive_request_path = review_run_directory / "artifacts/review-run-terminal-archive-request.json"
    from review_run_terminal_archive import _read_bound_archive_request, read_accepted_terminal_archive_registration

    if not archive_request_path.exists() and not archive_request_path.is_symlink():
        request = read_accepted_terminal_archive_registration(run_dir=review_run_directory, repo=Path(dispatcher_status_payload["repo"]),
            session_id=str(dispatcher_status_payload.get("session_id") or ""))
        if request is None:
            return None
    else:
        request, _summary = _read_bound_archive_request(archive_request_path, repo=Path(dispatcher_status_payload["repo"]),
            session_id=str(dispatcher_status_payload.get("session_id") or ""))
    if (dispatcher_status_path.resolve() != review_run_directory.resolve() / "artifacts/reviewers/.dispatch-thread.status.json"
            or request["run_id"] != dispatcher_status_payload.get("run_id")):
        raise ValueError("终态归档意图与dispatcher精确归属不符")
    return request


def assert_dispatcher_launch_not_prohibited_by_terminal_archive_intent(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any]) -> None:
    if read_bound_terminal_archive_intent_prohibiting_dispatcher_launch(dispatcher_status_path, dispatcher_status_payload) is not None:
        raise ValueError("精确终态归档意图已成立，禁止该run创建新的dispatcher启动许可")


def _dispatcher_writer_execution_observation_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dispatcher_writer_execution_command_sha256(dispatcher_command_argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(dispatcher_command_argv, ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def current_local_host_boot_identity_for_dispatcher_writer_execution() -> dict[str, str] | None:
    """进程不能跨 OS 重启存活；只缓存可信 OS 主机与 boot 身份，不猜 hostname。"""
    try:
        if sys.platform == "darwin":
            local_host_boot_session_id = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True, text=True, check=True, timeout=3).stdout.strip()
            local_host_hardware_identity_output = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, check=True, timeout=3).stdout
            hardware_uuid_match = re.search(r'"IOPlatformUUID"\s*=\s*"([^\"]+)"', local_host_hardware_identity_output)
            local_host_hardware_identifier = hardware_uuid_match.group(1) if hardware_uuid_match else ""
        elif sys.platform.startswith("linux"):
            local_host_boot_session_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            local_host_hardware_identifier = Path("/etc/machine-id").read_text().strip()
        else:
            return None
        if local_host_boot_session_id and local_host_hardware_identifier:
            return {"host_identity_sha256": hashlib.sha256(local_host_hardware_identifier.encode()).hexdigest(), "boot_session_id": local_host_boot_session_id}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _bound_dispatcher_writer_execution_identity_and_evidence_path(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    review_run_directory = Path(dispatcher_status_payload["run_dir"]).resolve()
    if (dispatcher_status_path.is_symlink() or dispatcher_status_path.resolve() != review_run_directory / "artifacts/reviewers/.dispatch-thread.status.json"
            or dispatcher_status_payload.get("kind") != "dispatch"):
        raise ValueError("dispatcher 状态路径或角色归属不符")
    review_run_summary = read_json_object(review_run_directory / "summary.json")
    if (not isinstance(dispatcher_status_payload.get("run_id"), str) or not dispatcher_status_payload["run_id"]
            or review_run_summary.get("run_id") != dispatcher_status_payload.get("run_id") or not isinstance(review_run_summary.get("repo"), str)
            or not isinstance(dispatcher_status_payload.get("repo"), str)
            or Path(review_run_summary["repo"]).resolve() != Path(dispatcher_status_payload["repo"]).resolve()
            or review_run_summary.get("session_id") and dispatcher_status_payload.get("session_id") != review_run_summary["session_id"]):
        raise ValueError("dispatcher 状态与正式 run、仓库或会话身份不符")
    dispatcher_writer_execution_id = dispatcher_status_payload.get("dispatcher_writer_execution_id")
    if not isinstance(dispatcher_writer_execution_id, str) or not re.fullmatch(r"[0-9a-f]{32}", dispatcher_writer_execution_id):
        raise ValueError("dispatcher 缺少精确实际执行绑定")
    dispatcher_writer_execution_directory = dispatcher_status_path.parent / "dispatcher-writer-executions" / dispatcher_writer_execution_id
    dispatcher_writer_execution_evidence_path = dispatcher_writer_execution_directory / "dispatcher-writer-execution-evidence.json"
    if (dispatcher_writer_execution_directory.is_symlink() or dispatcher_writer_execution_directory.parent.is_symlink() or dispatcher_writer_execution_evidence_path.is_symlink()
            or dispatcher_status_payload.get("dispatcher_writer_execution_evidence_path") != str(dispatcher_writer_execution_evidence_path.resolve())):
        raise ValueError("dispatcher 实际执行证据路径归属不符")
    dispatcher_command_argv = dispatcher_status_payload.get("command")
    if (not isinstance(dispatcher_command_argv, list) or len(dispatcher_command_argv) < 2
            or any(not isinstance(command_argv_token, str) for command_argv_token in dispatcher_command_argv)):
        raise ValueError("dispatcher 缺少精确执行命令")
    dispatcher_entrypoint_realpath = Path(dispatcher_command_argv[1]).resolve()
    dispatcher_writer_execution_identity = {"dispatcher_writer_execution_id": dispatcher_writer_execution_id, "run_id": dispatcher_status_payload["run_id"], "run_dir": str(review_run_directory),
        "repo_realpath": str(Path(dispatcher_status_payload["repo"]).resolve()), "session_id": dispatcher_status_payload.get("session_id"),
        "dispatcher_status_path": str(dispatcher_status_path.resolve()), "tmux_session": dispatcher_status_payload.get("tmux_session"),
        "dispatcher_writer_execution_command_sha256": _dispatcher_writer_execution_command_sha256(dispatcher_command_argv), "dispatcher_writer_execution_entrypoint_realpath": str(dispatcher_entrypoint_realpath),
        "dispatcher_writer_execution_entrypoint_sha256": dispatcher_status_payload.get("dispatcher_writer_execution_entrypoint_sha256")}
    return dispatcher_writer_execution_identity, dispatcher_writer_execution_evidence_path


def _dispatcher_writer_execution_executable_recovery_observation(dispatcher_executable_argv0: str, dispatcher_executable_search_path: str | None) -> dict[str, Any]:
    resolved_dispatcher_executable = (str(Path(dispatcher_executable_argv0).resolve()) if os.sep in dispatcher_executable_argv0
        else shutil.which(dispatcher_executable_argv0, path=dispatcher_executable_search_path if dispatcher_executable_search_path is not None else os.defpath))
    dispatcher_executable_observation = {"requested_argv0": dispatcher_executable_argv0, "resolved_executable": resolved_dispatcher_executable,
        "search_path": dispatcher_executable_search_path if os.sep not in dispatcher_executable_argv0 else None}
    if resolved_dispatcher_executable:
        try:
            dispatcher_executable_stat = Path(resolved_dispatcher_executable).stat()
            dispatcher_executable_observation.update({"permission_bits": dispatcher_executable_stat.st_mode & 0o7777,
                "read_access": os.access(resolved_dispatcher_executable, os.R_OK), "execute_access": os.access(resolved_dispatcher_executable, os.X_OK)})
            try:
                dispatcher_executable_observation["content_sha256"] = hashlib.sha256(Path(resolved_dispatcher_executable).read_bytes()).hexdigest()
            except OSError as dispatcher_executable_read_error:
                dispatcher_executable_observation["read_errno"] = dispatcher_executable_read_error.errno
        except OSError as dispatcher_executable_stat_error:
            dispatcher_executable_observation["stat_errno"] = dispatcher_executable_stat_error.errno
    return dispatcher_executable_observation


def observe_dispatcher_writer_execution_prelaunch_recovery_inputs(dispatcher_command_argv: list[str], dispatcher_total_timeout_seconds: float | None,
    dispatcher_tmux_executable_argv0: str | None = None, dispatcher_tmux_executable_search_path: str | None = None) -> dict[str, Any]:
    """可检测的输入代只限制准备重试；文件可读不证明任何 writer 已退出。"""
    observed_dispatcher_prelaunch_inputs = {"dispatcher_command_sha256": _dispatcher_writer_execution_command_sha256(dispatcher_command_argv),
        "dispatcher_entrypoint_realpath": str(Path(dispatcher_command_argv[1]).resolve()),
        "dispatcher_total_timeout_seconds": float(dispatcher_total_timeout_seconds) if dispatcher_total_timeout_seconds is not None else None}
    dispatcher_prelaunch_recovery_implementation_sources = {}
    for dispatcher_recovery_source_name in ("dispatcher_writer_execution_evidence.py", "rvf_detached_thread.py"):
        try:
            dispatcher_prelaunch_recovery_implementation_sources[dispatcher_recovery_source_name] = {
                "sha256": hashlib.sha256(Path(__file__).with_name(dispatcher_recovery_source_name).read_bytes()).hexdigest()}
        except OSError as dispatcher_recovery_source_read_error:
            dispatcher_prelaunch_recovery_implementation_sources[dispatcher_recovery_source_name] = {"read_errno": dispatcher_recovery_source_read_error.errno}
    observed_dispatcher_prelaunch_inputs["dispatcher_prelaunch_recovery_implementation_sources"] = dispatcher_prelaunch_recovery_implementation_sources
    try:
        observed_dispatcher_prelaunch_inputs["dispatcher_entrypoint_content_sha256"] = hashlib.sha256(Path(dispatcher_command_argv[1]).read_bytes()).hexdigest()
    except OSError as dispatcher_entrypoint_read_error:
        observed_dispatcher_prelaunch_inputs.update({"dispatcher_entrypoint_read_error_kind": type(dispatcher_entrypoint_read_error).__name__,
            "dispatcher_entrypoint_read_error_errno": dispatcher_entrypoint_read_error.errno})
    observed_dispatcher_prelaunch_inputs["dispatcher_selected_python_executable"] = _dispatcher_writer_execution_executable_recovery_observation(
        dispatcher_command_argv[0], dispatcher_tmux_executable_search_path)
    if dispatcher_tmux_executable_argv0 is not None:
        observed_dispatcher_prelaunch_inputs["dispatcher_tmux_executable"] = _dispatcher_writer_execution_executable_recovery_observation(
            dispatcher_tmux_executable_argv0, dispatcher_tmux_executable_search_path)
    return {"dispatcher_writer_execution_prelaunch_recovery_generation_sha256": hashlib.sha256(
                json.dumps(observed_dispatcher_prelaunch_inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "dispatcher_writer_execution_prelaunch_recovery_input_observation": observed_dispatcher_prelaunch_inputs}


def initialize_dispatcher_writer_execution_binding_before_entrypoint_read(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any]) -> dict[str, Any]:
    """在可能失败的入口读取前持久绑定；准备身份不包含实际出生或退出事实。"""
    import secrets

    dispatcher_status_payload = dict(dispatcher_status_payload)
    review_run_summary = read_json_object(Path(dispatcher_status_payload["run_dir"]) / "summary.json")
    if dispatcher_status_payload.get("session_id") is None:
        dispatcher_status_payload["session_id"] = review_run_summary.get("session_id")
    dispatcher_writer_execution_id = secrets.token_hex(16)
    dispatcher_status_payload["dispatcher_writer_execution_id"] = dispatcher_writer_execution_id
    dispatcher_status_payload["dispatcher_writer_execution_evidence_path"] = str(
        (dispatcher_status_path.parent / "dispatcher-writer-executions" / dispatcher_writer_execution_id / "dispatcher-writer-execution-evidence.json").resolve())
    dispatcher_status_payload["dispatcher_writer_execution_entrypoint_sha256"] = None
    dispatcher_status_payload["dispatcher_writer_execution_launch_boundary_status"] = "preparing_before_tmux_attempt"
    if "dispatcher_writer_execution_prelaunch_recovery_generation_sha256" not in dispatcher_status_payload:
        dispatcher_status_payload.update(observe_dispatcher_writer_execution_prelaunch_recovery_inputs(
            dispatcher_status_payload["command"], dispatcher_status_payload.get("total_timeout_seconds"),
            dispatcher_status_payload.get("dispatcher_writer_execution_tmux_executable_argv0"),
            dispatcher_status_payload.get("dispatcher_writer_execution_tmux_executable_search_path")))
    dispatcher_status_payload.setdefault("dispatcher_writer_execution_prelaunch_recovery_attempt_number", 1)
    dispatcher_writer_execution_identity, dispatcher_writer_execution_evidence_path = _bound_dispatcher_writer_execution_identity_and_evidence_path(dispatcher_status_path, dispatcher_status_payload)
    atomic_write_json(dispatcher_writer_execution_evidence_path, {"schema_version": 1, **dispatcher_writer_execution_identity,
        "dispatcher_writer_execution_command_argv": dispatcher_status_payload["command"],
        **{field_name: value for field_name, value in dispatcher_status_payload.items() if field_name.startswith("dispatcher_writer_execution_prelaunch_recovery_")},
        "dispatcher_writer_execution_previous_prelaunch_failure_evidence_path": dispatcher_status_payload.get("dispatcher_writer_execution_previous_prelaunch_failure_evidence_path"),
        "dispatcher_writer_execution_launch_boundary_status": "preparing_before_tmux_attempt",
        "dispatcher_writer_execution_binding_prepared_at": _dispatcher_writer_execution_observation_timestamp(), "actual_dispatcher_execution_status": "prepared"})
    atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
    return dispatcher_status_payload


def complete_dispatcher_writer_execution_entrypoint_content_binding(dispatcher_status_path: Path) -> dict[str, Any]:
    dispatcher_status_payload, dispatcher_writer_execution_observations, dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
    dispatcher_entrypoint_content_sha256 = hashlib.sha256(Path(dispatcher_status_payload["command"][1]).read_bytes()).hexdigest()
    dispatcher_status_payload["dispatcher_writer_execution_entrypoint_sha256"] = dispatcher_entrypoint_content_sha256
    dispatcher_writer_execution_observations["dispatcher_writer_execution_entrypoint_sha256"] = dispatcher_entrypoint_content_sha256
    atomic_write_json(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_observations)
    atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
    return dispatcher_status_payload


def prepare_dispatcher_writer_execution_evidence_binding(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any]) -> dict[str, Any]:
    review_run_directory = Path(dispatcher_status_payload["run_dir"])
    with dispatcher_writer_execution_terminal_archive_admission_lock(review_run_directory):
        dispatcher_status_payload = dict(dispatcher_status_payload)
        if dispatcher_status_payload.get("session_id") is None:
            dispatcher_status_payload["session_id"] = read_json_object(review_run_directory / "summary.json").get("session_id")
        prior_receipts = recover_unattempted_dispatcher_preparations_with_admission_lock_held(dispatcher_status_path, dispatcher_status_payload)
        assert_dispatcher_launch_not_prohibited_by_terminal_archive_intent(dispatcher_status_path, dispatcher_status_payload)
        observed_inputs = observe_dispatcher_writer_execution_prelaunch_recovery_inputs(dispatcher_status_payload["command"], dispatcher_status_payload.get("total_timeout_seconds"),
            dispatcher_status_payload.get("dispatcher_writer_execution_tmux_executable_argv0"), dispatcher_status_payload.get("dispatcher_writer_execution_tmux_executable_search_path"))
        consumed_attempts = sum(receipt["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] == observed_inputs["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] for receipt in prior_receipts)
        if consumed_attempts >= DISPATCHER_WRITER_EXECUTION_PRELAUNCH_RECOVERY_ATTEMPTS_PER_INPUT_GENERATION:
            raise ValueError("dispatcher同一输入代的三次准备预算已耗尽")
        dispatcher_status_payload.update(observed_inputs)
        dispatcher_status_payload["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] = consumed_attempts + 1
        initialize_dispatcher_writer_execution_binding_before_entrypoint_read(dispatcher_status_path, dispatcher_status_payload)
        return complete_dispatcher_writer_execution_entrypoint_content_binding(dispatcher_status_path)


def record_dispatcher_writer_execution_failure_before_tmux_attempt(dispatcher_status_path: Path, dispatcher_writer_execution_id: str, dispatcher_prelaunch_preparation_error: str) -> dict[str, Any]:
    """仅由已退出准备分支持久封存负证明；attempted、monitor/runtime 事实禁止降级。"""
    dispatcher_status_payload, dispatcher_writer_execution_observations, dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
    if (dispatcher_writer_execution_observations["dispatcher_writer_execution_id"] != dispatcher_writer_execution_id
            or dispatcher_writer_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") != "preparing_before_tmux_attempt"
            or any(field_name in dispatcher_writer_execution_observations for field_name in (
                "dispatcher_writer_execution_tmux_attempted_at", "actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at",
                "dispatcher_writer_execution_monitor_pid", "dispatcher_writer_execution_monitor_child_pid", "dispatcher_writer_execution_monitor_command_spawn_failed",
                "dispatcher_writer_execution_tmux_launcher_process_pid", "dispatcher_writer_execution_tmux_launcher_process_started_at"))):
        raise ValueError("已尝试启动或归属不符的 dispatcher 不能登记未启动负证明")
    dispatcher_prelaunch_failure_fields = {"dispatcher_writer_execution_launch_boundary_status": "failed_before_tmux_attempt",
        "dispatcher_writer_execution_prelaunch_failure_recorded_at": _dispatcher_writer_execution_observation_timestamp(),
        "dispatcher_writer_execution_prelaunch_failure_error": dispatcher_prelaunch_preparation_error,
        "dispatcher_writer_execution_prelaunch_recovery_condition": "后续合法派发入口自动重验；同一可观测输入代最多准备3次，预算用尽等待入口或tmux可读性/权限/内容/搜索路径等启动输入变化"}
    _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_observations, dispatcher_prelaunch_failure_fields)
    dispatcher_status_payload.update(dispatcher_prelaunch_failure_fields)
    dispatcher_status_payload.update({"launch_status": "launch_failed", "error": dispatcher_prelaunch_preparation_error})
    atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
    return dispatcher_status_payload


def record_dispatcher_writer_execution_tmux_attempt_boundary(dispatcher_status_path: Path, dispatcher_writer_execution_id: str,
    dispatcher_tmux_command_argv: list[str] | None = None) -> dict[str, Any]:
    review_run_directory = Path(read_json_object(dispatcher_status_path)["run_dir"])
    with dispatcher_writer_execution_terminal_archive_admission_lock(review_run_directory):
        return record_dispatcher_writer_execution_tmux_attempt_with_admission_lock_held(
            dispatcher_status_path, dispatcher_writer_execution_id, dispatcher_tmux_command_argv)


def record_dispatcher_writer_execution_tmux_attempt_with_admission_lock_held(dispatcher_status_path: Path, dispatcher_writer_execution_id: str,
    dispatcher_tmux_command_argv: list[str] | None = None) -> dict[str, Any]:
    """必须先落 attempted 再调用 tmux；落盘后任何失败都保持可能已有 writer。"""
    dispatcher_status_payload, dispatcher_writer_execution_observations, dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
    assert_dispatcher_launch_not_prohibited_by_terminal_archive_intent(dispatcher_status_path, dispatcher_status_payload)
    if (dispatcher_writer_execution_observations["dispatcher_writer_execution_id"] != dispatcher_writer_execution_id
            or dispatcher_writer_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") != "preparing_before_tmux_attempt"):
        raise ValueError("dispatcher 启动边界不再属于本次准备")
    dispatcher_tmux_attempt_fields = {"dispatcher_writer_execution_launch_boundary_status": "tmux_launch_attempted",
        "dispatcher_writer_execution_tmux_attempted_at": _dispatcher_writer_execution_observation_timestamp()}
    if dispatcher_tmux_command_argv is not None:
        dispatcher_tmux_attempt_fields.update({"dispatcher_writer_execution_tmux_launcher_command_sha256": _dispatcher_writer_execution_command_sha256(dispatcher_tmux_command_argv),
            "dispatcher_writer_execution_tmux_launcher_executable_argv0": dispatcher_tmux_command_argv[0]})
    _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_observations, dispatcher_tmux_attempt_fields)
    dispatcher_status_payload.update(dispatcher_tmux_attempt_fields)
    atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
    return dispatcher_status_payload


def _dispatcher_writer_execution_native_exec_rejection_evidence(dispatcher_native_creation_error: OSError, dispatcher_native_command_argv: list[str]) -> dict[str, Any] | None:
    if (isinstance(dispatcher_native_creation_error, (FileNotFoundError, PermissionError))
            and dispatcher_native_creation_error.errno in (errno.ENOENT, errno.EACCES, errno.EPERM)
            and str(dispatcher_native_creation_error.filename) == dispatcher_native_command_argv[0]):
        return {"source": "native_subprocess_popen_exec_failure", "errno": dispatcher_native_creation_error.errno,
            "filename": str(dispatcher_native_creation_error.filename), "exception_kind": type(dispatcher_native_creation_error).__name__,
            "recorded_at": _dispatcher_writer_execution_observation_timestamp(),
            "dispatcher_native_process_creation_command_sha256": _dispatcher_writer_execution_command_sha256(dispatcher_native_command_argv)}
    return None


def _dispatcher_writer_execution_native_exec_rejection_is_bound(dispatcher_native_creation_evidence: Any,
    dispatcher_expected_executable_argv0: str, dispatcher_expected_command_sha256: str) -> bool:
    return (isinstance(dispatcher_native_creation_evidence, dict)
        and dispatcher_native_creation_evidence.get("source") == "native_subprocess_popen_exec_failure"
        and dispatcher_native_creation_evidence.get("errno") in (errno.ENOENT, errno.EACCES, errno.EPERM)
        and dispatcher_native_creation_evidence.get("exception_kind") in ("FileNotFoundError", "PermissionError")
        and bool(dispatcher_native_creation_evidence.get("recorded_at"))
        and isinstance(dispatcher_expected_executable_argv0, str) and bool(dispatcher_expected_executable_argv0)
        and dispatcher_native_creation_evidence.get("filename") == dispatcher_expected_executable_argv0
        and isinstance(dispatcher_expected_command_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", dispatcher_expected_command_sha256) is not None
        and dispatcher_native_creation_evidence.get("dispatcher_native_process_creation_command_sha256") == dispatcher_expected_command_sha256)


def run_dispatcher_tmux_launcher_with_native_process_creation_evidence(dispatcher_status_path: Path, dispatcher_writer_execution_id: str,
    dispatcher_tmux_command_argv: list[str], dispatcher_launch_environment: dict[str, str], dispatcher_tmux_timeout_seconds: float) -> subprocess.CompletedProcess:
    """只有原生 Popen 创建边界的精确 exec 拒绝可补负证明；其余异常保持未知。"""
    dispatcher_status_payload, dispatcher_execution_observations, dispatcher_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
    if (dispatcher_execution_observations["dispatcher_writer_execution_id"] != dispatcher_writer_execution_id
            or dispatcher_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") != "tmux_launch_attempted"
            or dispatcher_execution_observations.get("dispatcher_writer_execution_tmux_launcher_command_sha256") != _dispatcher_writer_execution_command_sha256(dispatcher_tmux_command_argv)):
        raise ValueError("tmux 原生创建边界与 dispatcher 执行身份不符")
    try:
        dispatcher_tmux_launcher_process = subprocess.Popen(dispatcher_tmux_command_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dispatcher_launch_environment)
    except (FileNotFoundError, PermissionError) as dispatcher_tmux_process_creation_error:
        dispatcher_native_creation_evidence = _dispatcher_writer_execution_native_exec_rejection_evidence(dispatcher_tmux_process_creation_error, dispatcher_tmux_command_argv)
        if dispatcher_native_creation_evidence:
            dispatcher_native_creation_failure_fields = {"dispatcher_writer_execution_launch_boundary_status": "failed_during_native_tmux_process_creation",
                "dispatcher_writer_execution_native_tmux_process_creation_failure": dispatcher_native_creation_evidence,
                "dispatcher_writer_execution_prelaunch_failure_error": f"{type(dispatcher_tmux_process_creation_error).__name__}: {dispatcher_tmux_process_creation_error}",
                "dispatcher_writer_execution_prelaunch_failure_recorded_at": _dispatcher_writer_execution_observation_timestamp(),
                "dispatcher_writer_execution_prelaunch_recovery_condition": "后续合法入口自动重验tmux可执行文件/权限/搜索路径；同输入代最多3次，条件变化后开启新一代有界恢复"}
            _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_execution_evidence_path, dispatcher_execution_observations, dispatcher_native_creation_failure_fields)
            dispatcher_status_payload.update(dispatcher_native_creation_failure_fields)
            dispatcher_status_payload.update({"launch_status": "launch_failed", "error": dispatcher_native_creation_failure_fields["dispatcher_writer_execution_prelaunch_failure_error"]})
            atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
        raise
    with dispatcher_tmux_launcher_process:
        _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_execution_evidence_path, dispatcher_execution_observations, {
            "dispatcher_writer_execution_tmux_launcher_process_pid": dispatcher_tmux_launcher_process.pid,
            "dispatcher_writer_execution_tmux_launcher_process_started_at": _dispatcher_writer_execution_observation_timestamp()})
        try:
            dispatcher_tmux_stdout, dispatcher_tmux_stderr = dispatcher_tmux_launcher_process.communicate(timeout=dispatcher_tmux_timeout_seconds)
        except subprocess.TimeoutExpired:
            dispatcher_tmux_launcher_process.kill()
            dispatcher_tmux_launcher_process.communicate()
            raise
    return subprocess.CompletedProcess(dispatcher_tmux_command_argv, dispatcher_tmux_launcher_process.returncode, dispatcher_tmux_stdout, dispatcher_tmux_stderr)


def _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    dispatcher_status_payload = read_json_object(dispatcher_status_path)
    dispatcher_writer_execution_identity, dispatcher_writer_execution_evidence_path = _bound_dispatcher_writer_execution_identity_and_evidence_path(dispatcher_status_path, dispatcher_status_payload)
    dispatcher_writer_execution_observations = read_json_object(dispatcher_writer_execution_evidence_path)
    if not dispatcher_writer_execution_observations or any(dispatcher_writer_execution_observations.get(dispatcher_writer_execution_identity_field_name) != dispatcher_writer_execution_identity.get(dispatcher_writer_execution_identity_field_name) for dispatcher_writer_execution_identity_field_name in DISPATCHER_WRITER_EXECUTION_IDENTITY_FIELD_NAMES):
        raise ValueError("dispatcher 实际执行回执与原绑定不符")
    return dispatcher_status_payload, dispatcher_writer_execution_observations, dispatcher_writer_execution_evidence_path


def recover_unattempted_dispatcher_preparations_with_admission_lock_held(dispatcher_status_path: Path,
    expected_dispatcher_status_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """主动封存未提交许可的旧代；不证明launcher死亡，不读取后来入口内容修补历史。"""
    current_dispatcher_status = read_json_object(dispatcher_status_path)
    execution_receipts_directory = dispatcher_status_path.parent / "dispatcher-writer-executions"
    if not execution_receipts_directory.exists():
        return []
    if dispatcher_status_path.is_symlink() or execution_receipts_directory.is_symlink():
        raise ValueError("dispatcher准备恢复不能经过符号链接")
    expected_dispatcher_status = dict(expected_dispatcher_status_payload or current_dispatcher_status)
    receipt_paths = []
    for execution_directory in execution_receipts_directory.iterdir():
        if not execution_directory.is_dir() and not execution_directory.is_symlink():
            continue
        if execution_directory.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", execution_directory.name):
            raise ValueError("dispatcher准备恢复发现归属不明的执行目录")
        receipt_path = execution_directory / "dispatcher-writer-execution-evidence.json"
        if receipt_path.is_symlink():
            raise ValueError("dispatcher准备回执不能是符号链接")
        receipt_paths.append(receipt_path)
    if not receipt_paths:
        return []
    if not expected_dispatcher_status:
        first_receipt = read_json_object(receipt_paths[0])
        review_run_directory = dispatcher_status_path.parent.parent.parent
        summary = read_json_object(review_run_directory / "summary.json")
        expected_dispatcher_status = {"kind": "dispatch", "run_id": summary.get("run_id"), "run_dir": str(review_run_directory),
            "repo": summary.get("repo"), "session_id": summary.get("session_id"), "tmux_session": first_receipt.get("tmux_session"),
            "command": first_receipt.get("dispatcher_writer_execution_command_argv")}
    validated_dispatcher_receipts = []
    for receipt_path in sorted(receipt_paths):
        receipt = read_json_object(receipt_path)
        receipt_status_binding = {**expected_dispatcher_status, "dispatcher_writer_execution_id": receipt_path.parent.name,
            "dispatcher_writer_execution_evidence_path": str(receipt_path.resolve()),
            "dispatcher_writer_execution_entrypoint_sha256": receipt.get("dispatcher_writer_execution_entrypoint_sha256")}
        identity, expected_path = _bound_dispatcher_writer_execution_identity_and_evidence_path(dispatcher_status_path, receipt_status_binding)
        if expected_path != receipt_path or any(receipt.get(field) != identity.get(field) for field in DISPATCHER_WRITER_EXECUTION_IDENTITY_FIELD_NAMES):
            raise ValueError("dispatcher历史准备回执的精确身份不符")
        if not isinstance(receipt.get("dispatcher_writer_execution_prelaunch_recovery_generation_sha256"), str):
            raise ValueError("dispatcher历史准备缺少可计数的输入代次")
        if current_dispatcher_status.get("dispatcher_writer_execution_id") == receipt["dispatcher_writer_execution_id"]:
            current_identity, _current_path = _bound_dispatcher_writer_execution_identity_and_evidence_path(dispatcher_status_path, current_dispatcher_status)
            if any(current_identity.get(field) != identity.get(field) for field in DISPATCHER_WRITER_EXECUTION_IDENTITY_FIELD_NAMES
                   if field != "dispatcher_writer_execution_entrypoint_sha256"):
                raise ValueError("dispatcher状态和回执的关键归属不一致")
            if current_identity.get("dispatcher_writer_execution_entrypoint_sha256") != identity.get("dispatcher_writer_execution_entrypoint_sha256") and not (
                    current_identity.get("dispatcher_writer_execution_entrypoint_sha256") is None
                    and isinstance(identity.get("dispatcher_writer_execution_entrypoint_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", identity["dispatcher_writer_execution_entrypoint_sha256"])
                    and (receipt.get("dispatcher_writer_execution_launch_boundary_status") == "preparing_before_tmux_attempt"
                        or receipt.get("dispatcher_writer_execution_launch_boundary_status") == "failed_before_tmux_attempt"
                        and isinstance(receipt.get("dispatcher_writer_execution_unattempted_recovery_fence"), dict)
                        and receipt["dispatcher_writer_execution_unattempted_recovery_fence"].get("source") == "shared_dispatcher_terminal_archive_admission_lock"
                        and receipt["dispatcher_writer_execution_unattempted_recovery_fence"].get("prior_status_entrypoint_sha256") is None
                        and receipt["dispatcher_writer_execution_unattempted_recovery_fence"].get("recorded_receipt_entrypoint_sha256") == identity.get("dispatcher_writer_execution_entrypoint_sha256"))):
                raise ValueError("dispatcher内容身份不是可核验的准备半提交")
        validated_dispatcher_receipts.append((receipt_path, identity, receipt))
    # 先验证全部归属再写入，不能以坏回执为由覆盖另一执行的历史。
    recovered_receipts = []
    for receipt_path, identity, receipt in validated_dispatcher_receipts:
        preparing = receipt.get("dispatcher_writer_execution_launch_boundary_status") == "preparing_before_tmux_attempt"
        if preparing:
            if (receipt.get("actual_dispatcher_execution_status") != "prepared"
                    or any(field in receipt for field in DISPATCHER_WRITER_EXECUTION_START_OR_ATTEMPT_FACT_FIELD_NAMES)):
                raise ValueError("已有启动事实的dispatcher不能封存为未启动")
            fence = {"source": "shared_dispatcher_terminal_archive_admission_lock", "recorded_at": _dispatcher_writer_execution_observation_timestamp(),
                "recorded_receipt_entrypoint_sha256": receipt.get("dispatcher_writer_execution_entrypoint_sha256"),
                "prior_status_entrypoint_sha256": current_dispatcher_status.get("dispatcher_writer_execution_entrypoint_sha256")
                    if current_dispatcher_status.get("dispatcher_writer_execution_id") == receipt["dispatcher_writer_execution_id"] else None}
            receipt = _merge_bound_dispatcher_writer_execution_observation_fields(receipt_path, identity, {
                "dispatcher_writer_execution_launch_boundary_status": "failed_before_tmux_attempt",
                "dispatcher_writer_execution_prelaunch_failure_recorded_at": _dispatcher_writer_execution_observation_timestamp(),
                "dispatcher_writer_execution_prelaunch_failure_error": "共同许可锁持有者已撤销尚未提交attempted的准备代，原执行ID禁止启动",
                "dispatcher_writer_execution_prelaunch_recovery_condition": "后续合法入口按全部精确回执计数有界重试；终态归档意图优先禁止新启动",
                "dispatcher_writer_execution_unattempted_recovery_fence": fence})
        if receipt["dispatcher_writer_execution_id"] != current_dispatcher_status.get("dispatcher_writer_execution_id"):
            sealed_unattempted = (receipt.get("dispatcher_writer_execution_launch_boundary_status") == "failed_before_tmux_attempt"
                and not any(field in receipt for field in DISPATCHER_WRITER_EXECUTION_START_OR_ATTEMPT_FACT_FIELD_NAMES))
            delivered = (type(receipt.get("actual_dispatcher_runtime_pid")) is int and receipt["actual_dispatcher_runtime_pid"] > 1 and receipt.get("actual_dispatcher_execution_started_at")
                and receipt.get("actual_dispatcher_execution_status") == "delivery_completed"
                and type(receipt.get("actual_dispatcher_writer_delivery_returncode")) is int and receipt.get("actual_dispatcher_writer_delivery_completed_at"))
            native_tmux_rejected = (receipt.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_tmux_process_creation"
                and _dispatcher_writer_execution_native_exec_rejection_is_bound(receipt.get("dispatcher_writer_execution_native_tmux_process_creation_failure"),
                    receipt.get("dispatcher_writer_execution_tmux_launcher_executable_argv0"), receipt.get("dispatcher_writer_execution_tmux_launcher_command_sha256"))
                and not any(field in receipt for field in DISPATCHER_WRITER_EXECUTION_START_OR_ATTEMPT_FACT_FIELD_NAMES if field != "dispatcher_writer_execution_tmux_attempted_at"))
            native_child_rejected = (receipt.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_monitor_child_process_creation"
                and receipt.get("dispatcher_writer_execution_monitor_command_spawn_failed") is True
                and _dispatcher_writer_execution_native_exec_rejection_is_bound(receipt.get("dispatcher_writer_execution_native_monitor_child_process_creation_failure"),
                    expected_dispatcher_status["command"][0], receipt["dispatcher_writer_execution_command_sha256"])
                and not any(field in receipt for field in ("actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at", "dispatcher_writer_execution_monitor_child_pid")))
            actual_execution_prior_boot_ended = False
            if type(receipt.get("actual_dispatcher_runtime_pid")) is int and receipt["actual_dispatcher_runtime_pid"] > 1 and receipt.get("actual_dispatcher_execution_started_at"):
                recorded_boot = receipt.get("actual_dispatcher_host_boot")
                current_boot = current_local_host_boot_identity_for_dispatcher_writer_execution()
                actual_execution_prior_boot_ended = (isinstance(recorded_boot, dict) and current_boot
                    and recorded_boot.get("host_identity_sha256") == current_boot["host_identity_sha256"]
                    and recorded_boot.get("boot_session_id") and recorded_boot["boot_session_id"] != current_boot["boot_session_id"])
            if not sealed_unattempted and not delivered and not native_tmux_rejected and not native_child_rejected and not actual_execution_prior_boot_ended:
                raise ValueError("非当前dispatcher执行仍缺少精确交付证据，保留unknown")
        fenced_current_preparation = (receipt.get("dispatcher_writer_execution_launch_boundary_status") == "failed_before_tmux_attempt"
            and isinstance(receipt.get("dispatcher_writer_execution_unattempted_recovery_fence"), dict)
            and receipt["dispatcher_writer_execution_unattempted_recovery_fence"].get("source") == "shared_dispatcher_terminal_archive_admission_lock"
            and not any(field in receipt for field in DISPATCHER_WRITER_EXECUTION_START_OR_ATTEMPT_FACT_FIELD_NAMES))
        current_binding_requires_fence_repair = fenced_current_preparation and (
            current_dispatcher_status.get("dispatcher_writer_execution_launch_boundary_status") != "failed_before_tmux_attempt"
            or current_dispatcher_status.get("dispatcher_writer_execution_entrypoint_sha256") != receipt.get("dispatcher_writer_execution_entrypoint_sha256"))
        if (preparing or current_binding_requires_fence_repair) and receipt["dispatcher_writer_execution_id"] == current_dispatcher_status.get("dispatcher_writer_execution_id"):
            current_dispatcher_status.update({field: value for field, value in receipt.items() if field.startswith("dispatcher_writer_execution_prelaunch_")
                or field in ("dispatcher_writer_execution_entrypoint_sha256", "dispatcher_writer_execution_launch_boundary_status", "dispatcher_writer_execution_unattempted_recovery_fence")})
            current_dispatcher_status.update({"launch_status": "launch_failed", "error": receipt["dispatcher_writer_execution_prelaunch_failure_error"]})
            atomic_write_json(dispatcher_status_path, current_dispatcher_status)
        recovered_receipts.append(receipt)
    if not current_dispatcher_status.get("dispatcher_writer_execution_id") and recovered_receipts:
        receipt = max(recovered_receipts, key=lambda value: (value.get("dispatcher_writer_execution_binding_prepared_at", ""), value["dispatcher_writer_execution_id"]))
        recovered_status = {**expected_dispatcher_status, "dispatcher_writer_execution_id": receipt["dispatcher_writer_execution_id"],
            "dispatcher_writer_execution_evidence_path": str(execution_receipts_directory / receipt["dispatcher_writer_execution_id"] / "dispatcher-writer-execution-evidence.json"),
            "dispatcher_writer_execution_entrypoint_sha256": receipt.get("dispatcher_writer_execution_entrypoint_sha256"),
            **{field: value for field, value in receipt.items() if field.startswith("dispatcher_writer_execution_prelaunch_")},
            "dispatcher_writer_execution_launch_boundary_status": receipt["dispatcher_writer_execution_launch_boundary_status"],
            "launch_status": "launch_failed", "error": receipt.get("dispatcher_writer_execution_prelaunch_failure_error"), "returncode": None, "finished_at": None}
        atomic_write_json(dispatcher_status_path, recovered_status)
    return recovered_receipts


def _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path: Path, dispatcher_writer_execution_identity: dict[str, Any], dispatcher_writer_execution_observation_updates: dict[str, Any]) -> dict[str, Any]:
    with dispatcher_writer_execution_evidence_path.with_name(".dispatcher-writer-execution-evidence-metadata.lock").open("a+b") as dispatcher_execution_metadata_lock_owner:
        fcntl.flock(dispatcher_execution_metadata_lock_owner.fileno(), fcntl.LOCK_EX)
        latest_dispatcher_writer_execution_observations = read_json_object(dispatcher_writer_execution_evidence_path)
        if dispatcher_writer_execution_evidence_path.is_symlink() or any(latest_dispatcher_writer_execution_observations.get(dispatcher_writer_execution_identity_field_name) != dispatcher_writer_execution_identity.get(dispatcher_writer_execution_identity_field_name) for dispatcher_writer_execution_identity_field_name in DISPATCHER_WRITER_EXECUTION_IDENTITY_FIELD_NAMES):
            raise ValueError("dispatcher 执行回执的已绑定身份发生变化")
        latest_dispatcher_writer_execution_observations.update(dispatcher_writer_execution_observation_updates)
        atomic_write_json(dispatcher_writer_execution_evidence_path, latest_dispatcher_writer_execution_observations)
        return latest_dispatcher_writer_execution_observations


def observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(dispatcher_status_path: Path) -> dict[str, Any]:
    """只读补证：封存的未尝试负证明、实际交付或同机实际执行 boot 换代。"""
    unknown_dispatcher_writer_execution_observation = {"status": "unknown", "evidence_kind": "actual_dispatcher_runtime", "dispatcher_writers_may_still_write": True,
        "process_exit_status": "unknown", "recovery_condition":
        "精确实际 dispatcher 交付回执恢复可读，或同机实际执行 boot 换代后自动重验；tmux/launcher 消失不构成退出证明"}
    try:
        dispatcher_status_payload, dispatcher_writer_execution_observations, dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
        unknown_dispatcher_writer_execution_observation.update({"dispatcher_writer_execution_id": dispatcher_writer_execution_observations["dispatcher_writer_execution_id"], "dispatcher_writer_execution_evidence_path": str(dispatcher_writer_execution_evidence_path)})
        if (dispatcher_status_payload.get("launch_status") == "launch_failed"
                and dispatcher_status_payload.get("dispatcher_writer_execution_launch_boundary_status") == "failed_before_tmux_attempt"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") == "failed_before_tmux_attempt"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_prelaunch_failure_recorded_at")
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_prelaunch_failure_error")
                and not any(field_name in dispatcher_writer_execution_observations for field_name in (
                    "dispatcher_writer_execution_tmux_attempted_at", "actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at",
                    "dispatcher_writer_execution_monitor_pid", "dispatcher_writer_execution_monitor_child_pid", "dispatcher_writer_execution_monitor_command_spawn_failed",
                    "dispatcher_writer_execution_tmux_launcher_process_pid", "dispatcher_writer_execution_tmux_launcher_process_started_at"))):
            return {**unknown_dispatcher_writer_execution_observation, "status": "not_started", "evidence_kind": "dispatcher_launch_not_attempted",
                "dispatcher_writers_may_still_write": False, "process_exit_status": "not_started", "recovery_condition": None,
                **{field_name: value for field_name, value in dispatcher_writer_execution_observations.items() if field_name.startswith("dispatcher_writer_execution_prelaunch_")},
                "dispatcher_writer_execution_evidence_sha256": hashlib.sha256(dispatcher_writer_execution_evidence_path.read_bytes()).hexdigest()}
        dispatcher_native_creation_failure = dispatcher_writer_execution_observations.get("dispatcher_writer_execution_native_tmux_process_creation_failure")
        if (dispatcher_status_payload.get("launch_status") == "launch_failed"
                and dispatcher_status_payload.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_tmux_process_creation"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_tmux_process_creation"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_tmux_attempted_at")
                and _dispatcher_writer_execution_native_exec_rejection_is_bound(dispatcher_native_creation_failure,
                    dispatcher_writer_execution_observations.get("dispatcher_writer_execution_tmux_launcher_executable_argv0"),
                    dispatcher_writer_execution_observations.get("dispatcher_writer_execution_tmux_launcher_command_sha256"))
                and dispatcher_status_payload.get("dispatcher_writer_execution_tmux_launcher_executable_argv0") == dispatcher_writer_execution_observations.get("dispatcher_writer_execution_tmux_launcher_executable_argv0")
                and dispatcher_status_payload.get("dispatcher_writer_execution_tmux_launcher_command_sha256") == dispatcher_writer_execution_observations.get("dispatcher_writer_execution_tmux_launcher_command_sha256")
                and not any(field_name in dispatcher_writer_execution_observations for field_name in (
                    "actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at", "dispatcher_writer_execution_monitor_pid",
                    "dispatcher_writer_execution_monitor_child_pid", "dispatcher_writer_execution_monitor_command_spawn_failed",
                    "dispatcher_writer_execution_tmux_launcher_process_pid", "dispatcher_writer_execution_tmux_launcher_process_started_at"))):
            return {**unknown_dispatcher_writer_execution_observation, "status": "not_started", "evidence_kind": "dispatcher_tmux_process_creation_failed",
                "dispatcher_writers_may_still_write": False, "process_exit_status": "not_started", "recovery_condition": None,
                **{field_name: value for field_name, value in dispatcher_writer_execution_observations.items() if field_name.startswith("dispatcher_writer_execution_prelaunch_")},
                "dispatcher_writer_execution_native_tmux_process_creation_failure": dispatcher_native_creation_failure,
                "dispatcher_writer_execution_evidence_sha256": hashlib.sha256(dispatcher_writer_execution_evidence_path.read_bytes()).hexdigest()}
        dispatcher_monitor_child_creation_failure = dispatcher_writer_execution_observations.get("dispatcher_writer_execution_native_monitor_child_process_creation_failure")
        if (dispatcher_status_payload.get("launch_status") == "launch_failed"
                and dispatcher_status_payload.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_monitor_child_process_creation"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_launch_boundary_status") == "failed_during_native_monitor_child_process_creation"
                and dispatcher_writer_execution_observations.get("dispatcher_writer_execution_monitor_command_spawn_failed") is True
                and _dispatcher_writer_execution_native_exec_rejection_is_bound(dispatcher_monitor_child_creation_failure,
                    dispatcher_status_payload["command"][0], dispatcher_writer_execution_observations["dispatcher_writer_execution_command_sha256"])
                and not any(field_name in dispatcher_writer_execution_observations for field_name in (
                    "actual_dispatcher_runtime_pid", "actual_dispatcher_execution_started_at", "dispatcher_writer_execution_monitor_child_pid"))):
            return {**unknown_dispatcher_writer_execution_observation, "status": "not_started", "evidence_kind": "dispatcher_monitor_child_process_creation_failed",
                "dispatcher_writers_may_still_write": False, "process_exit_status": "not_started", "recovery_condition": None,
                **{field_name: value for field_name, value in dispatcher_writer_execution_observations.items() if field_name.startswith("dispatcher_writer_execution_prelaunch_")},
                "dispatcher_writer_execution_native_monitor_child_process_creation_failure": dispatcher_monitor_child_creation_failure,
                "dispatcher_writer_execution_evidence_sha256": hashlib.sha256(dispatcher_writer_execution_evidence_path.read_bytes()).hexdigest()}
        actual_dispatcher_runtime_pid = dispatcher_writer_execution_observations.get("actual_dispatcher_runtime_pid")
        actual_dispatcher_started_at = dispatcher_writer_execution_observations.get("actual_dispatcher_execution_started_at")
        if type(actual_dispatcher_runtime_pid) is not int or actual_dispatcher_runtime_pid <= 1 or not actual_dispatcher_started_at:
            return unknown_dispatcher_writer_execution_observation
        if (dispatcher_writer_execution_observations.get("actual_dispatcher_execution_status") == "delivery_completed"
                and type(dispatcher_writer_execution_observations.get("actual_dispatcher_writer_delivery_returncode")) is int
                and dispatcher_writer_execution_observations.get("actual_dispatcher_writer_delivery_completed_at")):
            return {**unknown_dispatcher_writer_execution_observation, "status": "delivered", "evidence_kind": "actual_dispatcher_delivery",
                "dispatcher_writers_may_still_write": False, "actual_dispatcher_writer_delivery_returncode": dispatcher_writer_execution_observations["actual_dispatcher_writer_delivery_returncode"],
                "dispatcher_writer_execution_evidence_sha256": hashlib.sha256(dispatcher_writer_execution_evidence_path.read_bytes()).hexdigest(), "recovery_condition": None}
        recorded_actual_dispatcher_host_boot_identity = dispatcher_writer_execution_observations.get("actual_dispatcher_host_boot")
        observed_local_host_boot_identity = current_local_host_boot_identity_for_dispatcher_writer_execution()
        if (isinstance(recorded_actual_dispatcher_host_boot_identity, dict) and observed_local_host_boot_identity and recorded_actual_dispatcher_host_boot_identity.get("host_identity_sha256")
                == observed_local_host_boot_identity["host_identity_sha256"] and recorded_actual_dispatcher_host_boot_identity.get("boot_session_id")
                and recorded_actual_dispatcher_host_boot_identity["boot_session_id"] != observed_local_host_boot_identity["boot_session_id"]):
            return {**unknown_dispatcher_writer_execution_observation, "status": "exited_before_current_boot", "evidence_kind": "actual_dispatcher_boot_changed",
                "dispatcher_writers_may_still_write": False, "process_exit_status": "prior_boot_ended",
                "recorded_actual_dispatcher_host_boot_identity": recorded_actual_dispatcher_host_boot_identity, "current_observed_local_host_boot_identity": observed_local_host_boot_identity, "recovery_condition": None}
    except (OSError, ValueError, KeyError, TypeError) as dispatcher_writer_execution_observation_error:
        unknown_dispatcher_writer_execution_observation["error"] = f"{type(dispatcher_writer_execution_observation_error).__name__}: {dispatcher_writer_execution_observation_error}"
    return unknown_dispatcher_writer_execution_observation


def run_actual_dispatcher_with_writer_execution_evidence(
    actual_dispatcher_main: Callable[[], int], *, dispatcher_script_path: Path,
) -> int:
    """由实际 dispatcher 入口调用；main 正常交付后才写 quiet，异常/强杀保持未知。"""
    dispatcher_writer_execution_id, raw_dispatcher_writer_execution_evidence_path = os.environ.get(DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE), os.environ.get(DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE)
    if not dispatcher_writer_execution_id and not raw_dispatcher_writer_execution_evidence_path:
        return actual_dispatcher_main()
    if not dispatcher_writer_execution_id or not raw_dispatcher_writer_execution_evidence_path:
        raise ValueError("dispatcher 实际执行绑定字段不完整")
    dispatcher_writer_execution_evidence_path = Path(raw_dispatcher_writer_execution_evidence_path)
    planned_dispatcher_writer_execution_identity = read_json_object(dispatcher_writer_execution_evidence_path)
    dispatcher_status_payload, dispatcher_writer_execution_identity, bound_dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(Path(planned_dispatcher_writer_execution_identity["dispatcher_status_path"]))
    if (dispatcher_writer_execution_evidence_path.resolve() != bound_dispatcher_writer_execution_evidence_path or dispatcher_writer_execution_identity["dispatcher_writer_execution_id"] != dispatcher_writer_execution_id
            or Path(dispatcher_writer_execution_identity["dispatcher_writer_execution_entrypoint_realpath"]) != dispatcher_script_path.resolve()
            or hashlib.sha256(dispatcher_script_path.read_bytes()).hexdigest() != dispatcher_writer_execution_identity["dispatcher_writer_execution_entrypoint_sha256"]
            or dispatcher_status_payload["command"][2:] != sys.argv[1:]):
        raise ValueError("实际 dispatcher 的入口、argv 或执行身份与原绑定不符")
    if dispatcher_writer_execution_identity.get("dispatcher_writer_execution_launch_boundary_status") in ("failed_before_tmux_attempt", "failed_during_native_tmux_process_creation", "failed_during_native_monitor_child_process_creation"):
        raise ValueError("已封存未启动的 dispatcher 执行身份禁止事后运行")
    if dispatcher_writer_execution_identity.get("dispatcher_writer_execution_launch_boundary_status") != "tmux_launch_attempted":
        raise ValueError("实际dispatcher缺少共同许可协议提交的attempted边界")
    with dispatcher_writer_execution_evidence_path.with_name(".actual-dispatcher-writer-execution-runtime-owner.lock").open("a+b") as dispatcher_execution_metadata_lock_owner:
        fcntl.flock(dispatcher_execution_metadata_lock_owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _unused_dispatcher_status_payload, dispatcher_writer_execution_identity, _unused_dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(Path(planned_dispatcher_writer_execution_identity["dispatcher_status_path"]))
        if (dispatcher_writer_execution_identity.get("actual_dispatcher_execution_started_at")
                or dispatcher_writer_execution_identity.get("dispatcher_writer_execution_launch_boundary_status") in ("failed_before_tmux_attempt", "failed_during_native_tmux_process_creation", "failed_during_native_monitor_child_process_creation")):
            raise ValueError("同一 dispatcher 执行身份不能再次开始")
        dispatcher_writer_execution_identity = _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"actual_dispatcher_runtime_pid": os.getpid(),
            "actual_dispatcher_execution_started_at": _dispatcher_writer_execution_observation_timestamp(), "actual_dispatcher_host_boot": current_local_host_boot_identity_for_dispatcher_writer_execution(),
            "actual_dispatcher_execution_status": "running"})
        try:
            actual_dispatcher_main_returncode = actual_dispatcher_main()
        except BaseException as dispatcher_writer_execution_observation_error:
            try:
                _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"actual_dispatcher_execution_status": "interrupted",
                    "actual_dispatcher_execution_error_kind": type(dispatcher_writer_execution_observation_error).__name__, "actual_dispatcher_execution_interruption_observed_at": _dispatcher_writer_execution_observation_timestamp()})
            except (OSError, ValueError):
                pass
            raise
        _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"actual_dispatcher_execution_status": "delivery_completed",
            "actual_dispatcher_writer_delivery_returncode": int(actual_dispatcher_main_returncode), "actual_dispatcher_writer_delivery_completed_at": _dispatcher_writer_execution_observation_timestamp()})
        return actual_dispatcher_main_returncode


def build_dispatcher_writer_execution_monitor_argv(dispatcher_status_path: Path, dispatcher_status_payload: dict[str, Any], dispatcher_monitor_timeout_seconds: float) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--dispatcher-status-path", str(dispatcher_status_path),
            "--dispatcher-writer-execution-id", dispatcher_status_payload["dispatcher_writer_execution_id"], "--dispatcher-monitor-timeout-seconds", str(float(dispatcher_monitor_timeout_seconds)),
            "--", *dispatcher_status_payload["command"]]


def monitor_bound_dispatcher_writer_execution_until_delivery(dispatcher_status_path: Path, dispatcher_writer_execution_id: str, dispatcher_monitor_timeout_seconds: float, dispatcher_command_argv: list[str]) -> int:
    """monitor 可存活于 tmux HUP；只等实际 runtime 交付，不把 forwarder 退出当完成。"""
    from rvf_detached_thread import _terminate_process_group

    dispatcher_status_payload, dispatcher_writer_execution_identity, dispatcher_writer_execution_evidence_path = _read_bound_dispatcher_writer_execution_evidence(dispatcher_status_path)
    if dispatcher_writer_execution_identity["dispatcher_writer_execution_id"] != dispatcher_writer_execution_id or dispatcher_status_payload["command"] != dispatcher_command_argv or dispatcher_monitor_timeout_seconds <= 0:
        raise ValueError("dispatcher monitor 的实际命令或执行绑定不符")
    if dispatcher_writer_execution_identity.get("dispatcher_writer_execution_launch_boundary_status") in ("failed_before_tmux_attempt", "failed_during_native_tmux_process_creation", "failed_during_native_monitor_child_process_creation"):
        raise ValueError("已封存未启动的 dispatcher 身份禁止启动 monitor")
    if dispatcher_writer_execution_identity.get("dispatcher_writer_execution_launch_boundary_status") != "tmux_launch_attempted":
        raise ValueError("dispatcher monitor缺少共同许可协议提交的attempted边界")
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    actual_dispatcher_command_environment = {**os.environ, DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE: dispatcher_writer_execution_id, DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE: str(dispatcher_writer_execution_evidence_path)}
    dispatcher_monitor_deadline_monotonic = time.monotonic() + dispatcher_monitor_timeout_seconds
    try:
        dispatcher_command_monitor_child_process = subprocess.Popen(dispatcher_command_argv, env=actual_dispatcher_command_environment, start_new_session=True)
    except OSError as dispatcher_writer_execution_observation_error:
        dispatcher_monitor_spawn_failure_fields = {"dispatcher_writer_execution_monitor_error": f"{type(dispatcher_writer_execution_observation_error).__name__}: {dispatcher_writer_execution_observation_error}",
            "dispatcher_writer_execution_monitor_command_spawn_failed": True}
        dispatcher_monitor_native_creation_evidence = _dispatcher_writer_execution_native_exec_rejection_evidence(dispatcher_writer_execution_observation_error, dispatcher_command_argv)
        if dispatcher_monitor_native_creation_evidence:
            dispatcher_monitor_spawn_failure_fields.update({"dispatcher_writer_execution_launch_boundary_status": "failed_during_native_monitor_child_process_creation",
                "dispatcher_writer_execution_monitor_pid": os.getpid(), "dispatcher_writer_execution_monitor_started_at": _dispatcher_writer_execution_observation_timestamp(),
                "dispatcher_writer_execution_native_monitor_child_process_creation_failure": dispatcher_monitor_native_creation_evidence,
                "dispatcher_writer_execution_prelaunch_failure_error": dispatcher_monitor_spawn_failure_fields["dispatcher_writer_execution_monitor_error"],
                "dispatcher_writer_execution_prelaunch_failure_recorded_at": _dispatcher_writer_execution_observation_timestamp(),
                "dispatcher_writer_execution_prelaunch_recovery_condition": "所选Python可执行文件/权限/搜索路径恢复且原tmux外壳结束后自动重验；同输入代最多3次，真实条件或恢复实现内容变化开启新代"})
        _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, dispatcher_monitor_spawn_failure_fields)
        if dispatcher_monitor_native_creation_evidence:
            dispatcher_status_payload.update(dispatcher_monitor_spawn_failure_fields)
            dispatcher_status_payload.update({"launch_status": "launch_failed", "error": dispatcher_monitor_spawn_failure_fields["dispatcher_writer_execution_monitor_error"]})
            atomic_write_json(dispatcher_status_path, dispatcher_status_payload)
        return 127
    _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"dispatcher_writer_execution_monitor_pid": os.getpid(), "dispatcher_writer_execution_monitor_child_pid": dispatcher_command_monitor_child_process.pid,
        "dispatcher_writer_execution_monitor_started_at": _dispatcher_writer_execution_observation_timestamp()})
    persisted_monitor_child_returncode = None
    while time.monotonic() < dispatcher_monitor_deadline_monotonic:
        observed_actual_dispatcher_writer_execution_evidence = observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(dispatcher_status_path)
        observed_monitor_child_returncode = dispatcher_command_monitor_child_process.poll()
        if observed_monitor_child_returncode is not None and observed_monitor_child_returncode != persisted_monitor_child_returncode:
            _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"dispatcher_writer_execution_monitor_child_returncode": observed_monitor_child_returncode})
            persisted_monitor_child_returncode = observed_monitor_child_returncode
        if (observed_actual_dispatcher_writer_execution_evidence.get("dispatcher_writer_execution_id") == dispatcher_writer_execution_id and not observed_actual_dispatcher_writer_execution_evidence["dispatcher_writers_may_still_write"]
                and observed_actual_dispatcher_writer_execution_evidence.get("evidence_kind") == "actual_dispatcher_delivery"):
            _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"dispatcher_writer_execution_monitor_child_returncode": observed_monitor_child_returncode,
                                                   "dispatcher_writer_execution_monitor_delivery_observed_at": _dispatcher_writer_execution_observation_timestamp()})
            return int(observed_actual_dispatcher_writer_execution_evidence["actual_dispatcher_writer_delivery_returncode"])
        time.sleep(0.2)
    if dispatcher_command_monitor_child_process.poll() is None:
        _terminate_process_group(dispatcher_command_monitor_child_process)
    _merge_bound_dispatcher_writer_execution_observation_fields(dispatcher_writer_execution_evidence_path, dispatcher_writer_execution_identity, {"dispatcher_writer_execution_monitor_timeout_observed_at": _dispatcher_writer_execution_observation_timestamp(),
        "dispatcher_writer_execution_monitor_child_returncode": dispatcher_command_monitor_child_process.poll()})
    return 124


def dispatcher_writer_execution_monitor_cli_main() -> int:
    dispatcher_execution_monitor_argument_parser = argparse.ArgumentParser(description="监督已绑定的实际 dispatcher 交付，不把外壳退出当 writer 退出。")
    dispatcher_execution_monitor_argument_parser.add_argument("--dispatcher-status-path", type=Path, required=True)
    dispatcher_execution_monitor_argument_parser.add_argument("--dispatcher-writer-execution-id", required=True)
    dispatcher_execution_monitor_argument_parser.add_argument("--dispatcher-monitor-timeout-seconds", type=float, required=True)
    dispatcher_execution_monitor_argument_parser.add_argument("dispatcher_command_argv", nargs=argparse.REMAINDER)
    dispatcher_execution_monitor_arguments = dispatcher_execution_monitor_argument_parser.parse_args()
    dispatcher_command_argv = dispatcher_execution_monitor_arguments.dispatcher_command_argv[1:] if dispatcher_execution_monitor_arguments.dispatcher_command_argv[:1] == ["--"] else dispatcher_execution_monitor_arguments.dispatcher_command_argv
    return monitor_bound_dispatcher_writer_execution_until_delivery(dispatcher_execution_monitor_arguments.dispatcher_status_path, dispatcher_execution_monitor_arguments.dispatcher_writer_execution_id, dispatcher_execution_monitor_arguments.dispatcher_monitor_timeout_seconds, dispatcher_command_argv)


if __name__ == "__main__":
    raise SystemExit(dispatcher_writer_execution_monitor_cli_main())
