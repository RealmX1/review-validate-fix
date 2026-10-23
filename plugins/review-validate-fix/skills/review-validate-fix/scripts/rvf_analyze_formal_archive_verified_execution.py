#!/usr/bin/env python3
"""执行后台复盘并校验正式归档；退出码同时约束完成计数与线程重试。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from analysis_artifacts import ANALYSIS_SCHEMA_VERSION
from rvf_detached_thread import _atomic_write_json, _iso_now


ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME = "analysis-causality-before-first-agent-execution.json"


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
    try:
        analysis_directory = formal_analysis_directory(run_directory)
    except (OSError, ValueError) as exc:
        return {"valid": False, "errors": [str(exc)], "output_sha256": {}}

    contents: dict[str, str] = {}
    for filename in ("summary.md", "causality.json"):
        try:
            raw = (analysis_directory / filename).read_bytes()
            contents[filename] = raw.decode("utf-8")
            output_sha256[filename] = hashlib.sha256(raw).hexdigest()
        except (OSError, UnicodeError) as exc:
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
    return {"valid": not errors, "errors": errors, "output_sha256": output_sha256}


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
    """保持 agent 原始退出码；综合失败返回非零，让现有陈旧锁逻辑允许重试。"""
    analysis_directory = formal_analysis_directory(run_directory)
    status_path = analysis_directory / ".analyze-thread.status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict) or Path(status.get("run_dir", "")).resolve() != run_directory.resolve():
        raise ValueError("分析线程状态的 run_dir 不匹配")
    recorded_causality = freeze_or_load_original_causality(analysis_directory, status)

    process_error: str | None = None
    try:
        # 继承冻结 prompt 的 stdin 与外层日志句柄，不把整个轨迹装进内存。
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
    status.update({
        "agent_process_returncode": process_returncode,
        "formal_analysis_completion_status": completion_status,
        "formal_analysis_validation_errors": validation["errors"],
        "formal_analysis_output_sha256": validation["output_sha256"],
        "formal_analysis_verified_at": _iso_now(),
        "returncode": aggregate_returncode,
        "error": process_error or (
            f"分析进程退出码为 {process_returncode}" if process_returncode != 0
            else "; ".join(validation["errors"]) or None
        ),
    })
    # 先落盘完成证据，再计数；状态写入失败不得算作成功。
    _atomic_write_json(status_path, status)
    print(json.dumps({
        "formal_analysis_completion_status": completion_status,
        "agent_process_returncode": process_returncode,
        "validation_errors": validation["errors"],
    }, ensure_ascii=False), flush=True)
    if completed:
        try:
            from agentic_initiation_guardian_dispatch import record_analyze_completion

            print(json.dumps(record_analyze_completion(run_dir=str(run_directory)), ensure_ascii=False))
        except Exception as exc:  # 计数为 best-effort，不改变已验证的归档结果。
            print(f"复盘完成计数失败：{type(exc).__name__}: {exc}", file=sys.stderr)
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
