#!/usr/bin/env python3
"""执行后台复盘并校验正式归档；退出码同时约束完成计数与线程重试。"""

from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from pathlib import Path
import subprocess
import sys
from typing import Any

from analysis_artifacts import ANALYSIS_SCHEMA_VERSION, discover_inputs
from rvf_detached_thread import _atomic_write_json, _iso_now
import _rvf_pyroot  # noqa: F401
from core.run_ledger.review_run_lifecycle import atomic_write_json, read_json_object, review_run_metadata_lock


ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME = "analysis-causality-before-first-agent-execution.json"
ANALYSIS_ATTEMPT_BUDGET_FILENAME = "analysis-execution-attempt-budget.json"
FORMAL_OUTPUT_IO_READABILITY_RECOVERY_CONDITION = "正式输出恢复可读后只重验完成与计数；不启动 Analyze，不消耗剩余尝试"


def _original_analysis_input_access_evidence(run_directory: Path) -> list[Any]:
    """只观察原始/冻结输入的访问条件，不以模型输出内容授予新额度。"""
    paths = [run_directory / "artifacts" / ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME,
             run_directory / "artifacts/analysis-frozen-prompt-before-first-agent-execution.md"]
    evidence: list[Any] = []
    try:
        inputs = discover_inputs(run_directory)
        paths.extend(path for path in (
            inputs.summary_json, inputs.handoff_md, inputs.workspace_diff_json,
            inputs.workspace_diff_patch, inputs.trajectory_jsonl, inputs.trajectory_index_json,
            inputs.rvf_rollout_jsonl,
        ) if path is not None)
        paths.extend(inputs.reviewer_results)
        paths.extend(inputs.reviewer_trajectories)
        if inputs.pre_rvf_dir is not None:
            paths.extend(inputs.pre_rvf_dir / name for name in (
                "manifest.json", "trajectory.jsonl", "trajectory.index.json", "rollout.jsonl",
            ))
        for subagent in inputs.subagents:
            if subagent.trajectory_jsonl is not None:
                paths.extend([subagent.trajectory_jsonl, subagent.trajectory_jsonl.parent / "manifest.json"])
    except OSError as exc:
        evidence.append(("original_input_discovery", type(exc).__name__, exc.errno))
    for path in sorted(set(paths), key=str):
        try:
            file_type = stat.S_IFMT(path.stat().st_mode)
            access_evidence: list[Any] = [str(path.resolve()), file_type, os.access(path, os.R_OK)]
            if file_type == stat.S_IFREG:
                try:
                    with path.open("rb"):
                        pass
                    access_evidence.append("readable")
                except OSError as exc:
                    access_evidence.append(("unreadable", type(exc).__name__, exc.errno))
            evidence.append(access_evidence)
        except OSError as exc:
            evidence.append((str(path), None, type(exc).__name__, exc.errno))
    return evidence


def automatic_analysis_evidence_generation(run_directory: Path, agent_argv: list[str]) -> str:
    """版本、入口、登录和原始输入访问条件可重验；LLM 产物变化不重置预算。"""
    evidence: list[Any] = [hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), agent_argv,
                           _original_analysis_input_access_evidence(run_directory)]
    candidates = [Path(argument) for argument in agent_argv if len(argument) < 1024 and not argument.startswith("-")]
    execution_entry_path = None
    if agent_argv:
        execution_entry_path = Path(shutil.which(agent_argv[0]) or agent_argv[0])
        candidates.append(execution_entry_path)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    candidates.extend([home / "auth.json", Path.home() / ".claude/.credentials.json"])
    for path in dict.fromkeys(candidates):
        try:
            path_status = path.stat()
            file_evidence: list[Any] = [str(path.resolve()), stat.S_IFMT(path_status.st_mode),
                                        os.access(path, os.R_OK)]
            if path == execution_entry_path:
                file_evidence.append(os.access(path, os.X_OK))
            if stat.S_ISREG(path_status.st_mode):
                content_sha256 = hashlib.sha256()
                try:
                    # touch、原样重写与 inode 替换不产生新预算；不持久保存凭证内容。
                    with path.open("rb") as content_stream:
                        for content_block in iter(lambda: content_stream.read(128 * 1024), b""):
                            content_sha256.update(content_block)
                    file_evidence.append(content_sha256.hexdigest())
                except OSError as exc:
                    file_evidence.append(("unreadable", type(exc).__name__, exc.errno))
            evidence.append(file_evidence)
        except OSError as exc:
            evidence.append((str(path), None, type(exc).__name__, exc.errno))
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def _claim_analysis_attempt(run_directory: Path, generation: str, limit: int) -> str | None:
    path = run_directory / "artifacts" / ANALYSIS_ATTEMPT_BUDGET_FILENAME
    if path.is_symlink():
        raise ValueError("分析执行预算不能为符号链接")
    with review_run_metadata_lock(run_directory):
        budget = read_json_object(path)
        if budget and budget.get("run_dir") != str(run_directory.resolve()):
            raise ValueError("分析执行预算的 run 身份不符")
        if budget.get("evidence_generation_sha256") != generation:
            budget = {**budget, "run_dir": str(run_directory.resolve()), "schema_version": 1,
                      "evidence_generation_sha256": generation, "attempts_started": 0,
                      "attempt_history": budget.get("attempt_history", [])}
        if budget["attempts_started"] >= limit:
            return None
        attempt_id = f"analysis-attempt-{secrets.token_hex(8)}"
        budget["attempts_started"] += 1
        budget["attempt_history"].append({"attempt_id": attempt_id, "started_at": _iso_now(),
            "evidence_generation_sha256": generation, "status": "started"})
        atomic_write_json(path, budget)
        return attempt_id


def _finish_analysis_attempt(run_directory: Path, attempt_id: str, status: dict[str, Any]) -> None:
    with review_run_metadata_lock(run_directory):
        path = run_directory / "artifacts" / ANALYSIS_ATTEMPT_BUDGET_FILENAME
        budget = read_json_object(path)
        for attempt in budget["attempt_history"]:
            if attempt["attempt_id"] == attempt_id:
                attempt.update({"finished_at": _iso_now(), "status": status["formal_analysis_completion_status"],
                    "returncode": status["returncode"], "error": status["error"],
                    "agent_process_wall_seconds": status["agent_process_wall_seconds"]})
        atomic_write_json(path, budget)


def formal_analysis_directory(run_directory: Path) -> Path:
    """正式目录及输出不得经符号链接跳到其他位置。"""
    expected_directory = run_directory.expanduser().resolve() / "artifacts" / "analysis"
    if expected_directory.resolve() != expected_directory:
        raise ValueError("正式 analysis 目录存在符号链接跳转")
    for filename in ("summary.md", "causality.json", ".analyze-thread.status.json"):
        expected_path = expected_directory / filename
        if expected_path.resolve() != expected_path:
            raise ValueError(f"正式产物存在符号链接跳转：{filename}")
    return expected_directory


def validate_formal_analysis_outputs(
    run_directory: Path, *, recorded_causality: dict[str, Any] | None = None
) -> dict[str, Any]:
    """校验磁盘正式产物；空 patch 归因和未变化的 JSON 都是合法结果。"""
    errors: list[str] = []
    output_sha256: dict[str, str] = {}
    unavailable_output_io_evidence: list[dict[str, Any]] = []
    try:
        analysis_directory = formal_analysis_directory(run_directory)
    except (OSError, ValueError) as exc:
        return {"valid": False, "errors": [str(exc)], "output_sha256": {}, "output_io_evidence_unavailable": []}

    contents: dict[str, str] = {}
    for filename in ("summary.md", "causality.json"):
        try:
            raw = (analysis_directory / filename).read_bytes()
            contents[filename] = raw.decode("utf-8")
            output_sha256[filename] = hashlib.sha256(raw).hexdigest()
        except OSError as exc:
            errors.append(f"{filename} 无法读取：{type(exc).__name__}")
            # 未生成文件或文件形态错误属于产物未完成；权限/实际 I/O 故障则先等机器证据恢复。
            if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.EISDIR):
                unavailable_output_io_evidence.append({"filename": filename,
                    "error_type": type(exc).__name__, "errno": exc.errno})
        except UnicodeError as exc:
            errors.append(f"{filename} 无法读取：{type(exc).__name__}")
    summary = contents.get("summary.md")
    if summary is not None:
        if not summary.strip():
            errors.append("summary.md 为空")
        if "TODO(rvf-analyze)" in summary:
            errors.append("summary.md 仍有待补全占位符")

    if "causality.json" in contents:
        try:
            causality = json.loads(contents["causality.json"])
        except ValueError:
            errors.append("causality.json 不是合法 JSON")
        else:
            if not isinstance(causality, dict):
                errors.append("causality.json 必须是对象")
            else:
                if type(causality.get("schema_version")) is not int or (
                    causality["schema_version"] != ANALYSIS_SCHEMA_VERSION
                ):
                    errors.append("causality.json schema_version 不匹配")
                if causality.get("run_id") != run_directory.resolve().name:
                    errors.append("causality.json run_id 不匹配")
                issues = causality.get("issues")
                patches = causality.get("patches")
                if not isinstance(issues, list) or not all(isinstance(item, dict) for item in issues):
                    errors.append("causality.json issues 必须是对象列表")
                elif not isinstance(patches, list) or not all(isinstance(item, dict) for item in patches):
                    errors.append("causality.json patches 必须是对象列表")
                else:
                    available_call_ids = {
                        patch["call_id"] for patch in patches
                        if isinstance(patch.get("call_id"), str) and patch["call_id"]
                    }
                    for issue in issues:
                        candidates = issue.get("candidate_patch_call_ids")
                        if not isinstance(candidates, list) or not all(
                            isinstance(call_id, str) and call_id in available_call_ids
                            for call_id in candidates
                        ):
                            errors.append("causality.json 存在非法 candidate_patch_call_ids")
                            break
                if recorded_causality is not None:
                    def immutable_facts(payload: dict[str, Any]) -> dict[str, Any]:
                        facts = copy.deepcopy(payload)
                        if isinstance(facts.get("issues"), list):
                            for issue in facts["issues"]:
                                if isinstance(issue, dict):
                                    issue.pop("candidate_patch_call_ids", None)
                        return facts

                    if immutable_facts(causality) != immutable_facts(recorded_causality):
                        errors.append("causality.json 改写了候选 patch 归因以外的已记录事实")
    return {"valid": not errors, "errors": errors, "output_sha256": output_sha256,
            "output_io_evidence_unavailable": unavailable_output_io_evidence}


def freeze_or_load_original_causality(
    analysis_directory: Path, status: dict[str, Any],
) -> dict[str, Any] | None:
    """首次执行前冻结事实；快照位于 agent 的 analysis 写权限目录之外。"""
    snapshot_path = analysis_directory.parent / ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME
    if snapshot_path.resolve() != snapshot_path:
        raise ValueError("原始 causality 快照存在符号链接跳转")
    if not snapshot_path.exists():
        if status.get("formal_analysis_causality_snapshot_required"):
            raise ValueError("原始 causality 快照丢失，不能重新采信当前归档")
        try:
            original_causality = json.loads(
                (analysis_directory / "causality.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            original_causality = None
        if not isinstance(original_causality, dict):
            original_causality = None
        # 无法取得初始事实也必须留下记录，避免下一轮将 agent 产物确认为初始事实。
        _atomic_write_json(snapshot_path, {"recorded_causality": original_causality})

    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    expected_sha256 = status.get("formal_analysis_causality_snapshot_sha256")
    if expected_sha256 is not None and expected_sha256 != snapshot_sha256:
        raise ValueError("原始 causality 快照摘要不匹配")
    snapshot = json.loads(snapshot_bytes)
    if not isinstance(snapshot, dict) or "recorded_causality" not in snapshot:
        raise ValueError("原始 causality 快照格式不匹配")
    recorded_causality = snapshot["recorded_causality"]
    if recorded_causality is not None and not isinstance(recorded_causality, dict):
        raise ValueError("原始 causality 快照事实必须是对象")
    # 在线程或机器于 agent 执行期间退出时，后续重派仍须保留这份事实基线。
    status.update({
        "formal_analysis_causality_snapshot_required": True,
        "formal_analysis_causality_snapshot_sha256": snapshot_sha256,
    })
    _atomic_write_json(analysis_directory / ".analyze-thread.status.json", status)
    return recorded_causality


def execute_and_verify_analysis(run_directory: Path, agent_argv: list[str]) -> int:
    """同 run 只允许一个分析执行者；机器重启保留尝试历史并释放内核锁。"""
    import fcntl

    formal_analysis_directory(run_directory)
    with (run_directory / "artifacts/.analysis-execution.lock").open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        try:
            return _execute_and_verify_analysis_owned(run_directory, agent_argv)
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def _record_verified_analysis_completion_count(
    run_directory: Path, status_path: Path, status: dict[str, Any], *, emit_result: bool = True,
) -> dict[str, Any]:
    """每次正式重验后幂等补记；计数故障与已经落盘的完成事实分开。"""
    try:
        from agentic_initiation_guardian_dispatch import record_analyze_completion

        result = record_analyze_completion(run_dir=str(run_directory.resolve()))
    except Exception as exc:
        result = {"status": "guardian_record_failed", "error": f"{type(exc).__name__}: {exc}"}
    recorded = result.get("status") in {"recorded", "already_recorded"}
    status.update({
        "formal_analysis_completion_counter_status": "recorded" if recorded else "pending",
        "formal_analysis_completion_counter_observed_at": _iso_now(),
        "formal_analysis_completion_counter_recovery_condition": None if recorded else (
            "正式归档再次通过验证且 guardian 计数存储可用后，按精确 run 幂等补记，无需重跑分析进程"
        ),
    })
    if not recorded:
        status.setdefault("formal_analysis_completion_counter_failure_history", []).append({
            "observed_at": status["formal_analysis_completion_counter_observed_at"],
            "status": result.get("status"), "error": result.get("error"),
        })
    try:
        _atomic_write_json(status_path, status)
    except Exception as exc:
        # guardian 的原子认领仍是唯一计数依据；本地回执不可写时，下次重验可安全重放。
        if emit_result:
            print(f"复盘完成计数回执暂不可写：{type(exc).__name__}: {exc}", file=sys.stderr)
    if emit_result:
        print(json.dumps(result, ensure_ascii=False))
    return result


def _retain_formal_output_io_recovery_condition(
    status_path: Path, status: dict[str, Any], validation: dict[str, Any],
) -> bool:
    unavailable = validation["output_io_evidence_unavailable"]
    status["formal_analysis_output_io_evidence_unavailable"] = unavailable
    status["formal_analysis_output_recovery_condition"] = (
        FORMAL_OUTPUT_IO_READABILITY_RECOVERY_CONDITION if unavailable else None
    )
    if unavailable:
        status["formal_analysis_validation_errors"] = validation["errors"]
        _atomic_write_json(status_path, status)
    return bool(unavailable)


def _reconcile_formal_analysis_completion_owned(
    run_directory: Path, status_path: Path, status: dict[str, Any],
    recorded_causality: dict[str, Any] | None, *, emit_result: bool,
) -> bool:
    """已持有分析互斥时，只重验原进程产物和精确 run 计数，不执行 agent。"""
    if recorded_causality is None:
        return False
    if status.get("formal_analysis_completion_status") == "completed":
        verified = validate_formal_analysis_outputs(run_directory, recorded_causality=recorded_causality)
        if _retain_formal_output_io_recovery_condition(status_path, status, verified):
            return False
        if verified["valid"] and verified["output_sha256"] == status.get("formal_analysis_output_sha256"):
            _record_verified_analysis_completion_count(run_directory, status_path, status, emit_result=emit_result)
            return True
    elif status.get("agent_process_returncode") == 0:
        verified = validate_formal_analysis_outputs(run_directory, recorded_causality=recorded_causality)
        if _retain_formal_output_io_recovery_condition(status_path, status, verified):
            return False
        if verified["valid"]:
            # 原进程已成功，正式输出恢复可读/合法后只重验，不刷新预算或重跑模型。
            status.update({"formal_analysis_completion_status": "completed", "returncode": 0,
                           "formal_analysis_validation_errors": [], "error": None,
                           "formal_analysis_output_sha256": verified["output_sha256"],
                           "formal_analysis_verified_at": _iso_now(),
                           "formal_analysis_completion_recovery_reason": "正式输出通过独立重验"})
            _atomic_write_json(status_path, status)
            _record_verified_analysis_completion_count(run_directory, status_path, status, emit_result=emit_result)
            return True
    return False


def reconcile_formal_analysis_completion_without_agent_execution(run_directory: Path) -> dict[str, Any]:
    """供 claim/扫描静默补证与计数；没有 agent 参数，也没有分析进程启动路径。"""
    import fcntl

    run_directory = run_directory.resolve()
    status: dict[str, Any] = {}
    try:
        analysis_directory = formal_analysis_directory(run_directory)
        status_path = analysis_directory / ".analyze-thread.status.json"
        with (run_directory / "artifacts/.analysis-execution.lock").open("a+b") as owner:
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "waiting_for_evidence", "recovery_condition": "本 run 的分析执行者退出后只重验完成与计数"}
            try:
                status = read_json_object(status_path)
                if Path(status.get("run_dir", "")).resolve() != run_directory:
                    raise ValueError("分析线程状态的 run_dir 不匹配")
                if not status.get("formal_analysis_causality_snapshot_required"):
                    raise ValueError("只重验完成必须保留首次执行前的原始事实快照")
                if not isinstance(status.get("formal_analysis_causality_snapshot_sha256"), str):
                    raise ValueError("只重验完成缺少原始事实快照的绑定摘要")
                recorded_causality = freeze_or_load_original_causality(analysis_directory, status)
                if _reconcile_formal_analysis_completion_owned(
                    run_directory, status_path, status, recorded_causality, emit_result=False,
                ):
                    return {"status": "completed", "formal_analysis_completion_status": "completed",
                            "formal_analysis_completion_counter_status": status["formal_analysis_completion_counter_status"],
                            "recovery_condition": status["formal_analysis_completion_counter_recovery_condition"]}
                if status.get("formal_analysis_output_recovery_condition"):
                    return {"status": "waiting_for_evidence",
                            "formal_analysis_completion_status": status.get("formal_analysis_completion_status"),
                            "formal_analysis_completion_counter_status": status.get("formal_analysis_completion_counter_status"),
                            "recovery_condition": status["formal_analysis_output_recovery_condition"]}
                if status.get("formal_analysis_completion_status") != "completed":
                    return {"status": "not_completed"}
                raise ValueError("已完成归档的正式输出或原始事实证据暂未通过复核")
            finally:
                fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError) as exc:
        return {"status": "waiting_for_evidence", "error": f"{type(exc).__name__}: {exc}",
                "formal_analysis_completion_status": status.get("formal_analysis_completion_status"),
                "recovery_condition": "正式输出及绑定的原始事实快照恢复可核验后只重验完成与计数"}


def _execute_and_verify_analysis_owned(run_directory: Path, agent_argv: list[str]) -> int:
    """保持 agent 原始退出码；综合失败返回非零，让现有陈旧锁逻辑允许重试。"""
    analysis_directory = formal_analysis_directory(run_directory)
    status_path = analysis_directory / ".analyze-thread.status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict) or Path(status.get("run_dir", "")).resolve() != run_directory.resolve():
        raise ValueError("分析线程状态的 run_dir 不匹配")
    recorded_causality = freeze_or_load_original_causality(analysis_directory, status)

    limit = status.get("automatic_analysis_attempt_limit", 1)
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("自动分析尝试上限必须在 1 到 3 之间")
    if _reconcile_formal_analysis_completion_owned(
        run_directory, status_path, status, recorded_causality, emit_result=True,
    ):
        return 0
    if status.get("formal_analysis_output_recovery_condition"):
        return 1
    frozen_prompt = run_directory / "artifacts/analysis-frozen-prompt-before-first-agent-execution.md"
    if limit > 1:
        if frozen_prompt.is_symlink():
            raise ValueError("冻结分析 prompt 不能为符号链接")
        if not frozen_prompt.exists():
            prompt = (analysis_directory / ".analyze-thread.prompt.md").read_bytes()
            if len(prompt) > 65536:
                raise ValueError("冻结分析 prompt 超过大小限制")
            frozen_prompt.write_bytes(prompt)
        delivered_prompt = sys.stdin.read(65537)
        if delivered_prompt != frozen_prompt.read_text(encoding="utf-8"):
            raise ValueError("分析输入与首次冻结的 prompt 不一致")
    aggregate_returncode = 1
    completed = False
    generation = automatic_analysis_evidence_generation(run_directory, agent_argv)
    for _attempt_number in range(limit):
        attempt_id = None
        if limit > 1:
            attempt_id = _claim_analysis_attempt(run_directory, generation, limit)
            if attempt_id is None:
                status.update({"formal_analysis_completion_status": "failed", "returncode": 1,
                               "error": "本证据代次的三次自动分析尝试已耗尽",
                               "recovery_condition": "执行版本、入口、登录配置或原始输入可读性变化后自动重验；已成功进程的正式输出可独立重验"})
                _atomic_write_json(status_path, status)
                break
        process_error: str | None = None
        process_started = time.monotonic()
        try:
            # 继承冻结 prompt 的 stdin 与外层日志句柄，不把整个轨迹装进内存。
            if limit > 1:
                with frozen_prompt.open("rb") as input_stream:
                    process_returncode = subprocess.run(agent_argv, check=False, stdin=input_stream).returncode
            else:
                process_returncode = subprocess.run(agent_argv, check=False).returncode
        except OSError as exc:
            process_returncode = 127
            process_error = f"分析进程无法启动：{type(exc).__name__}: {exc}"

        validation = validate_formal_analysis_outputs(run_directory, recorded_causality=recorded_causality)
        if recorded_causality is None:
            validation["valid"] = False
            validation["errors"].append("首次 agent 执行前无法取得原始 causality 已记录事实")
        completed = process_returncode == 0 and validation["valid"]
        completion_status = "completed" if completed else (
            "failed" if process_returncode != 0 else "incomplete"
        )
        aggregate_returncode = 0 if completed else (
            process_returncode if process_returncode > 0 else 1
        )
        wait_for_output_io = process_returncode == 0 and bool(validation["output_io_evidence_unavailable"])
        status.update({
            "agent_process_returncode": process_returncode,
            "formal_analysis_completion_status": completion_status,
            "formal_analysis_validation_errors": validation["errors"],
            "formal_analysis_output_io_evidence_unavailable": validation["output_io_evidence_unavailable"],
            "formal_analysis_output_recovery_condition": FORMAL_OUTPUT_IO_READABILITY_RECOVERY_CONDITION if wait_for_output_io else None,
            "formal_analysis_output_sha256": validation["output_sha256"],
            "formal_analysis_verified_at": _iso_now(),
            "returncode": aggregate_returncode,
            "agent_process_wall_seconds": round(time.monotonic() - process_started, 6),
            "automatic_analysis_attempt_id": attempt_id,
            "error": process_error or (
                f"分析进程退出码为 {process_returncode}" if process_returncode != 0
                else "; ".join(validation["errors"]) or None
            ),
        })
        # 先落盘完成证据，再计数；状态写入失败不得算作成功。
        _atomic_write_json(status_path, status)
        if attempt_id:
            _finish_analysis_attempt(run_directory, attempt_id, status)
        print(json.dumps({
            "formal_analysis_completion_status": completion_status,
            "agent_process_returncode": process_returncode,
            "validation_errors": validation["errors"],
        }, ensure_ascii=False), flush=True)
        if completed or wait_for_output_io:
            break
    if completed:
        _record_verified_analysis_completion_count(run_directory, status_path, status)
    return aggregate_returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("agent_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.verify_only:
            result = validate_formal_analysis_outputs(args.run_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["valid"] else 1
        agent_argv = args.agent_argv
        if agent_argv[:1] == ["--"]:
            agent_argv = agent_argv[1:]
        if not agent_argv:
            parser.error("必须提供分析 agent 命令")
        return execute_and_verify_analysis(args.run_dir.resolve(), agent_argv)
    except (OSError, ValueError) as exc:
        print(json.dumps({"formal_analysis_completion_status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
