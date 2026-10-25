"""准备轮次的非成功终态归档，以及按精确仓库和会话接续的持久恢复。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401
from core.run_ledger.review_run_lifecycle import atomic_write_json, read_json_object, review_run_metadata_lock
from core.run_ledger.run_ledger import RunLedger
from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from rvf_analyze_advisory import surface_rvf_analyze_advisory
from rvf_run_finalize import (
    finalize_run, NON_REVIEW_COMPLETION_KINDS, read_review_run_finalization_evidence,
    terminal_archive_finalization_progress_recovery_evidence_status,
)


UNSUCCESSFUL_TERMINAL_KINDS = NON_REVIEW_COMPLETION_KINDS
ARCHIVE_REQUEST_FILENAME = "review-run-terminal-archive-request.json"
MAX_ARCHIVE_ATTEMPTS_PER_EVIDENCE_GENERATION = 3
ANALYSIS_RECOVERY_FILENAME = "review-run-analysis-recovery.json"
ANALYSIS_RECOVERY_REQUEST_IDENTITY_FIELDS = ("run_id", "run_dir", "repo_realpath", "session_id")
ARCHIVE_REQUEST_IDENTITY_FIELDS = (
    "run_id", "run_dir", "repo_realpath", "session_id", "decision_kind", "requested_at", "reason",
    "scope_contract_sha256",
)
ARCHIVE_REQUEST_RECOVERY_OWNED_FIELDS = frozenset({
    "status", "attempt_count", "evidence_generation_sha256", "last_attempt_at", "last_error",
    "waited_for_writer_evidence", "writer_evidence", "recovery_condition", "recovery_condition_code",
    "analysis_recovery_condition", "archived_at", "review_completed", "reconciled_at",
    "finalize_completion_evidence", "preserved_terminal_status",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pending_archive_directory(repo: Path, session_id: str) -> Path:
    identity = hashlib.sha256((str(repo.resolve()) + "\n" + session_id).encode()).hexdigest()
    return resolved_rvf_user_local_data_paths().runtime_state_root / "pending-review-run-terminal-archives" / identity


def _archive_evidence_generation(request: dict[str, Any]) -> str:
    run_dir = Path(request["run_dir"])
    evidence: list[Any] = []
    for source in (Path(__file__), Path(__file__).with_name("rvf_run_finalize.py"),
                   Path(__file__).with_name("trajectory_capture.py"), Path(__file__).with_name("analysis_artifacts.py")):
        evidence.append((source.name, hashlib.sha256(source.read_bytes()).hexdigest()))
    for name in ("transcript_path", "session_path", "conversation_path", "log_path", "session_file"):
        raw = request.get("event", {}).get(name)
        if isinstance(raw, str):
            path = Path(raw).expanduser()
            evidence.append((name, str(path), path.is_file(), os.access(path, os.R_OK)))
    # 输入身份或可读性发生改变才刷新预算；每次新增日志不刷新预算。
    for relative in ("artifacts/inputs/scope.contract.json", "artifacts/before-workspace-snapshot.json"):
        path = run_dir / relative
        evidence.append((relative, path.is_file(), os.access(path, os.R_OK)))
    evidence.append(("finalization_stage_evidence_status", terminal_archive_finalization_progress_recovery_evidence_status(request)))
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def _recorded_process_may_still_write(pid: int, run_id: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        observed = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                  capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True
    # PID 已复用且命令明确属于别处时不能阻塞旧 run，也绝不操作该进程。
    return run_id in observed.stdout if observed.returncode == 0 and observed.stdout.strip() else True


def _fixer_delivery_artifact_evidence(
    run_dir: Path, summary: dict[str, Any], tracker_attempt: dict[str, Any],
) -> dict[str, Any]:
    """复用 stop 已交付的精确工件，不把交付终态冒充进程退出。"""
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    attempt_id = tracker_attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or attempt_id in ("", ".", "..") or Path(attempt_id).name != attempt_id:
        raise ValueError("fixer attempt 目录身份无效")
    directory = run_dir / "artifacts/fix-attempts" / attempt_id
    paths = [directory / "attempt.json", directory / "result.json", directory / "fix.patch"]
    if directory.is_symlink() or directory.parent.is_symlink() or any(
        path.is_symlink() or not path.is_file() for path in paths
    ):
        raise ValueError("fixer 交付工件缺失、越界或为符号链接")
    attempt, result = (read_json_object(path) for path in paths[:2])
    for key, expected in (("run_id", summary["run_id"]), ("attempt_id", attempt_id),
                          ("issue_id", attempt.get("issue_id"))):
        if (not isinstance(expected, str) or not expected or attempt.get(key) != expected
                or result.get(key) != expected or tracker_attempt.get(key, expected) != expected):
            raise ValueError(f"fixer 交付工件的 {key} 归属不符")
    if (not isinstance(attempt.get("repo"), str) or Path(attempt["repo"]).resolve() != Path(summary["repo"]).resolve()
            or not isinstance(attempt.get("worktree_path"), str)
            or Path(attempt["worktree_path"]).resolve() != (directory / "worktree").resolve()
            or (tracker_attempt.get("worktree_path") is not None
                and Path(tracker_attempt["worktree_path"]).resolve() != Path(attempt["worktree_path"]).resolve())):
        raise ValueError("fixer 交付工件的仓库或 worktree 归属不符")
    terminal_statuses = reviewable_unit_diff_tracker.RVF_ATTEMPT_STATUSES - {"prepared", "started", "applied", "merge_conflict"}
    if result.get("status") not in terminal_statuses or not (
        attempt.get("status") == result["status"]
        or result["status"] == "fixed" and attempt.get("status") in {
            "applied", "merge_conflict", "scope_expansion_conflict", "requires_main_agent_confirmation",
        }
    ):
        raise ValueError("fixer 双工件尚未达成一致交付终态")
    for payload in (attempt, result):
        if (not isinstance(payload.get("fix_patch_path"), str)
                or Path(payload["fix_patch_path"]).resolve() != paths[2].resolve()):
            raise ValueError("fixer 交付 patch 归属不符")
    return {"attempt_id": attempt_id, "status": "delivered", "evidence_kind": "fixer_delivery_artifacts",
            "process_exit_status": "unknown", "delivery_status": result["status"],
            "artifacts": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]}


def _writers_still_active(run_dir: Path, observations: list[dict[str, Any]] | None = None) -> bool:
    observations = observations if observations is not None else []
    summary = read_json_object(run_dir / "summary.json")
    for pid in summary.get("still_running_pids", []):
        if type(pid) is int and pid > 1 and _recorded_process_may_still_write(pid, summary["run_id"]):
            observations.append({"status": "active", "evidence_kind": "recorded_run_writer_process", "pid": pid})
            return True
    status_path = run_dir / "artifacts/reviewers/.dispatch-thread.status.json"
    if status_path.is_file() or status_path.is_symlink() or (status_path.parent / "dispatcher-writer-executions").is_dir():
        from dispatcher_writer_execution_evidence import observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence

        observed = observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status_path)
        observations.append(observed)
        if observed["dispatcher_writers_may_still_write"]:
            return True
    attempts_root = run_dir / "artifacts/fix-attempts"
    if attempts_root.is_dir():
        from core.session_scope_allocation import reviewable_unit_diff_tracker

        tracker_error = None
        try:
            evidence = reviewable_unit_diff_tracker.rvf_causality_for_run(repo=Path(summary["repo"]), run_id=summary["run_id"])
        except Exception as exc:
            evidence = {}
            tracker_error = f"{type(exc).__name__}: {exc}"
        attempts = {attempt["attempt_id"]: attempt for attempt in evidence.get("fix_attempts", [])}
        for directory in attempts_root.iterdir():
            if directory.is_dir() or directory.is_symlink():
                attempts.setdefault(directory.name, {"attempt_id": directory.name})
        if tracker_error and not attempts:
            raise RuntimeError(tracker_error)
        blocked = False
        for attempt in attempts.values():
            if attempt.get("status") not in (None, "created", "prepared", "running", "started"):
                continue
            try:
                observation = _fixer_delivery_artifact_evidence(run_dir, summary, attempt)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                blocked = True
                observation = {"attempt_id": attempt.get("attempt_id"), "status": "unknown",
                    "evidence_kind": "fixer_delivery_artifacts", "process_exit_status": "unknown",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovery_condition": "精确 attempt/result/patch 交付工件或 tracker 原生终态恢复可读后自动重验"}
            if tracker_error:
                observation["tracker_read_error"] = tracker_error
            observations.append(observation)
        return blocked
    return False


def _current_analysis_recovery_request_identity(run_dir: Path, event: dict[str, Any]) -> dict[str, str]:
    summary = read_json_object(run_dir / "summary.json")
    if any(not isinstance(summary.get(key), str) or not summary[key]
           for key in ("run_id", "run_dir", "repo", "session_id")):
        raise ValueError("正式 run 的分析恢复归属字段缺失，身份仍未知")
    if Path(summary["run_dir"]).resolve() != run_dir:
        raise ValueError("正式 run 的目录归属不符")
    if event.get("session_id") is not None and event["session_id"] != summary["session_id"]:
        raise ValueError("分析恢复事件与正式 run 的会话归属不符")
    return {"run_id": summary["run_id"], "run_dir": str(run_dir),
            "repo_realpath": str(Path(summary["repo"]).resolve()), "session_id": summary["session_id"]}


def _read_bound_analysis_recovery_request(
    request_path: Path, identity: dict[str, str], *, allow_missing: bool,
) -> dict[str, Any] | None:
    expected_path = Path(identity["run_dir"]) / "artifacts" / ANALYSIS_RECOVERY_FILENAME
    if request_path != expected_path or request_path.resolve() != expected_path or request_path.is_symlink():
        raise ValueError("分析恢复请求必须为精确普通路径，不能经符号链接跳转")
    try:
        descriptor = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        if allow_missing:
            return None
        raise ValueError("已绑定的分析恢复请求丢失，身份仍未知") from exc
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("分析恢复请求必须为普通文件")
        request = json.load(source)
    if not isinstance(request, dict) or any(
        not isinstance(request.get(key), str) or request[key] != identity[key]
        for key in ANALYSIS_RECOVERY_REQUEST_IDENTITY_FIELDS
    ):
        raise ValueError("分析恢复请求的 run/repo/session 归属缺失或不符")
    return request


def claim_review_run_analysis_launch(*, analysis: dict[str, str], event: dict[str, Any]) -> dict[str, Any] | None:
    """为后台启动保留有限预算；执行预算另外由分析包装器持久保存。"""
    run_dir = Path(analysis["run_dir"]).resolve()
    from rvf_analyze_thread import build_analyze_command, select_host
    from rvf_analyze_formal_archive_verified_execution import (
        automatic_analysis_evidence_generation, reconcile_formal_analysis_completion_without_agent_execution,
    )

    request_path = run_dir / "artifacts" / ANALYSIS_RECOVERY_FILENAME
    with review_run_metadata_lock(run_dir):
        identity = _current_analysis_recovery_request_identity(run_dir, event)
        repo, session_id = Path(identity["repo_realpath"]), identity["session_id"]
        index = _pending_archive_directory(repo, session_id) / f"analysis-{hashlib.sha256(str(run_dir).encode()).hexdigest()}.json"
        request = _read_bound_analysis_recovery_request(request_path, identity, allow_missing=True)
        request_was_present = request is not None
        if request is None:
            request = {"schema_version": 1, **identity,
                       "event": {key: event[key] for key in ("cwd", "session_id", "transcript_path", "session_path",
                                 "conversation_path", "log_path", "session_file", "kanban_task_id",
                                 "suppress_review_validate_fix", "review_validate_fix_suppressed") if key in event}}
        thread = read_json_object(run_dir / "artifacts/analysis/.analyze-thread.status.json")
        can_reconcile_without_agent = (Path(thread.get("run_dir", "")).resolve() == run_dir and (
            thread.get("formal_analysis_completion_status") == "completed" or thread.get("agent_process_returncode") == 0))
    if can_reconcile_without_agent:
        # 正式产物核验、分析互斥和 guardian 补记都在短元数据锁之外；不重启 Analyze。
        reconciliation = reconcile_formal_analysis_completion_without_agent_execution(run_dir)
        if reconciliation["status"] in ("completed", "waiting_for_evidence"):
            counter_pending = reconciliation.get("formal_analysis_completion_counter_status") != "recorded"
            completion_evidence_pending = reconciliation["status"] == "waiting_for_evidence"
            with review_run_metadata_lock(run_dir):
                if _current_analysis_recovery_request_identity(run_dir, event) != identity:
                    raise ValueError("长操作后的正式 run 归属已经改变")
                latest_request = _read_bound_analysis_recovery_request(request_path, identity, allow_missing=not request_was_present)
                request = request if latest_request is None else latest_request
                request.update({"status": "waiting_for_evidence" if counter_pending or completion_evidence_pending else "completed",
                                "formal_analysis_completion_status": reconciliation.get("formal_analysis_completion_status") or thread.get("formal_analysis_completion_status"),
                                "formal_analysis_completion_counter_status": "pending" if counter_pending else "recorded",
                                "completion_reconciliation_observed_at": _now(), "recovery_condition": reconciliation.get("recovery_condition")})
                if reconciliation["status"] == "completed":
                    request["completed_observed_at"] = _now()
                atomic_write_json(request_path, request)
                if counter_pending or completion_evidence_pending:
                    atomic_write_json(index, {"kind": "analysis_completion", "request_path": str(request_path), "run_id": identity["run_id"]})
            return {"status": request["status"], "request_path": str(request_path),
                    **identity,
                    "formal_analysis_completion_status": request["formal_analysis_completion_status"],
                    "formal_analysis_completion_counter_status": request["formal_analysis_completion_counter_status"],
                    "recovery_condition": request["recovery_condition"]}
    agent_argv, _stdin = build_analyze_command(select_host(event), analysis_directory=run_dir / "artifacts/analysis")
    generation = hashlib.sha256((automatic_analysis_evidence_generation(run_dir, agent_argv)
        + str(shutil.which(os.environ.get("RVF_TMUX_BIN", "tmux")))).encode()).hexdigest()
    with review_run_metadata_lock(run_dir):
        if _current_analysis_recovery_request_identity(run_dir, event) != identity:
            raise ValueError("长操作后的正式 run 归属已经改变")
        latest_request = _read_bound_analysis_recovery_request(request_path, identity, allow_missing=not request_was_present)
        request = request if latest_request is None else latest_request
        if request.get("evidence_generation_sha256") != generation:
            request = {**request, "schema_version": 1, **identity,
                       "evidence_generation_sha256": generation, "attempt_count": 0, "status": "pending"}
        atomic_write_json(index, {"kind": "analysis_completion", "request_path": str(request_path), "run_id": identity["run_id"]})
        if request.get("attempt_count", 0) >= 3:
            request.update({"status": "waiting_for_evidence", "recovery_condition": "执行版本、入口、tmux 可用性或登录配置变化后重验"})
            atomic_write_json(request_path, request)
            return {"status": "waiting_for_evidence", "request_path": str(request_path), **identity}
        request["attempt_count"] += 1
        request["status"] = "launch_claimed"
        request["analysis_launch_claim_id"] = secrets.token_hex(16)
        atomic_write_json(request_path, request)
    return {"status": "launch_claimed", "request_path": str(request_path), **identity,
            "analysis_launch_claim_id": request["analysis_launch_claim_id"]}


def record_review_run_analysis_launch(claim: dict[str, Any] | None, outcome: dict[str, Any]) -> None:
    if not claim or claim.get("status") != "launch_claimed":
        return
    if any(not isinstance(claim.get(key), str) or not claim[key]
           for key in (*ANALYSIS_RECOVERY_REQUEST_IDENTITY_FIELDS, "request_path", "analysis_launch_claim_id")):
        raise ValueError("分析启动认领缺少完整绑定身份，不能猜测旧格式归属")
    identity = {key: claim[key] for key in ANALYSIS_RECOVERY_REQUEST_IDENTITY_FIELDS}
    run_dir, path = Path(identity["run_dir"]), Path(claim["request_path"])
    if str(run_dir.resolve()) != identity["run_dir"]:
        raise ValueError("分析启动认领的 run 目录不是精确路径")
    with review_run_metadata_lock(run_dir):
        if _current_analysis_recovery_request_identity(run_dir, {"session_id": identity["session_id"]}) != identity:
            raise ValueError("分析启动认领与当前正式 run 归属不符")
        request = _read_bound_analysis_recovery_request(path, identity, allow_missing=False)
        if request["status"] != "launch_claimed" or request.get("analysis_launch_claim_id") != claim["analysis_launch_claim_id"]:
            raise ValueError("分析启动结果不属于当前仍有效的唯一认领")
        if outcome.get("launch_status") == "already_running":
            request["attempt_count"] = max(0, request["attempt_count"] - 1)
        request.update({"status": "pending", "last_launch_status": outcome.get("launch_status"),
                        "last_error": outcome.get("error"), "last_observed_at": _now()})
        atomic_write_json(path, request)


def _automatic_analysis_is_prohibited(event: dict[str, Any]) -> bool:
    from codex_gui_without_kanban_task_stop_hook_policy import codex_gui_without_kanban_task_stop_hook_is_disabled

    return (event.get("suppress_review_validate_fix") is True or event.get("review_validate_fix_suppressed") is True
            or any(os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "y", "on"}
                   for name in ("RVF_SUPPRESS", "RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"))
            or codex_gui_without_kanban_task_stop_hook_is_disabled(event))


def _surface_terminal_archive_analysis(request: dict[str, Any], summary: dict[str, Any],
                                       finalization: dict[str, Any]) -> None:
    if not isinstance(finalization.get("analysis"), dict):
        return
    event = dict(request.get("event") or {})
    session_id = request.get("session_id")
    run_dir = Path(request["run_dir"])
    harness = read_json_object(run_dir / "artifacts/inputs/main-harness.json")
    transcript = event.get("transcript_path") or summary.get("origin_transcript_path") or harness.get("transcript")
    if not isinstance(transcript, str) or not Path(transcript).is_file():
        captured = run_dir / "artifacts/trajectory/rvf/rollout.jsonl"
        manifest = read_json_object(captured.parent / "rollout.manifest.json")
        if (captured.is_file() and not captured.is_symlink() and manifest.get("source_session_id") == session_id
                and manifest.get("captured_sha256") == hashlib.sha256(captured.read_bytes()).hexdigest()):
            transcript = str(captured)
    if not session_id or not isinstance(transcript, str) or not Path(transcript).is_file():
        request["analysis_recovery_condition"] = "绑定 transcript 或同身份且摘要匹配的归档副本恢复后重验"
        return
    event.update({"session_id": session_id, "transcript_path": transcript})
    if _automatic_analysis_is_prohibited(event):
        request["analysis_recovery_condition"] = "现有自动分析禁止策略解除后重验；不消耗执行预算"
        return
    ledger = RunLedger(component="prepare-run", repo=request["repo_realpath"], run_id=request["run_id"], run_dir=run_dir)
    surface_rvf_analyze_advisory(event=event, ledger=ledger, payload={}, finalize_record=finalization)
    request.pop("analysis_recovery_condition", None)


def _resume_analysis_request(request_path: Path, *, repo: Path, session_id: str) -> dict[str, Any]:
    request = read_json_object(request_path)
    run_dir = Path(request["run_dir"]).resolve()
    if request_path.is_symlink() or request_path.resolve() != run_dir / "artifacts" / ANALYSIS_RECOVERY_FILENAME:
        raise ValueError("分析恢复请求路径不符")
    if request.get("repo_realpath") != str(repo.resolve()) or request.get("session_id") != session_id:
        raise ValueError("分析恢复的仓库或会话归属不符")
    summary = read_json_object(run_dir / "summary.json")
    if summary.get("run_id") != request.get("run_id") or Path(summary["repo"]).resolve() != repo.resolve():
        raise ValueError("分析恢复请求与正式 run 身份不符")
    if _automatic_analysis_is_prohibited(request.get("event", {})):
        return {"status": "prohibited_by_current_policy", "run_id": request["run_id"]}
    ledger = RunLedger(component="prepare-run", repo=str(repo), run_id=request["run_id"], run_dir=run_dir)
    fields = surface_rvf_analyze_advisory(event=request.get("event", {}), ledger=ledger, payload={}, finalize_record=summary.get("finalize"))
    return {"status": "completed" if (fields or {}).get("rvf_analyze_status") == "completed" else "waiting_for_evidence",
            "run_id": request["run_id"], "analysis_recovery": fields}


def _validate_archive_run_contract_identity(
    run_dir: Path, summary: dict[str, Any], contract: dict[str, Any], *, requested_session: str | None = None,
) -> str | None:
    """未知字段保持未知；已声明的 run、仓库及会话必须交叉一致。"""
    run_id, repo = summary.get("run_id"), summary.get("repo")
    if not isinstance(run_id, str) or not run_id or not isinstance(repo, str) or not repo:
        raise ValueError("终态归档缺少正式 run 或仓库身份")
    if summary.get("run_dir") is not None and Path(summary["run_dir"]).resolve() != run_dir.resolve():
        raise ValueError("终态归档 summary 的精确 run 目录身份不符")
    declarations = [summary, contract]
    for key in ("review_candidate_snapshot", "review_start_boundary_receipt"):
        declaration = contract.get(key)
        if declaration is not None:
            if not isinstance(declaration, dict):
                raise ValueError("终态归档的固定候选身份声明必须为对象")
            declarations.append(declaration)
    sessions = []
    for declaration in declarations:
        declared_run = declaration.get("run_id")
        declared_repo = declaration.get("repo")
        declared_directory = declaration.get("run_dir")
        if declared_run is not None and declared_run != run_id:
            raise ValueError("终态归档的固定 contract 与正式 run 身份矛盾")
        if declared_directory is not None and (not isinstance(declared_directory, str) or not declared_directory
                or Path(declared_directory).expanduser().resolve() != run_dir.resolve()):
            raise ValueError("终态归档的固定 contract 与精确 run 目录身份矛盾")
        if declared_repo is not None and (not isinstance(declared_repo, str) or not declared_repo
                or Path(declared_repo).expanduser().resolve() != Path(repo).expanduser().resolve()):
            raise ValueError("终态归档的固定 contract 与仓库归属矛盾")
        declared_session = declaration.get("session_id")
        if declared_session is not None:
            if not isinstance(declared_session, str) or not declared_session:
                raise ValueError("终态归档的已声明会话身份无效")
            sessions.append(declared_session)
    if requested_session is not None:
        sessions.append(requested_session)
    if sessions and any(value != sessions[0] for value in sessions):
        raise ValueError("终态归档的 summary、固定 contract 或请求会话身份矛盾")
    return sessions[0] if sessions else None


def _read_bound_archive_request(
    request_path: Path, *, repo: Path, session_id: str, expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if request_path.is_symlink():
        raise ValueError("终态归档请求不能是符号链接")
    return _validate_bound_archive_request_payload(request_path, read_json_object(request_path),
        repo=repo, session_id=session_id, expected=expected)


def _validate_bound_archive_request_payload(
    request_path: Path, request: dict[str, Any], *, repo: Path, session_id: str, expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """canonical和已接受登记共用身份门禁；尚未生成canonical也不得放宽scope。"""
    if request_path.is_symlink():
        raise ValueError("终态归档请求不能是符号链接")
    if not isinstance(request.get("run_dir"), str):
        raise ValueError("终态归档请求缺少精确 run 目录身份")
    run_dir = Path(request["run_dir"])
    if request_path.resolve() != (run_dir / "artifacts" / ARCHIVE_REQUEST_FILENAME).resolve():
        raise ValueError("终态归档请求路径身份不符")
    if (request.get("repo_realpath") != str(repo.resolve()) or request.get("session_id", "") != session_id
            or request.get("decision_kind") not in UNSUCCESSFUL_TERMINAL_KINDS):
        raise ValueError("终态归档的仓库、会话或终态归属不符")
    if expected and any(request.get(key) != expected.get(key) for key in ARCHIVE_REQUEST_IDENTITY_FIELDS):
        raise ValueError("终态归档请求的已绑定身份发生变化")
    summary = read_json_object(run_dir / "summary.json")
    if (summary.get("run_id") != request.get("run_id") or not isinstance(summary.get("repo"), str)
            or Path(summary["repo"]).resolve() != repo.resolve()
            or summary.get("session_id") and summary["session_id"] != session_id):
        raise ValueError("终态归档请求与正式 run 身份不符")
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    expected_digest = request.get("scope_contract_sha256")
    if contract_path.is_symlink() or expected_digest and (
        not contract_path.is_file() or hashlib.sha256(contract_path.read_bytes()).hexdigest() != expected_digest
    ):
        raise ValueError("终态归档的固定 scope contract 已改变，须恢复原绑定证据")
    _validate_archive_run_contract_identity(run_dir, summary, read_json_object(contract_path), requested_session=session_id)
    return request, summary


def _terminal_archive_accepted_registration_snapshot_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _terminal_archive_registration_index_path(*, run_dir: Path, repo: Path, session_id: str) -> Path:
    return _pending_archive_directory(repo, session_id) / f"{hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()}.json"


def read_accepted_terminal_archive_registration(*, run_dir: Path, repo: Path, session_id: str) -> dict[str, Any] | None:
    """只读精确登记，索引原子提交即接受点；不扫描或猜测run目录。"""
    index_path = _terminal_archive_registration_index_path(run_dir=run_dir, repo=repo, session_id=session_id)
    if not index_path.exists() and not index_path.is_symlink():
        return None
    if index_path.is_symlink() or run_dir.is_symlink() or (run_dir / "summary.json").is_symlink():
        raise ValueError("已接受终态登记不能经过符号链接")
    index = read_json_object(index_path)
    request = index.get("accepted_request_snapshot")
    request_path = run_dir.resolve() / "artifacts" / ARCHIVE_REQUEST_FILENAME
    if (index.get("kind") != "accepted_terminal_archive_request_registration" or index.get("schema_version") != 1 or not isinstance(request, dict)
            or index.get("request_path") != str(request_path) or index.get("run_dir") != str(run_dir.resolve())
            or index.get("repo_realpath") != str(repo.resolve()) or index.get("session_id") != session_id
            or index.get("run_id") != request.get("run_id")
            or index.get("accepted_request_snapshot_sha256") != _terminal_archive_accepted_registration_snapshot_sha256(request)):
        raise ValueError("已接受终态登记的精确身份或请求快照摘要不符")
    request, _summary = _validate_bound_archive_request_payload(request_path, request, repo=repo, session_id=session_id)
    return request


def _resume_accepted_terminal_archive_registration(index_path: Path, *, repo: Path, session_id: str) -> dict[str, Any]:
    from dispatcher_writer_execution_evidence import dispatcher_writer_execution_terminal_archive_admission_lock

    if index_path.is_symlink():
        raise ValueError("已接受终态登记索引不能是符号链接")
    initial = read_json_object(index_path)
    if not isinstance(initial.get("run_dir"), str):
        raise ValueError("已接受终态登记缺少精确run目录")
    run_dir = Path(initial["run_dir"])
    if index_path.resolve() != _terminal_archive_registration_index_path(run_dir=run_dir, repo=repo, session_id=session_id).resolve():
        raise ValueError("已接受终态登记的索引路径归属不符")
    request_path = run_dir / "artifacts" / ARCHIVE_REQUEST_FILENAME
    with dispatcher_writer_execution_terminal_archive_admission_lock(run_dir), review_run_metadata_lock(run_dir):
        accepted = read_accepted_terminal_archive_registration(run_dir=run_dir, repo=repo, session_id=session_id)
        if accepted is None:
            request, _summary = _read_bound_archive_request(request_path, repo=repo, session_id=session_id)
        elif request_path.exists() or request_path.is_symlink():
            request, _summary = _read_bound_archive_request(request_path, repo=repo, session_id=session_id, expected=accepted)
            # 登记后的禁止只增强；快照不能覆盖锁外追加的successor、计数或归档进度。
            latest_event = request.setdefault("event", {})
            policy_strengthened = False
            for name in ("suppress_review_validate_fix", "review_validate_fix_suppressed"):
                if accepted.get("event", {}).get(name) is True and latest_event.get(name) is not True:
                    latest_event[name] = True
                    policy_strengthened = True
            if policy_strengthened:
                atomic_write_json(request_path, request)
        else:
            atomic_write_json(request_path, accepted)
    # 两把短锁全部释放，再按原顺序获取archive-attempt和共同许可锁。
    return _resume_archive(request_path, repo=repo, session_id=session_id)


def _commit_archive_recovery_fields(
    request_path: Path, expected: dict[str, Any], updates: dict[str, Any], *, clear: tuple[str, ...] = (),
) -> dict[str, Any]:
    """只提交本执行者拥有的字段，保留锁外期间新增的关联与持久事实。"""
    if (set(updates) | set(clear)) - ARCHIVE_REQUEST_RECOVERY_OWNED_FIELDS:
        raise ValueError("归档恢复不能写入其他拥有者的字段")
    with review_run_metadata_lock(Path(expected["run_dir"])):
        latest, _summary = _read_bound_archive_request(request_path, repo=Path(expected["repo_realpath"]),
                                                      session_id=expected["session_id"], expected=expected)
        latest.update(updates)
        for key in clear:
            latest.pop(key, None)
        atomic_write_json(request_path, latest)
        return latest


def _archive_analysis_recovery_fields(
    request: dict[str, Any], summary: dict[str, Any], finalization: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    local = dict(request)
    try:
        _surface_terminal_archive_analysis(local, summary, finalization)
    except Exception as exc:
        local["analysis_recovery_condition"] = f"分析证据或启动入口恢复后自动重验：{type(exc).__name__}: {exc}"
    if "analysis_recovery_condition" in local:
        return {"analysis_recovery_condition": local["analysis_recovery_condition"]}, ()
    return {}, ("analysis_recovery_condition",)


def _reconcile_archive_finalization(
    request_path: Path, request: dict[str, Any], summary: dict[str, Any], evidence: dict[str, Any],
) -> dict[str, Any] | None:
    first_terminal = evidence["terminal_status"]
    completion = evidence["completion"]
    if first_terminal and first_terminal != request["decision_kind"]:
        updates = {"preserved_terminal_status": first_terminal}
        if not completion:
            return _commit_archive_recovery_fields(request_path, request, {**updates,
                "status": "waiting_for_evidence", "recovery_condition_code": "first_terminal_finalization_incomplete",
                "recovery_condition": "首次终态的精确 finalize 完成回执恢复可读后自动对账；不执行旧请求的归档"})
        return _commit_archive_recovery_fields(request_path, request, {**updates,
            "status": "already_terminal", "review_completed": first_terminal == "handoff", "reconciled_at": _now(),
            "finalize_completion_evidence": {key: completion[key] for key in ("path", "sha256")},
            "last_error": None, "recovery_condition": None, "recovery_condition_code": None},
            clear=("analysis_recovery_condition",))
    if completion:
        updates, clear = _archive_analysis_recovery_fields(request, summary, completion["record"])
        return _commit_archive_recovery_fields(request_path, request, {**updates,
            "status": "archived", "archived_at": completion["record"]["completed_at"], "reconciled_at": _now(),
            "finalize_completion_evidence": {key: completion[key] for key in ("path", "sha256")},
            "review_completed": False, "last_error": None, "recovery_condition": None, "recovery_condition_code": None},
            clear=clear)
    return None


def _resume_archive(request_path: Path, *, repo: Path, session_id: str) -> dict[str, Any]:
    initial = read_json_object(request_path)
    if initial.get("repo_realpath") != str(repo.resolve()) or initial.get("session_id", "") != session_id or request_path.is_symlink():
        raise ValueError("终态归档的仓库或会话归属不符")
    with (request_path.parent / ".terminal-archive-attempt.lock").open("a+b") as owner:
        try:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "waiting_for_evidence", "recovery_condition": "本 run 的归档执行者退出"}
        try:
            return _resume_archive_owned(request_path, repo=repo, session_id=session_id)
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def _resume_archive_owned(request_path: Path, *, repo: Path, session_id: str) -> dict[str, Any]:
    request, summary = _read_bound_archive_request(request_path, repo=repo, session_id=session_id)
    run_dir = Path(request["run_dir"])
    try:
        evidence = read_review_run_finalization_evidence(run_dir=run_dir, run_id=request["run_id"], repo=repo)
    except Exception as exc:
        return _commit_archive_recovery_fields(request_path, request, {
            "status": "waiting_for_evidence", "last_error": f"{type(exc).__name__}: {exc}",
            "recovery_condition_code": "finalize_completion_evidence_unavailable",
            "recovery_condition": "精确首次终态及 finalize 完成回执恢复可读后自动对账；不重复归档副作用"})
    reconciled = _reconcile_archive_finalization(request_path, request, summary, evidence)
    if reconciled:
        return reconciled
    if request.get("status") in ("archived", "already_terminal"):
        if request.get("analysis_recovery_condition"):
            updates, clear = _archive_analysis_recovery_fields(request, summary, summary.get("finalize") or {})
            return _commit_archive_recovery_fields(request_path, request, updates, clear=clear)
        return request
    from dispatcher_writer_execution_evidence import (
        dispatcher_writer_execution_terminal_archive_admission_lock,
        recover_unattempted_dispatcher_preparations_with_admission_lock_held,
    )

    # 请求是持久终态意图；共同许可锁内核验writer并提交archiving，随后释放再做长归档。
    with dispatcher_writer_execution_terminal_archive_admission_lock(run_dir):
        request, summary = _read_bound_archive_request(request_path, repo=repo, session_id=session_id, expected=request)
        writer_evidence: list[dict[str, Any]] = []
        try:
            recover_unattempted_dispatcher_preparations_with_admission_lock_held(run_dir / "artifacts/reviewers/.dispatch-thread.status.json")
            writers_active = _writers_still_active(run_dir, writer_evidence)
        except Exception as exc:
            return _commit_archive_recovery_fields(request_path, request, {
                "status": "waiting_for_evidence", "waited_for_writer_evidence": True, "writer_evidence": writer_evidence,
                "last_error": f"{type(exc).__name__}: {exc}", "recovery_condition_code": "writer_evidence_unavailable",
                "recovery_condition": "本 run 的写入者状态或等价交付/原生退出证据恢复可读后自动重验"})
        if writers_active:
            return _commit_archive_recovery_fields(request_path, request, {
                "status": "waiting_for_evidence", "waited_for_writer_evidence": True, "writer_evidence": writer_evidence,
                "recovery_condition_code": "writer_delivery_or_exit_evidence_required",
                "recovery_condition": "本 run 的 dispatcher 或 fixer 有可信交付/退出证据后自动重验"})
        generation = _archive_evidence_generation(request)
        count = request.get("attempt_count", 0) if generation == request.get("evidence_generation_sha256") else 0
        if count >= MAX_ARCHIVE_ATTEMPTS_PER_EVIDENCE_GENERATION:
            return _commit_archive_recovery_fields(request_path, request, {
                "status": "waiting_for_evidence", "writer_evidence": writer_evidence,
                "recovery_condition_code": "archive_attempt_budget_exhausted",
                "recovery_condition": "精确 finalize 完成回执可读时先对账；稳定输入可读性或运行版本改变后刷新三次执行预算"})
        request = _commit_archive_recovery_fields(request_path, request, {
            "attempt_count": count + 1, "evidence_generation_sha256": generation, "status": "archiving",
            "last_attempt_at": _now(), "writer_evidence": writer_evidence})
    # 长时间归档及后台启动在元数据锁外；finalize 自身拥有同 run 的执行互斥。
    try:
        event = {**request.get("event", {}), "review_run_terminal_observed_at": request["requested_at"],
                 "review_run_terminal_reason": request["reason"],
                 "review_run_resume_failed_terminal_archive_stages": True}
        if count > 0:
            event["review_run_recovering_incomplete_terminal_archive"] = True
        if request.get("waited_for_writer_evidence"):
            event["review_run_skip_live_workspace_snapshot"] = True
        finalization = finalize_run(run_dir=run_dir, event=event, decision_kind=request["decision_kind"])
        if (finalization.get("decision_kind", request["decision_kind"]) != request["decision_kind"]
                or finalization.get("review_run_terminal_status", request["decision_kind"]) != request["decision_kind"]):
            evidence = read_review_run_finalization_evidence(run_dir=run_dir, run_id=request["run_id"], repo=repo)
            reconciled = _reconcile_archive_finalization(request_path, request, summary, evidence)
            if reconciled:
                return reconciled
            raise ValueError("finalize 返回的终态不属于当前请求，等待精确完成证据")
        if finalization.get("errors"):
            raise RuntimeError("终态归档仍有失败阶段：" + ", ".join(str(error.get("stage")) for error in finalization["errors"]))
        updates, clear = _archive_analysis_recovery_fields(request, summary, finalization)
        updates.update({"status": "archived", "archived_at": _now(), "last_error": None,
                        "review_completed": False, "recovery_condition": None, "recovery_condition_code": None})
    except Exception as exc:
        updates, clear = {"status": "waiting_for_evidence", "last_error": f"{type(exc).__name__}: {exc}",
            "recovery_condition_code": "archive_stage_evidence_unavailable",
            "recovery_condition": "归档输入重新可读或运行版本修复后自动重验；每个证据代次最多三次"}, ()
    return _commit_archive_recovery_fields(request_path, request, updates, clear=clear)


def request_review_run_terminal_archive(
    *, run_dir: Path, decision_kind: str, reason: str, event: dict[str, Any] | None = None,
    defer_resume: bool = False,
) -> dict[str, Any]:
    if decision_kind not in UNSUCCESSFUL_TERMINAL_KINDS:
        raise ValueError("准备终态归档不能声明审查成功")
    if run_dir.is_symlink() or (run_dir / "summary.json").is_symlink():
        raise ValueError("终态归档的 run 目录及 summary 不能是符号链接")
    run_dir = run_dir.expanduser().resolve()
    summary = read_json_object(run_dir / "summary.json")
    existing_finalization = summary.get("finalize") or {}
    repo = Path(summary["repo"]).expanduser().resolve()
    event = event or {}
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    if contract_path.is_symlink():
        raise ValueError("终态归档的固定 scope contract 不能是符号链接")
    contract = read_json_object(contract_path)
    bound_session_id = _validate_archive_run_contract_identity(
        run_dir, summary, contract, requested_session=event.get("session_id"))
    if summary.get("review_run_terminal_status") == "handoff" or (
        isinstance(existing_finalization, dict) and existing_finalization.get("decision_kind")
        and existing_finalization["decision_kind"] not in UNSUCCESSFUL_TERMINAL_KINDS
    ):
        return {"status": "already_terminal", "run_id": summary["run_id"],
                "terminal_status": summary.get("review_run_terminal_status"),
                "reason": "保留原轮次的既有完成事实，后续取消不改写它"}
    session_id = str(event.get("session_id") or bound_session_id or "")
    if bound_session_id and bound_session_id != session_id:
        raise ValueError("终态归档的候选会话身份不符")
    event = {**event, "session_id": session_id}
    harness = read_json_object(run_dir / "artifacts/inputs/main-harness.json")
    if not event.get("transcript_path") and isinstance(harness.get("transcript"), str):
        event["transcript_path"] = harness["transcript"]
    if summary.get("session_id") and summary["session_id"] != session_id:
        raise ValueError("终态归档的会话身份不符")
    request_path = run_dir / "artifacts" / ARCHIVE_REQUEST_FILENAME
    from dispatcher_writer_execution_evidence import dispatcher_writer_execution_terminal_archive_admission_lock

    # 不可反向持run metadata等待共同许可锁；登记后退出两把短锁再进入resume。
    with dispatcher_writer_execution_terminal_archive_admission_lock(run_dir), review_run_metadata_lock(run_dir):
        if request_path.is_symlink():
            raise ValueError("终态归档请求不能是符号链接")
        accepted_registration_request = None
        if not request_path.exists():
            accepted_registration_request = read_accepted_terminal_archive_registration(run_dir=run_dir, repo=repo, session_id=session_id)
        if not request_path.exists() and accepted_registration_request is None:
            request = {
                "schema_version": 1, "run_id": summary["run_id"], "run_dir": str(run_dir),
                "repo_realpath": str(repo), "session_id": session_id, "decision_kind": decision_kind,
                "reason": reason, "requested_at": _now(), "status": "pending", "attempt_count": 0,
                "event": {key: event[key] for key in ("cwd", "session_id", "transcript_path", "session_path",
                          "conversation_path", "log_path", "session_file", "kanban_task_id",
                          "suppress_review_validate_fix", "review_validate_fix_suppressed") if key in event},
                "initial_resume_deferred": defer_resume,
                "waited_for_writer_evidence": False,
                "scope_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest() if contract_path.is_file() else None,
            }
        else:
            if accepted_registration_request is not None:
                request = accepted_registration_request
            else:
                request, _summary = _read_bound_archive_request(request_path, repo=repo, session_id=session_id)
            # 新的显式禁止只会加强既有策略，普通恢复不能清掉它。
            saved_event = request.setdefault("event", {})
            for name in ("suppress_review_validate_fix", "review_validate_fix_suppressed"):
                if event.get(name) is True:
                    saved_event[name] = True
        index_path = _terminal_archive_registration_index_path(run_dir=run_dir, repo=repo, session_id=session_id)
        if index_path.is_symlink():
            raise ValueError("终态归档登记索引不能是符号链接")
        # 此原子提交是接受点；之后canonical只是可由正式后台入口重建的投影。
        atomic_write_json(index_path, {"kind": "accepted_terminal_archive_request_registration", "schema_version": 1,
            "request_path": str(request_path), "run_id": request["run_id"], "run_dir": str(run_dir),
            "repo_realpath": str(repo), "session_id": session_id, "accepted_request_snapshot": request,
            "accepted_request_snapshot_sha256": _terminal_archive_accepted_registration_snapshot_sha256(request)})
        atomic_write_json(request_path, request)
    if defer_resume:
        return request
    return _resume_archive(request_path, repo=repo, session_id=session_id)


def resume_pending_review_run_terminal_archives(*, repo: Path, session_id: str) -> list[dict[str, Any]]:
    directory = _pending_archive_directory(repo, session_id)
    if not directory.is_dir():
        return []
    results = []
    paths = sorted(path for path in directory.glob("*.json") if path.name != "recovery-scan-cursor.json")
    cursor_path = directory / "recovery-scan-cursor.json"
    cursor = read_json_object(cursor_path).get("last_index_filename", "")
    paths = [path for path in paths if path.name > cursor] + [path for path in paths if path.name <= cursor]
    for path in paths[:8]:
        try:
            if path.is_symlink():
                raise ValueError("终态归档索引不能为符号链接")
            index = read_json_object(path)
            request_path = Path(index["request_path"])
            if index.get("kind") == "accepted_terminal_archive_request_registration":
                result = _resume_accepted_terminal_archive_registration(path, repo=repo, session_id=session_id)
            elif read_json_object(request_path).get("run_id") != index.get("run_id"):
                raise ValueError("终态归档索引与请求身份不符")
            elif index.get("kind") == "native_preparation_interruption":
                result = _resume_native_preparation_interruption(request_path, repo=repo, session_id=session_id)
            elif index.get("kind") == "analysis_completion":
                result = _resume_analysis_request(request_path, repo=repo, session_id=session_id)
            else:
                result = _resume_archive(request_path, repo=repo, session_id=session_id)
            results.append(result)
            if result.get("status") in ("archived", "completed", "already_terminal") and not result.get("analysis_recovery_condition"):
                path.unlink(missing_ok=True)
        except Exception as exc:
            results.append({"status": "waiting_for_evidence", "index_path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        atomic_write_json(cursor_path, {"last_index_filename": path.name})
    return results


def record_native_preparation_interruption_recovery_index(observation_path: Path) -> Path:
    """先保住原生中断意图；contract 暂时不可读时仍能由合法扫描自动接续。"""
    observation = read_json_object(observation_path)
    repo, session_id = Path(observation["repo"]), observation["session_id"]
    index = _pending_archive_directory(repo, session_id) / (
        "native-preparation-interruption-" + hashlib.sha256(str(observation_path.resolve()).encode()).hexdigest() + ".json")
    atomic_write_json(index, {"kind": "native_preparation_interruption", "request_path": str(observation_path),
                              "run_id": observation["run_id"]})
    return index


def _resume_native_preparation_interruption(observation_path: Path, *, repo: Path, session_id: str) -> dict[str, Any]:
    observation = read_json_object(observation_path)
    run_dir = Path(observation["run_dir"])
    if (observation_path.is_symlink() or observation_path.resolve()
            != run_dir / "artifacts/preparation-native-interruption-observation.json"
            or observation.get("repo") != str(repo.resolve()) or observation.get("session_id") != session_id
            or observation.get("native_exception_kind") != "KeyboardInterrupt"):
        raise ValueError("原生 preparation 中断观察的路径、仓库或会话归属不符")
    summary = read_json_object(run_dir / "summary.json")
    if (summary.get("run_id") != observation.get("run_id") or not isinstance(summary.get("repo"), str)
            or Path(summary["repo"]).resolve() != repo.resolve()):
        raise ValueError("原生 preparation 中断观察与正式 run 身份不符")
    outcome = request_review_run_terminal_archive(run_dir=run_dir, decision_kind="interrupted",
        reason="preparation 实际执行收到 KeyboardInterrupt；根据持久原生观察恢复终态请求",
        event={**observation.get("event", {}), "cwd": str(repo), "session_id": session_id}, defer_resume=True)
    if outcome.get("status") == "already_terminal":
        return outcome
    return _resume_archive(run_dir / "artifacts" / ARCHIVE_REQUEST_FILENAME, repo=repo, session_id=session_id)


def record_review_run_successor(*, source_run_dir: Path, successor_run_dir: Path, session_id: str) -> dict[str, Any] | None:
    """在新 run 已实际准备后关联历史终态，不把尚未发生的派发写成完成事实。"""
    request_path = source_run_dir / "artifacts" / ARCHIVE_REQUEST_FILENAME
    if not request_path.is_file():
        return None
    successor = read_json_object(successor_run_dir / "summary.json")
    with review_run_metadata_lock(source_run_dir):
        request, _summary = _read_bound_archive_request(request_path, repo=Path(successor["repo"]), session_id=session_id)
        if successor_run_dir.resolve() == source_run_dir.resolve():
            raise ValueError("后续 run 必须具有独立身份")
        entries = request.setdefault("successor_review_runs", [])
        if not any(entry.get("run_id") == successor["run_id"] for entry in entries):
            entries.append({"run_id": successor["run_id"], "run_dir": str(successor_run_dir.resolve()), "linked_at": _now()})
        atomic_write_json(request_path, request)
        return entries[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复精确仓库和会话的非成功 RVF 终态归档。")
    parser.add_argument("operation", nargs="?", default="resume", choices=("request", "resume"),
                        help="request 显式登记真实终止事件；resume 只恢复已有请求。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-dir", help="request 必须提供实际已创建的精确 run 目录。")
    parser.add_argument("--decision-kind", choices=sorted(UNSUCCESSFUL_TERMINAL_KINDS))
    parser.add_argument("--reason", help="真实终止决定或已观测中断的具体原因。")
    parser.add_argument("--defer-resume", action="store_true", help="只登记持久请求，留待后续合法恢复入口接续。")
    arguments = parser.parse_args()
    repo = Path(arguments.repo).expanduser().resolve()
    if arguments.operation == "request":
        if not arguments.run_dir or not arguments.decision_kind or not arguments.reason or not arguments.reason.strip():
            parser.error("request 必须提供 --run-dir、--decision-kind 和非空 --reason")
        run_dir = Path(arguments.run_dir).expanduser()
        if run_dir.is_symlink() or (run_dir / "summary.json").is_symlink():
            parser.error("request 的 run 目录及 summary 不能是符号链接")
        summary = read_json_object(run_dir / "summary.json")
        contract_path = run_dir / "artifacts/inputs/scope.contract.json"
        if contract_path.is_symlink():
            parser.error("request 的固定 scope contract 不能是符号链接")
        contract = read_json_object(contract_path)
        snapshot = contract.get("review_candidate_snapshot") or contract.get("review_start_boundary_receipt") or {}
        sessions = [value for value in (summary.get("session_id"), snapshot.get("session_id")) if value]
        if (not summary.get("run_id") or not isinstance(summary.get("repo"), str)
                or Path(summary["repo"]).resolve() != repo or not sessions
                or any(value != arguments.session_id for value in sessions)
                or summary.get("run_dir") and Path(summary["run_dir"]).resolve() != run_dir.resolve()):
            parser.error("request 的 run、仓库或已有会话归属不符；不能新增或猜测身份")
        result = request_review_run_terminal_archive(run_dir=run_dir, decision_kind=arguments.decision_kind,
            reason=arguments.reason, event={"cwd": str(repo), "session_id": arguments.session_id},
            defer_resume=arguments.defer_resume)
    else:
        if any((arguments.run_dir, arguments.decision_kind, arguments.reason, arguments.defer_resume)):
            parser.error("resume 只接受 --repo 与 --session-id，不推造新的终止事件")
        result = resume_pending_review_run_terminal_archives(repo=repo, session_id=arguments.session_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
