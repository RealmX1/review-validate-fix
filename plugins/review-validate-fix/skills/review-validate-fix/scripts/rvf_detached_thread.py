#!/usr/bin/env python3
"""通用 detached tmux 线程启动 helper（RVF 共享）。

把一条命令派进按 run 命名的 detached tmux session，提供：

- **两阶段原子 ``status.json``**：``launch_detached`` 启动时原子写入调用方构造的
  status payload（``launch_status="launched"`` + ``started_at`` 等）；被包命令在
  tmux 内退出时经 ``--finalize-status`` 回调把 ``returncode`` / ``finished_at``
  **merge** 进同一文件，不覆盖启动期字段。
- **``O_EXCL`` 每-run 幂等锁**：重复启动命中 ``already_running``；tmux 报
  duplicate session 同样落 ``already_running``；启动失败释放锁以便重试。
- **catch-all**：任何异常收敛成 ``launch_failed`` 返回，绝不让线程启动失败打断
  调用方主路径（finalize/handoff / reviewer 派发）。
- **可选总超时 backstop**（``total_timeout_seconds``）：经一个纯 Python 的
  ``--run-with-timeout`` 包装器对被包命令施加 wall-clock 上限（``start_new_session``
  + ``killpg`` SIGTERM→宽限→SIGKILL，超时退 124），**不依赖 ``timeout(1)``**，
  跨平台（macOS 默认无 ``timeout`` 二进制）。传 ``None`` → 不包装、行为不变。

本模块从 ``rvf_analyze_thread.py`` 抽出其 detached-launch 机制，供 analyze 线程与
reviewer dispatch 两条路径共用、消除重复。各路径专属决策（analyze 的 host 选择 /
prompt 冻结 / 自抑制 env；dispatch 的 run 解析 / 子命令拼装）仍留在各自调用方，
本 helper 只拥有「detached 起 tmux + 两阶段 status + 幂等锁 + 可选总超时」这一通用层。
"""
from __future__ import annotations

import json
import contextlib
import os
import secrets
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LAUNCH_LAUNCHED = "launched"
LAUNCH_ALREADY_RUNNING = "already_running"
LAUNCH_FAILED = "launch_failed"

DETACHED_LAUNCH_RESERVED_STATUS_FIELD_NAMES = frozenset({
    "schema_version", "run_dir", "run_name", "host", "tmux_session", "command",
    "pid", "started_at", "armed_at", "returncode", "finished_at", "launch_status",
    "error", "lock_path", "status_path", "idempotency_key",
    "dispatcher_writer_execution_id", "dispatcher_writer_execution_evidence_path", "dispatcher_writer_execution_entrypoint_sha256",
    "dispatcher_writer_execution_launch_boundary_status", "dispatcher_writer_execution_tmux_attempted_at",
})

# run-with-timeout backstop 命中 wall-clock 上限时的退出码：沿用 ``timeout(1)`` /
# ``run_alternative_reviewer.EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE`` 的 124 约定。
RUN_TIMEOUT_EXIT_CODE = 124
# 被包命令无法启动（OSError）时的退出码，沿用 shell「command not found / cannot
# execute」的 127 约定，便于调用方与正常退出码区分。
RUN_LAUNCH_ERROR_EXIT_CODE = 127

_HELPER_PATH = Path(__file__).resolve()


def tmux_bin() -> str:
    return os.environ.get("RVF_TMUX_BIN", "tmux")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _run_with_timeout_argv(total_timeout_seconds: float, inner_argv: list[str]) -> list[str]:
    """把 ``inner_argv`` 包进 ``python rvf_detached_thread.py --run-with-timeout <s> -- ...``。

    ``--run-with-timeout`` 恒为首参，故 ``main`` 可在 argparse 之前先手工识别它、
    避免 ``argparse.REMAINDER`` 对 ``--`` 的处理歧义。
    """
    return [
        sys.executable,
        str(_HELPER_PATH),
        "--run-with-timeout",
        str(float(total_timeout_seconds)),
        "--",
        *inner_argv,
    ]


def _build_wrapper_shell(
    *,
    argv: list[str],
    log_path: Path,
    status_path: Path,
    exports: dict[str, str],
    stdin_path: Path | None = None,
    post_exit_argv: list[str] | None = None,
) -> str:
    """拼 tmux 内执行的 shell：导出 env → 跑 ``argv``（log 落盘）→ 经 ``--finalize-status`` 回写 status。

    退出码经独立的 ``--finalize-status`` 回调写入 status.json，避免从 shell 手工拼
    JSON 丢字段。``$?`` 取最后一条命令（管道场景下是 ``argv``）的退出码。

    ``post_exit_argv``（可选，S8 守护计数用）：被包命令退出**且** status 回写之后追加
    执行的一条命令（同样 log 落盘）,以 ``|| true`` 兜底——post-exit 回调失败绝不改变
    wrapper 的整体行为，也不影响已回写的 status。缺省时输出与旧版逐字节相同。
    """
    export_lines = "".join(
        f"export {name}={shlex.quote(value)}; " for name, value in exports.items()
    )
    cmd = " ".join(shlex.quote(token) for token in argv)
    log_q = shlex.quote(str(log_path))
    if stdin_path is not None:
        run_line = f"cat {shlex.quote(str(stdin_path))} | {cmd} >> {log_q} 2>&1"
    else:
        run_line = f"{cmd} >> {log_q} 2>&1"
    finalize_cmd = " ".join(
        shlex.quote(token)
        for token in [
            sys.executable,
            str(_HELPER_PATH),
            "--finalize-status",
            "--status-path",
            str(status_path),
        ]
    )
    wrapper = (
        f"{export_lines}"
        f"{run_line}; rc=$?; "
        f"{finalize_cmd} --returncode \"$rc\" >> {log_q} 2>&1"
    )
    if post_exit_argv:
        post_cmd = " ".join(shlex.quote(token) for token in post_exit_argv)
        wrapper += f"; {post_cmd} >> {log_q} 2>&1 || true"
    return wrapper


def _tmux_session_alive(session_name: str) -> bool:
    """``tmux has-session`` 探活：仅当能**确定性**断定 session 不存在时才回 False。

    - has-session 跑完、returncode 0 → session 存活 → True。
    - has-session 跑完、returncode≠0（含「无此 session」「无 server」）→ 确定 tmux 不存在 → False。
      这不证明脱离该 session 的 dispatcher writer 已退出；该角色另核验实际执行回执。
    - 探活本身失败（tmux 二进制不可用 / PATH 问题 / 超时 / 任何异常）→ **无法确认** →
      **保守回 True（当作可能存活）**。这是「绝不误删活锁」不变量的关键：tmux 临时不可用
      时，决不能把仍在跑的 detached session 误判为死、进而删它的锁。代价是 tmux 持续不可用
      下真陈旧锁暂不被回收——这是更安全的失败方向（且回到 FU-2 之前的「不回收」行为，可由
      后续 tmux 恢复后的下一次派发兜底）。
    """
    try:
        completed = subprocess.run(
            [tmux_bin(), "has-session", "-t", session_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except Exception:  # noqa: BLE001
        return True
    return completed.returncode == 0


def _detached_run_finished_clean(status_path: Path) -> bool:
    """读 status.json，仅当 ``returncode == 0``（被包命令干净完成）才返回 True。

    缺文件 / 非法 JSON / 非 dict / 无 returncode / returncode≠0 一律 False（视作未干净
    完成，可被重派）。
    """
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("returncode") == 0


def probe_dispatcher_tmux_session_before_new_execution(session_name: str, dispatcher_tmux_executable: str,
    dispatcher_launch_environment: dict[str, str]) -> dict[str, Any]:
    """只读检查已有资源；占用/无法查询不消耗执行代，原生exec拒绝仍走正式创建补证。"""
    query_argv = [dispatcher_tmux_executable, "has-session", "-t", session_name]
    try:
        queried = subprocess.run(query_argv, capture_output=True, text=True, check=False, env=dispatcher_launch_environment, timeout=10)
    except (FileNotFoundError, PermissionError) as native_query_creation_error:
        import errno

        exact_native_exec_rejection = (native_query_creation_error.errno in (errno.ENOENT, errno.EACCES, errno.EPERM)
            and str(native_query_creation_error.filename) == dispatcher_tmux_executable)
        return {"status": "query_unavailable", "native_exec_rejection": exact_native_exec_rejection,
            "argv": query_argv, "error": f"{type(native_query_creation_error).__name__}: {native_query_creation_error}"}
    except (OSError, subprocess.SubprocessError) as query_error:
        return {"status": "query_unavailable", "argv": query_argv, "error": f"{type(query_error).__name__}: {query_error}"}
    result = {"argv": query_argv, "returncode": queried.returncode, "stdout": queried.stdout, "stderr": queried.stderr}
    if queried.returncode == 0:
        return {**result, "status": "occupied"}
    diagnostics = (queried.stderr + queried.stdout).lower()
    absent = (queried.returncode == 1 and not diagnostics.strip()) or any(message in diagnostics for message in ("can't find session", "cannot find session", "no such session", "no server running", "no sessions")) or (
        "no such file or directory" in diagnostics and ("error connecting" in diagnostics or "failed to connect" in diagnostics))
    return {**result, "status": "absent" if absent else "query_unavailable"}


def _reclaim_stale_detached_lock(
    *, lock_path: Path, session_name: str, status_path: Path, require_actual_dispatcher_writer_execution_evidence: bool = False,
) -> int | None:
    """detached 幂等锁的 staleness 判定 + 单次重夺。

    仅当 ``tmux has-session`` **确定性报 session 不存在**（探活成功且 returncode≠0）**且其
    run 未干净完成**（status.json ``returncode`` 非 0）时，判定锁陈旧：删旧锁并以 ``O_EXCL``
    单次重夺，成功则返回新 fd（调用方据此继续 launch，后续用新 payload 覆盖旧 status.json）。

    返回 None 的四种情形，调用方一律回退到 ``already_running``：

    - session 仍存活 → 真 already_running（不可重派）；
    - 探活无法确认存活/死亡（tmux 临时不可用 → ``_tmux_session_alive`` 保守回 True）→ 不动锁；
    - session 已死但 run 已干净完成 → 幂等、不重跑已完成的 run；
    - 重夺时与并发者竞态再撞 ``FileExistsError`` → 让对方持有。

    全程兜底：任何异常 → None（保守保留 already_running，绝不误删活锁）。
    """
    try:
        if require_actual_dispatcher_writer_execution_evidence:
            from dispatcher_writer_execution_evidence import observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence

            observed = observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status_path)
            if observed["dispatcher_writers_may_still_write"] or observed.get("actual_dispatcher_writer_delivery_returncode") == 0:
                return None
            if observed.get("evidence_kind") not in ("dispatcher_launch_not_attempted", "dispatcher_tmux_process_creation_failed") and _tmux_session_alive(session_name):
                return None
        elif _tmux_session_alive(session_name):
            return None
        if _detached_run_finished_clean(status_path):
            return None
        _unlink_quiet(lock_path)
        try:
            return os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return None
    except Exception:  # noqa: BLE001
        return None


@contextlib.contextmanager
def _dispatcher_writer_execution_launch_metadata_lock(lock_path: Path):
    from dispatcher_writer_execution_evidence import dispatcher_writer_execution_terminal_archive_admission_lock

    if lock_path.is_symlink():
        raise ValueError("dispatcher许可锁必须绑定正式run的幂等锁路径")
    with dispatcher_writer_execution_terminal_archive_admission_lock(lock_path.parent.parent.parent):
        yield


def launch_detached(
    *,
    session_name: str,
    argv: list[str],
    log_path: Path,
    status_path: Path,
    lock_path: Path,
    status_payload: dict[str, Any],
    launch_failure_status_fields: dict[str, Any] | None = None,
    exports: dict[str, str] | None = None,
    stdin_path: Path | None = None,
    launch_env: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    total_timeout_seconds: float | None = None,
    tmux_timeout: float = 30.0,
    post_exit_argv: list[str] | None = None,
    require_actual_dispatcher_writer_execution_evidence: bool = False,
) -> dict[str, Any]:
    """把 ``argv`` 派进 detached tmux session ``session_name``。

    返回 ``{launch_status, returncode, error, tmux_command}``：

    - ``launched``：tmux 成功起 session（``returncode`` = tmux new-session 退出码 0）。
    - ``already_running``：每-run O_EXCL 锁已存在（``tmux_command=None``、``returncode=None``），
      或 tmux 报 duplicate session（``tmux_command`` / ``returncode`` 为 tmux 实际值）。
    - ``launch_failed``：tmux 失败或任何异常；已写 failure status + 释放锁。

    调用方负责构造 ``status_payload``（含各自专属字段、``started_at`` 等）；本 helper
    启动时把它（``launch_status`` 强制为 ``launched``）原子落盘，并在被包命令退出时经
    ``--finalize-status`` 回调 merge ``returncode``/``finished_at``。``total_timeout_seconds``
    非 None 时对被包命令施加 wall-clock backstop（见模块 docstring）。
    专属失败字段经 ``launch_failure_status_fields`` 传入，由本 helper 持锁落盘；不得覆盖
    启动身份、锁、退出码等保留字段。调用方收到失败结果后不再补写共享状态。
    """
    exports = exports or {}
    lock_acquired = False
    payload = dict(status_payload)
    try:
        failure_status_fields = dict(launch_failure_status_fields or {})
        reserved_overrides = failure_status_fields.keys() & DETACHED_LAUNCH_RESERVED_STATUS_FIELD_NAMES
        reserved_overrides |= {field_name for field_name in failure_status_fields if field_name.startswith("dispatcher_writer_execution_")}
        if reserved_overrides:
            raise ValueError(f"失败扩展字段不能覆盖启动状态：{', '.join(sorted(reserved_overrides))}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        if require_actual_dispatcher_writer_execution_evidence and (
                status_path.resolve() != Path(payload["run_dir"]).resolve() / "artifacts/reviewers/.dispatch-thread.status.json"
                or lock_path.parent.resolve() != status_path.parent.resolve()):
            raise ValueError("dispatcher共同许可锁与正式状态路径归属不符")

        # 每-run O_EXCL 锁：第二次 launch 命中 FileExistsError → 默认 already_running
        # （不重写 status：首个 launch 已写过，保留其 started_at 等启动期字段）。
        # 例外——staleness 重夺：若持锁的 detached 线程确已死（tmux session 不在）且其
        # run 未干净完成，则该锁是陈旧锁（死 tmux 后内层命令失败/被杀，finalize 只回写
        # returncode、从不删锁），删后单次重夺以放行重派；其余情形仍幂等地保持
        # already_running。
        metadata_owner = _dispatcher_writer_execution_launch_metadata_lock(lock_path) if require_actual_dispatcher_writer_execution_evidence else contextlib.nullcontext()
        with metadata_owner:
            if require_actual_dispatcher_writer_execution_evidence:
                from dispatcher_writer_execution_evidence import (
                    DISPATCHER_WRITER_EXECUTION_PRELAUNCH_RECOVERY_ATTEMPTS_PER_INPUT_GENERATION,
                    observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence,
                    observe_dispatcher_writer_execution_prelaunch_recovery_inputs,
                    recover_unattempted_dispatcher_preparations_with_admission_lock_held,
                    assert_dispatcher_launch_not_prohibited_by_terminal_archive_intent,
                    bind_dispatcher_prelaunch_input_generation_with_admission_lock_held,
                )
                from core.run_ledger.review_run_lifecycle import read_json_object

                if payload.get("session_id") is None:
                    payload["session_id"] = read_json_object(Path(payload["run_dir"]) / "summary.json").get("session_id")
                summary_session_id = read_json_object(Path(payload["run_dir"]) / "summary.json").get("session_id")
                if summary_session_id is not None and payload.get("session_id") != summary_session_id:
                    raise ValueError("dispatcher恢复的显式会话归属与正式summary矛盾")
                payload["total_timeout_seconds"] = total_timeout_seconds
                dispatcher_tmux_executable_argv0 = tmux_bin()
                payload["dispatcher_writer_execution_tmux_executable_argv0"] = dispatcher_tmux_executable_argv0
                payload["dispatcher_writer_execution_tmux_executable_search_path"] = (launch_env if launch_env is not None else {**os.environ, **exports}).get("PATH")
                try:
                    previous_dispatcher_execution_receipts = recover_unattempted_dispatcher_preparations_with_admission_lock_held(status_path, payload)
                except (OSError, ValueError, KeyError, TypeError) as preparing_recovery_error:
                    return {"launch_status": LAUNCH_ALREADY_RUNNING, "returncode": None, "tmux_command": None,
                        "error": f"{type(preparing_recovery_error).__name__}: {preparing_recovery_error}",
                        "recovery_condition": "精确准备/交付回执及关键归属恢复一致后，在共同许可锁内自动重验"}
                if status_path.exists() or status_path.is_symlink() or (status_path.parent / "dispatcher-writer-executions").exists():
                    previous_dispatcher_execution_observation = observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status_path)
                    if (previous_dispatcher_execution_observation["dispatcher_writers_may_still_write"]
                            or previous_dispatcher_execution_observation.get("actual_dispatcher_writer_delivery_returncode") == 0):
                        return {"launch_status": LAUNCH_ALREADY_RUNNING, "returncode": None, "error": None, "tmux_command": None,
                                "recovery_condition": previous_dispatcher_execution_observation.get("recovery_condition")}
                    previous_dispatcher_status_payload = json.loads(status_path.read_text(encoding="utf-8"))
                    if (any(previous_dispatcher_status_payload.get(field_name) != payload.get(field_name)
                            for field_name in ("kind", "run_id", "session_id", "tmux_session", "command"))
                            or Path(previous_dispatcher_status_payload["run_dir"]).resolve() != Path(payload["run_dir"]).resolve()
                            or Path(previous_dispatcher_status_payload["repo"]).resolve() != Path(payload["repo"]).resolve()):
                        raise ValueError("dispatcher 恢复不能替换已有执行的精确归属或启动命令")
                    if previous_dispatcher_execution_observation.get("evidence_kind") in ("dispatcher_launch_not_attempted", "dispatcher_tmux_process_creation_failed", "dispatcher_monitor_child_process_creation_failed"):
                        payload["dispatcher_writer_execution_previous_prelaunch_failure_evidence_path"] = previous_dispatcher_execution_observation["dispatcher_writer_execution_evidence_path"]
                assert_dispatcher_launch_not_prohibited_by_terminal_archive_intent(status_path, payload)
                current_dispatcher_prelaunch_inputs = observe_dispatcher_writer_execution_prelaunch_recovery_inputs(argv, total_timeout_seconds,
                    dispatcher_tmux_executable_argv0, payload["dispatcher_writer_execution_tmux_executable_search_path"])
                current_dispatcher_prelaunch_inputs = bind_dispatcher_prelaunch_input_generation_with_admission_lock_held(status_path, payload, current_dispatcher_prelaunch_inputs, previous_dispatcher_execution_receipts)
                generation_receipt_count = sum(receipt["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"]
                    == current_dispatcher_prelaunch_inputs["dispatcher_writer_execution_prelaunch_recovery_generation_sha256"] for receipt in previous_dispatcher_execution_receipts)
                if generation_receipt_count >= DISPATCHER_WRITER_EXECUTION_PRELAUNCH_RECOVERY_ATTEMPTS_PER_INPUT_GENERATION:
                    return {"launch_status": LAUNCH_FAILED, "returncode": None, "error": "该输入代的精确准备回执已耗尽三次预算", "tmux_command": None,
                        "recovery_state": "waiting_for_dispatcher_prelaunch_input_change",
                        "recovery_condition": "入口/可执行条件或恢复实现内容变化后自动重验；孤立准备回执同样计数"}
                tmux_preflight = probe_dispatcher_tmux_session_before_new_execution(session_name, dispatcher_tmux_executable_argv0,
                    launch_env if launch_env is not None else {**os.environ, **exports})
                if tmux_preflight["status"] == "occupied" or (tmux_preflight["status"] == "query_unavailable" and not tmux_preflight.get("native_exec_rejection")):
                    return {"launch_status": LAUNCH_ALREADY_RUNNING, "returncode": None, "error": tmux_preflight.get("error"), "tmux_command": None,
                        "recovery_state": "waiting_for_tmux_session_release" if tmux_preflight["status"] == "occupied" else "waiting_for_tmux_query_recovery",
                        "recovery_condition": "精确tmux资源释放或查询恢复后自动重验；等待不创建新receipt或消耗次数",
                        "tmux_preflight_observation": tmux_preflight}
                payload["dispatcher_writer_execution_tmux_resource_preflight"] = tmux_preflight
                payload.update(current_dispatcher_prelaunch_inputs)
                payload["dispatcher_writer_execution_prelaunch_recovery_attempt_number"] = generation_receipt_count + 1
            try:
                fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = _reclaim_stale_detached_lock(lock_path=lock_path, session_name=session_name,
                    status_path=status_path, require_actual_dispatcher_writer_execution_evidence=require_actual_dispatcher_writer_execution_evidence)
                if fd is None:
                    outcome = {"launch_status": LAUNCH_ALREADY_RUNNING, "returncode": None, "error": None, "tmux_command": None}
                    if require_actual_dispatcher_writer_execution_evidence:
                        outcome.update({"recovery_state": "waiting_for_dispatcher_lock_or_evidence_recovery",
                            "recovery_condition": "精确tmux资源/查询及执行回执恢复后自动重验；等待不消耗新准备次数"})
                    return outcome
            lock_acquired = True
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{idempotency_key or session_name}\n{_iso_now()}\n")
            payload["launch_status"] = LAUNCH_LAUNCHED
            if require_actual_dispatcher_writer_execution_evidence:
                from dispatcher_writer_execution_evidence import (
                    initialize_dispatcher_writer_execution_binding_before_entrypoint_read,
                    complete_dispatcher_writer_execution_entrypoint_content_binding,
                )

                payload = initialize_dispatcher_writer_execution_binding_before_entrypoint_read(status_path, payload)
                if total_timeout_seconds is None or total_timeout_seconds <= 0:
                    raise ValueError("dispatcher writer monitor 必须具有明确正数总时限")
                payload = complete_dispatcher_writer_execution_entrypoint_content_binding(status_path)
            _atomic_write_json(status_path, payload)

            effective_argv = (
                _run_with_timeout_argv(total_timeout_seconds, argv)
                if total_timeout_seconds is not None
                else list(argv)
            )
            if require_actual_dispatcher_writer_execution_evidence:
                from dispatcher_writer_execution_evidence import build_dispatcher_writer_execution_monitor_argv

                effective_argv = build_dispatcher_writer_execution_monitor_argv(status_path, payload, total_timeout_seconds)
            shell_command = _build_wrapper_shell(
                argv=effective_argv,
                log_path=log_path,
                status_path=status_path,
                exports=exports,
                stdin_path=stdin_path,
                post_exit_argv=post_exit_argv,
            )
            tmux_command = [dispatcher_tmux_executable_argv0 if require_actual_dispatcher_writer_execution_evidence else tmux_bin(), "new-session", "-d", "-s", session_name, shell_command]
            env = launch_env if launch_env is not None else {**os.environ, **exports}
            if require_actual_dispatcher_writer_execution_evidence:
                from dispatcher_writer_execution_evidence import record_dispatcher_writer_execution_tmux_attempt_with_admission_lock_held

                payload = record_dispatcher_writer_execution_tmux_attempt_with_admission_lock_held(status_path, payload["dispatcher_writer_execution_id"], tmux_command)
        if require_actual_dispatcher_writer_execution_evidence:
            from dispatcher_writer_execution_evidence import run_dispatcher_tmux_launcher_with_native_process_creation_evidence

            completed = run_dispatcher_tmux_launcher_with_native_process_creation_evidence(
                status_path, payload["dispatcher_writer_execution_id"], tmux_command, env, tmux_timeout)
        else:
            completed = subprocess.run(
                tmux_command,
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=tmux_timeout,
            )

        if completed.returncode != 0:
            combined = (completed.stderr + completed.stdout).lower()
            if "duplicate session" in combined:
                payload["launch_status"] = LAUNCH_ALREADY_RUNNING
                _atomic_write_json(status_path, payload)
                return {
                    "launch_status": LAUNCH_ALREADY_RUNNING,
                    "returncode": completed.returncode,
                    "error": None,
                    "tmux_command": tmux_command,
                }
            error = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "tmux new-session failed"
            )
            payload.update(failure_status_fields)
            payload["launch_status"] = LAUNCH_FAILED
            payload["error"] = error
            payload["returncode"] = completed.returncode
            _atomic_write_json(status_path, payload)
            # launch 失败时释放锁，便于后续重新 launch。
            if not require_actual_dispatcher_writer_execution_evidence:
                _unlink_quiet(lock_path)
            return {
                "launch_status": LAUNCH_FAILED,
                "returncode": completed.returncode,
                "error": error,
                "tmux_command": tmux_command,
            }

        return {
            "launch_status": LAUNCH_LAUNCHED,
            "returncode": completed.returncode,
            "error": None,
            "tmux_command": tmux_command,
        }
    except Exception as exc:  # noqa: BLE001 - 启动失败绝不阻断调用方主路径。
        error = f"{type(exc).__name__}: {exc}"
        if lock_acquired:
            try:
                failure_metadata_owner = _dispatcher_writer_execution_launch_metadata_lock(lock_path) if require_actual_dispatcher_writer_execution_evidence else contextlib.nullcontext()
                with failure_metadata_owner:
                    if require_actual_dispatcher_writer_execution_evidence and payload.get("dispatcher_writer_execution_id"):
                        from dispatcher_writer_execution_evidence import record_dispatcher_writer_execution_failure_before_tmux_attempt

                        latest_dispatcher_failure_status = json.loads(status_path.read_text(encoding="utf-8"))
                        if latest_dispatcher_failure_status.get("dispatcher_writer_execution_id") != payload["dispatcher_writer_execution_id"]:
                            raise ValueError("dispatcher 失败清理不能覆盖其他执行绑定")
                        payload = latest_dispatcher_failure_status
                        try:
                            payload = record_dispatcher_writer_execution_failure_before_tmux_attempt(status_path, payload["dispatcher_writer_execution_id"], error)
                        except ValueError:
                            pass
                    failure = {**payload, **failure_status_fields}
                    failure["launch_status"] = LAUNCH_FAILED
                    failure["error"] = error
                    failure.setdefault("returncode", None)
                    failure.setdefault("finished_at", None)
                    _atomic_write_json(status_path, failure)
            except Exception:  # noqa: BLE001
                pass
            if not require_actual_dispatcher_writer_execution_evidence:
                _unlink_quiet(lock_path)
        return {
            "launch_status": LAUNCH_FAILED,
            "returncode": None,
            "error": error,
            "tmux_command": None,
        }


def _finalize_status(status_path: Path, returncode: int) -> int:
    """tmux 内 shell 完成时回写 returncode / finished_at；保留 launch 期字段。"""
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["returncode"] = returncode
    payload["finished_at"] = _iso_now()
    try:
        _atomic_write_json(status_path, payload)
    except OSError:
        return 1
    return 0


def _terminate_process_group(proc: "subprocess.Popen[Any]") -> None:
    """对 ``proc`` 所在进程组发 SIGTERM→宽限→SIGKILL（own session，故可安全 killpg）。"""
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    def _signal(sig: int) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except OSError:
                pass
        try:
            proc.send_signal(sig)
        except OSError:
            pass

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal(signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_with_timeout(total_timeout_seconds: float, inner_argv: list[str]) -> int:
    """跑 ``inner_argv`` 并施加 wall-clock 上限；超时则 killpg 整个进程组。

    纯 Python、不依赖 ``timeout(1)``，跨平台。返回子进程退出码；超时返回
    ``RUN_TIMEOUT_EXIT_CODE``(124)，无法启动返回 ``RUN_LAUNCH_ERROR_EXIT_CODE``(127)。
    ``start_new_session=True`` 让子进程自成进程组，超时 killpg 能连带清理其后代
    （reviewer → 外部 agent 子进程链）。
    """
    try:
        proc = subprocess.Popen(inner_argv, start_new_session=True)
    except OSError as exc:
        print(f"rvf_detached_thread: failed to start command: {exc}", file=sys.stderr)
        return RUN_LAUNCH_ERROR_EXIT_CODE
    try:
        return proc.wait(timeout=total_timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"rvf_detached_thread: command exceeded total backstop "
            f"{total_timeout_seconds:g}s; terminating process group.",
            file=sys.stderr,
        )
        _terminate_process_group(proc)
        return RUN_TIMEOUT_EXIT_CODE


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # --run-with-timeout 恒为首参（见 _run_with_timeout_argv）：在 argparse 之前手工
    # 识别，规避 argparse.REMAINDER 对 `--` 的处理歧义。
    if raw and raw[0] == "--run-with-timeout":
        if len(raw) < 2:
            return 2
        try:
            seconds = float(raw[1])
        except ValueError:
            return 2
        inner = raw[2:]
        if inner and inner[0] == "--":
            inner = inner[1:]
        if not inner:
            return 2
        return _run_with_timeout(seconds, inner)

    import argparse

    parser = argparse.ArgumentParser(description="RVF detached tmux thread helper.")
    parser.add_argument("--finalize-status", action="store_true")
    parser.add_argument("--status-path")
    parser.add_argument("--returncode", type=int)
    args = parser.parse_args(raw)
    if args.finalize_status:
        if not args.status_path or args.returncode is None:
            return 2
        return _finalize_status(Path(args.status_path), args.returncode)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
