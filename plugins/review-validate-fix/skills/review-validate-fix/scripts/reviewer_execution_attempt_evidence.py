"""精确 reviewer 尝试的执行锁继承与持久退出证据。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from independent_backup_route import durable_json


REVIEWER_ATTEMPT_ID_ENVIRONMENT_VARIABLE = "RVF_REVIEWER_EXECUTION_ATTEMPT_ID"
DISPATCH_EXECUTION_LOCK_DESCRIPTOR_ENVIRONMENT_VARIABLE = "RVF_REVIEWER_DISPATCH_EXECUTION_LOCK_DESCRIPTOR"


def reviewer_execution_input_binding(*, run_id: str, repo: str | Path | None,
                                     review_packet: str | Path | None,
                                     scope_contract: str | Path | None) -> dict[str, Any]:
    return {"run_id": run_id, "repo": str(Path(repo).resolve()) if repo else None,
            "packet_sha256": hashlib.sha256(Path(review_packet).read_bytes()).hexdigest() if review_packet else None,
            "scope_sha256": hashlib.sha256(Path(scope_contract).read_bytes()).hexdigest() if scope_contract else None}


def inherited_reviewer_dispatch_execution_lock_descriptors(run_directory: Path) -> tuple[int, ...]:
    """真实 reviewer 继续持有父锁；父 dispatcher 或 supervisor 被杀不能放行重复派发。"""
    raw_descriptor = os.environ.get(DISPATCH_EXECUTION_LOCK_DESCRIPTOR_ENVIRONMENT_VARIABLE)
    if raw_descriptor is None:
        return ()
    descriptor = int(raw_descriptor)
    inherited_identity = os.fstat(descriptor)
    expected_identity = (run_directory / "artifacts/reviewers/.reviewer-dispatch-execution.lock").stat()
    if (inherited_identity.st_dev, inherited_identity.st_ino) != (expected_identity.st_dev, expected_identity.st_ino):
        raise ValueError("继承的 dispatcher 执行锁不属于精确 run")
    return (descriptor,)


def persist_reviewer_execution_attempt_receipt(*, run_directory: Path, reviewer_id: str,
        execution_input_binding: dict[str, Any], result_path: Path, returncode: int,
        reviewer_subprocess_returncode: int, review_complete: bool) -> None:
    attempt_id = os.environ.get(REVIEWER_ATTEMPT_ID_ENVIRONMENT_VARIABLE)
    if attempt_id is None:
        return
    if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
        raise ValueError("reviewer 尝试身份无效")
    receipt = {"schema": "rvf-reviewer-execution-attempt/v1", "attempt_id": attempt_id,
        "run_directory": str(run_directory.resolve()), "reviewer_id": reviewer_id,
        "execution_input_binding": execution_input_binding, "returncode": returncode,
        "reviewer_subprocess_returncode": reviewer_subprocess_returncode,
        "review_complete": review_complete, "completed_at": datetime.now(timezone.utc).isoformat(),
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path.is_file() else None}
    durable_json(result_path.parent / "execution-attempt-results" / f"{attempt_id}.json", receipt)


def recover_exited_reviewer_execution_attempts(plan: dict[str, Any], artifacts_directory: Path) -> list[str]:
    """执行锁已释放后，仅接纳计划中精确尝试的最终回执，不猜测最新文件或成功状态。"""
    recovered = []
    for reviewer in plan.get("reviewers", []):
        attempt_id = reviewer.get("execution_attempt_id")
        if reviewer.get("returncode") is not None or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            continue
        result_path = artifacts_directory / "reviewers" / reviewer["reviewer_id"] / "review-result.json"
        receipt_path = result_path.parent / "execution-attempt-results" / f"{attempt_id}.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("schema") != "rvf-reviewer-execution-attempt/v1"
                    or receipt.get("attempt_id") != attempt_id or receipt.get("reviewer_id") != reviewer["reviewer_id"]
                    or receipt.get("run_directory") != str(artifacts_directory.parent.resolve())
                    or receipt.get("execution_input_binding") != plan.get("execution_input_binding")
                    or type(receipt.get("returncode")) is not int
                    or (receipt["returncode"] == 0 and receipt.get("reviewer_subprocess_returncode") != 0)
                    or receipt.get("result_sha256") != (hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path.is_file() else None)):
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        reviewer.update(returncode=receipt["returncode"], execution_state="exited",
            review_result_path=str(result_path), execution_receipt_recovery={"receipt_path": str(receipt_path),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()})
        recovered.append(reviewer["reviewer_id"])
    return recovered
