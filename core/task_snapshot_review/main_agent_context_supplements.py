"""主会话的事实补充独立于 controller 登记；补充不能替代 Validate-Fixer 验真。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .persistent_artifacts import exclusive_lock, read_json, verify_repository_identity, write_bytes, write_json
from .execution_lifecycle import ensure_execution_not_revoked, execution_is_revoked


class MainAgentContextEvidenceUnavailable(RuntimeError):
    pass


def verified_reviewer_findings_snapshot(run_directory: Path) -> dict:
    snapshot = read_json(run_directory / "reviewer-findings.json")
    record = read_json(run_directory / "run.json")
    digest = hashlib.sha256(json.dumps(snapshot["findings"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if (snapshot.get("run_directory") != str(run_directory) or snapshot.get("candidate_commit") != record["candidate_commit"]
            or snapshot.get("findings_sha256") != digest):
        raise MainAgentContextEvidenceUnavailable("匿名 findings 原件与精确 run、候选或摘要不匹配")
    return snapshot


def registered_main_agent_context_supplements(run_directory: Path) -> list[dict]:
    snapshot_file = run_directory / "reviewer-findings.json"
    snapshot = verified_reviewer_findings_snapshot(run_directory) if snapshot_file.exists() else {}
    supplements = []
    for path in sorted((run_directory / "main-agent-context-supplements").glob("*.json")):
        supplement = read_json(path)
        if any(supplement.get(key) != snapshot.get(key) for key in ("run_directory", "candidate_commit", "findings_sha256")):
            raise ValueError("事实补充与精确 run、候选或 findings 绑定不匹配")
        original = run_directory / "main-agent-context-originals-by-sha256" / f"{supplement['context_sha256']}.md"
        try:
            original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        except OSError as error:
            raise MainAgentContextEvidenceUnavailable(f"事实补充原件不可读：{original}") from error
        if supplement.get("context_file") != str(original) or original_digest != supplement["context_sha256"]:
            raise MainAgentContextEvidenceUnavailable("事实补充原件缺失或已改变，恢复匹配摘要的原件后自动接续")
        supplements.append(supplement)
    return supplements


def pending_main_agent_context_supplements(run_directory: Path, supplements: list[dict] | None = None) -> list[dict]:
    supplements = registered_main_agent_context_supplements(run_directory) if supplements is None else supplements
    covered = set()
    # 只有最新一次完成的独立验真可以覆盖补充，未完成结果不能授予最终审阅资格。
    attempts = sorted((run_directory / "fixer").glob("attempt-*"))
    if attempts and (attempts[-1] / "result.json").exists():
        result = read_json(attempts[-1] / "result.json")
        assigned = {item["supplement_id"]: item for item in read_json(attempts[-1] / "request.json").get("main_agent_context_supplements", [])}
        registered = {item["supplement_id"]: item for item in supplements}
        if result.get("status") == "completed":
            covered = {item["supplement_id"] for item in result.get("context_supplement_responses", [])
                       if item.get("status") in {"confirmed", "refuted", "not_applicable"} and item.get("reason")
                       and item.get("supplement_id") in assigned and assigned[item["supplement_id"]] == registered.get(item["supplement_id"])}
    return [item for item in supplements if item["supplement_id"] not in covered]


def _withdraw_final_continuation_notices(run_directory: Path) -> None:
    manifest_file = run_directory / "agent-continuation-notices.json"
    if not manifest_file.exists():
        return
    manifest = read_json(manifest_file)
    write_json(manifest_file, {"notices": [item for item in manifest.get("notices", []) if item.get("stage") != "final_agent_review"]})


def publish_context_supplements_pending(run_directory: Path) -> dict:
    supplements = registered_main_agent_context_supplements(run_directory)
    result = {"run_directory": str(run_directory), "status": "context_supplements_pending",
              "disposition": "machine_evidence_pending", "recovery": {
                  "continuation_request_id": hashlib.sha256(json.dumps(supplements, sort_keys=True).encode()).hexdigest(),
                  "missing_evidence": "registered_context_supplements_independent_validation",
                  "trigger": "registered_context_supplement_changed", "action": "resume_same_run",
                  "maximum_unchanged_retries": 0,
                  "evidence_paths": [str(run_directory / "main-agent-context-supplements"),
                                     str(run_directory / "main-agent-context-continuation-request.json")]}}
    write_json(run_directory / "business-result.json", result)
    _withdraw_final_continuation_notices(run_directory)
    write_json(run_directory / "main-agent-context-continuation-request.json", {
        "supplement_ids": [item["supplement_id"] for item in supplements]})
    return result


def reconcile_interrupted_context_registration_locked(run_directory: Path) -> None:
    """补齐登记写到一半的补充，并撤下因此失效的旧最终资格。调用方持有补充锁。"""
    if execution_is_revoked(run_directory):
        return
    business_path = run_directory / "business-result.json"
    business = read_json(business_path) if business_path.exists() else {}
    if business.get("disposition") == "agent_judgment_required":
        return
    payload = business.get("pending_context_registration")
    if isinstance(payload, dict) and payload.get("supplement_id"):
        registration = run_directory / "main-agent-context-supplements" / f"{payload['supplement_id']}.json"
        if not registration.exists():
            write_json(registration, payload)
    try:
        pending = pending_main_agent_context_supplements(run_directory)
    except MainAgentContextEvidenceUnavailable:
        return
    if not pending and not (isinstance(payload, dict) and payload.get("supplement_id")):
        return
    manifest_file = run_directory / "agent-continuation-notices.json"
    final_notice = manifest_file.exists() and any(
        item.get("stage") == "final_agent_review" for item in read_json(manifest_file).get("notices", []))
    continuation = run_directory / "main-agent-context-continuation-request.json"
    recorded_ids = read_json(continuation).get("supplement_ids") if continuation.exists() else None
    pending_ids = [item["supplement_id"] for item in pending]
    if (isinstance(payload, dict) or business.get("disposition") == "agent_review_ready" or final_notice
            or recorded_ids != pending_ids):
        publish_context_supplements_pending(run_directory)


def reconcile_interrupted_context_registration(run_directory: Path) -> None:
    with exclusive_lock(run_directory / "main-agent-context-supplements.lock"):
        reconcile_interrupted_context_registration_locked(run_directory)


def register_main_agent_context_supplement(run_directory: Path, *, supplement_id: str, candidate_commit: str,
                                          findings_sha256: str, context_file: Path, finding_ids: list[str] | None = None) -> dict:
    run_directory = run_directory.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", supplement_id):
        raise ValueError("补充 ID 必须是最多 128 字符的稳定字母数字标识符")
    content = context_file.read_bytes()
    if not content.strip():
        raise ValueError("事实补充不能为空")
    content.decode("utf-8")
    with exclusive_lock(run_directory / "main-agent-context-supplements.lock"), exclusive_lock(run_directory / "execution-revocation.lock"):
        ensure_execution_not_revoked(run_directory)
        reconcile_interrupted_context_registration_locked(run_directory)
        record = read_json(run_directory / "run.json")
        verify_repository_identity(record)
        snapshot = verified_reviewer_findings_snapshot(run_directory)
        if snapshot.get("run_directory") != str(run_directory) or candidate_commit != record["candidate_commit"]:
            raise ValueError("事实补充的精确 run 或候选不匹配")
        if findings_sha256 != snapshot["findings_sha256"]:
            raise ValueError("事实补充的 findings 摘要不匹配")
        identities = sorted(set(finding_ids or []))
        if set(identities) - {item["finding_id"] for item in snapshot["findings"]}:
            raise ValueError("事实补充包含未知 finding ID")
        digest = hashlib.sha256(content).hexdigest()
        original = run_directory / "main-agent-context-originals-by-sha256" / f"{digest}.md"
        supplement = {"supplement_id": supplement_id, "run_directory": str(run_directory), "candidate_commit": candidate_commit,
                      "findings_sha256": findings_sha256, "finding_ids": identities, "context_sha256": digest, "context_file": str(original)}
        registration = run_directory / "main-agent-context-supplements" / f"{supplement_id}.json"
        if registration.exists():
            if read_json(registration) != supplement:
                raise ValueError("补充 ID 已用于不同材料，不能覆盖原登记")
            registered_main_agent_context_supplements(run_directory)
            if pending_main_agent_context_supplements(run_directory):
                publish_context_supplements_pending(run_directory)
            return supplement
        if original.exists() and original.read_bytes() != content:
            raise ValueError("事实补充原件摘要冲突")
        write_bytes(original, content)
        business_file = run_directory / "business-result.json"
        existing_result = read_json(business_file) if business_file.exists() else {}
        if existing_result.get("disposition") in {"agent_review_ready", "agent_judgment_required"}:
            # 先撤下旧最终资格并留下可重放登记，再写入补充名录。中断后的接续能自行补验。
            write_json(business_file, {"run_directory": str(run_directory), "status": "context_supplements_pending",
                "disposition": "machine_evidence_pending", "pending_context_registration": supplement,
                "recovery": {"continuation_request_id": hashlib.sha256(json.dumps([supplement], sort_keys=True).encode()).hexdigest(),
                    "missing_evidence": "registered_context_supplements_independent_validation",
                    "trigger": "registered_context_supplement_changed", "action": "resume_same_run",
                    "maximum_unchanged_retries": 0,
                    "evidence_paths": [str(run_directory / "main-agent-context-supplements"),
                                       str(run_directory / "main-agent-context-continuation-request.json")]}})
            _withdraw_final_continuation_notices(run_directory)
        write_json(registration, supplement)
        # 独立登记不改 run.json，避免覆盖正在运行的 controller 进度。
        publish_context_supplements_pending(run_directory)
        return supplement
