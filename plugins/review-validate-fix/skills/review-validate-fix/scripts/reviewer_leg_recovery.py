"""现有 dispatcher 的同腿恢复：保留发现、有界重试并持久保存可重验条件。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

import write_review_result
import command_lock
from reviewer_request_progress_identity import reviewer_request_content_identity


def observe_readable_reviewer_request_material(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            return {"path": str(path), "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}
    except OSError:
        return None


def prepare_request_leg_recovery(reviewer: dict[str, Any], result_path: Path, *, repo: Path,
                                  run_directory: Path, skill_directory: Path) -> bool:
    """机器可补齐的材料自行补齐；同一条件最多续跑一次，条件变化后可再入。"""
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    recovery = reviewer.setdefault("recovery", {})
    conditions = []
    for request in payload.get("requests", []):
        request["request_id"] = reviewer_request_content_identity(request)
        if request.get("status") == "resolved":
            conditions.append({"request_id": request.get("request_id"), "status": request["status"],
                               "response": request.get("response"), "resolution": request.get("resolution")})
            continue
        request_type = request.get("type")
        response = None
        waiting = None
        material_observation = None
        target = request.get("target")
        if request_type == "standard_request":
            standard = skill_directory / "references/review-standards" / (request["domain"] + "-subset.md")
            material_observation = observe_readable_reviewer_request_material(standard)
            if material_observation:
                response = f"专项标准已可读取：{standard}；自主选择适用部分并继续完整 Scope。"
            else:
                waiting = {"kind": "file_readable", "path": str(standard)}
        elif request_type == "context_request" and isinstance(target, str) and target:
            path = Path(target)
            path = path.resolve() if path.is_absolute() else (repo / path).resolve()
            if material_observation := observe_readable_reviewer_request_material(path):
                material_observation["material_scope_relation"] = "dependency_context_only"
                response = f"所需材料已可只读访问：{path}；sha256={material_observation['sha256']}。请核对材料归属；依赖上下文不扩大冻结 Scope。"
            else:
                waiting = {"kind": "file_readable", "path": str(path)}
        elif request_type == "context_request":
            if request.get("reason") == "need-clean-review-context":
                reviewer["force_file_references"] = True
            relative = {"manifest": "session-manifest.json", "packet": "review-packet.md"}.get(request.get("need"))
            path = run_directory / "artifacts/inputs" / relative if relative else None
            if path is not None and path.is_file():
                response = f"材料入口：{path}；可以先继续不依赖缺失材料的检查。"
            else:
                response = ("从本腿持久检查点启动 clean context，不读取另一腿结论。" if request.get("reason") == "need-clean-review-context"
                            else "请在 context-request 中用 --target 指明确切路径；继续不依赖该材料的审查。")
        elif request_type == "lock_request":
            response = (f"请自主使用隔离资源或既有锁包装器：{skill_directory / 'scripts/command_lock.py'}；"
                        "不要直接删除其他执行的锁。等待条件变化后继续。")
            lock_path, _ = command_lock.lock_paths(repo, command_lock.default_lock_root(), request["name"])
            try:
                if lock_path.exists():
                    with lock_path.open("r+b") as handle:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                material_observation = {"command_lock_path": str(lock_path), "available": True}
            except OSError as exc:
                waiting = {"kind": "command_lock_available", "path": str(lock_path), "name": request["name"],
                           "last_observation": "held" if isinstance(exc, BlockingIOError) else type(exc).__name__}
        elif request_type in {"measurement_request", "subtask_request"}:
            response = "可以在既有只读权限内自行选择工具完成查证，使用隔离资源及命令锁；仅新增权限或真实价值取舍才请求人类决定。"
        if waiting:
            conditions.append({"request_id": request.get("request_id"), **waiting})
        elif response and request.get("request_id"):
            if request.get("status") != "responded":
                write_review_result.command_request_progress(argparse.Namespace(
                    out=str(result_path), command="respond-request", request_id=request["request_id"], response=response,
                ))
            conditions.append({"request_id": request["request_id"], "status": "responded",
                "response": request.get("response") if request.get("status") == "responded" else response,
                "resolution": None, **({"material_observation": material_observation} if material_observation else {})})
        else:
            conditions.append({"kind": "reviewer_request_details_required", "request": request})
    fingerprint = hashlib.sha256(json.dumps(conditions, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    recovery["conditions"] = conditions
    recovery["condition_fingerprint"] = fingerprint
    recovery["attempted_actions"] = ["inspect_durable_request", "resolve_existing_material", "persist_response"]
    waiting = any(condition.get("kind") in {"file_readable", "command_lock_available", "reviewer_request_details_required"} for condition in conditions)
    attempts = recovery.setdefault("attempts_by_condition", {})
    if waiting or attempts.get(fingerprint, 0) >= 1:
        recovery["state"] = "waiting_for_condition_change"
        return False
    attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
    recovery["state"] = "ready_to_resume_same_leg"
    return True


def input_failure_condition(summary: dict[str, Any], stderr: str) -> str | None:
    combined = (str(summary.get("reason_code", "")) + " " + stderr).lower()
    for marker in ("context_length_exceeded", "maximum context", "context window", "prompt is too long",
                   "input too large", "request too large", "too many tokens", "context limit"):
        if marker in combined:
            return "reviewer_input_context_limit"
    return None
