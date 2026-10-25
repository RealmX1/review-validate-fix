#!/usr/bin/env python3
"""持久记录用户委派；原 elevated 事实及所有执行权限保持独立。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from independent_backup_route import BackupRouteOwned, guard_normal_mutation
from validation_result_cache import candidate_content_manifest_sha256, canonical_json_bytes, clean_git_environment


DELEGATION_CAPABILITY = "user_delegated_elevated_issue_disposition_v1"
DELEGATION_CONCLUSION = "review_completed_with_user_delegated_issues"
RECEIPT_SCHEMA = "rvf-elevated-issue-delegation-receipt"
DELEGATION_DIRECTORY = Path("artifacts/elevated-issue-delegation")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SHA256 = re.compile(r"[0-9a-f]{64}")


class DelegationEvidenceError(ValueError):
    def __init__(self, reason: str, *, status: str = "invalid_evidence") -> None:
        self.status = status
        super().__init__(reason)


def digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DelegationEvidenceError(f"证据文件缺失或不是普通文件：{path}", status="waiting_for_machine_evidence")
    return path.read_bytes()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(regular_bytes(path))
    if not isinstance(value, dict):
        raise DelegationEvidenceError(f"证据必须为 JSON object：{path}")
    return value


def assert_safe_artifact_path(path: Path, root: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(root):
        raise DelegationEvidenceError("处置工件路径不属于绑定 run")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise DelegationEvidenceError(f"处置工件路径包含软链接：{parent}")
        if parent == root:
            break


def read_canonical(path: Path, expected_digest: str | None = None) -> tuple[dict[str, Any], str]:
    encoded = regular_bytes(path)
    value = json.loads(encoded)
    actual = hashlib.sha256(encoded).hexdigest()
    if not isinstance(value, dict) or encoded != canonical_json_bytes(value):
        raise DelegationEvidenceError("处置工件不是 canonical JSON")
    if regular_bytes(Path(f"{path}.sha256")) != f"{actual}\n".encode() or (expected_digest is not None and actual != expected_digest):
        raise DelegationEvidenceError("处置工件摘要不符")
    return value, actual


def atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_artifact_path(path, path.parent)
    descriptor, staged_name = tempfile.mkstemp(prefix=".delegation-atomic-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(staged_name).unlink(missing_ok=True)


def immutable_artifact(root: Path, directory: str, payload: dict[str, Any]) -> dict[str, str]:
    sha = digest(payload)
    path = root / directory / f"{sha}.json"
    assert_safe_artifact_path(path, root)
    encoded = canonical_json_bytes(payload)
    if path.exists() and regular_bytes(path) != encoded:
        raise DelegationEvidenceError("不可变处置工件冲突")
    if not path.exists():
        atomic_bytes(path, encoded)
    sidecar = Path(f"{path}.sha256")
    if sidecar.exists() and regular_bytes(sidecar) != f"{sha}\n".encode():
        raise DelegationEvidenceError("不可变工件 sidecar 冲突")
    if not sidecar.exists():
        atomic_bytes(sidecar, f"{sha}\n".encode())
    return {"path": str(path), "sha256": sha}


def git_identity(repo: Path) -> dict[str, str]:
    def output(*arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=repo, env=clean_git_environment(), capture_output=True, text=True, check=True).stdout.strip()
    return {"worktree_realpath": str(Path(output("rev-parse", "--show-toplevel")).resolve()),
            "git_common_dir_realpath": str(Path(output("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())}


def frozen_contract(run_dir: Path) -> dict[str, Any]:
    current = read_object(run_dir / "artifacts/inputs/scope.contract.json")
    if current.get("landing_restamp"):
        current, _sha = read_canonical(run_dir / "artifacts/inputs/scope.contract.reviewed.json")
    canonical_scope = current.get("canonical_scope")
    if not isinstance(canonical_scope, dict) or digest(canonical_scope) != current.get("scope_hash"):
        raise DelegationEvidenceError("冻结 scope 摘要不符")
    return current


def latest_issue_facts(run_dir: Path, issue_id: str, *, require_elevated: bool = True) -> dict[str, Any]:
    if not isinstance(issue_id, str) or not IDENTIFIER.fullmatch(issue_id):
        raise DelegationEvidenceError("issue_id 不合法")
    contract = frozen_contract(run_dir)
    run_id = read_object(run_dir / "summary.json").get("run_id")
    if run_id != contract.get("run_id"):
        raise DelegationEvidenceError("run 身份不符")
    issue = read_object(run_dir / "artifacts/fix-issues" / f"{issue_id}.json")
    if issue.get("run_id") != run_id or issue.get("issue_id") != issue_id:
        raise DelegationEvidenceError("canonical issue 身份不符")
    attempts: list[tuple[datetime, dict[str, Any], Path]] = []
    for path in sorted((run_dir / "artifacts/fix-attempts").glob("*/attempt.json")):
        if path.parent.is_symlink():
            raise DelegationEvidenceError("attempt 目录不可为软链接")
        attempt = read_object(path)
        if attempt.get("issue_id") != issue_id:
            continue
        if (type(attempt.get("schema_version")) is not int or attempt.get("schema_version") != 1 or attempt.get("run_id") != run_id
            or attempt.get("attempt_id") != path.parent.name or Path(str(attempt.get("repo", ""))).resolve() != Path(contract["repo"]).resolve()):
            raise DelegationEvidenceError("attempt 身份不符")
        created = datetime.fromisoformat(str(attempt["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise DelegationEvidenceError("attempt 时间必须带时区")
        attempts.append((created, attempt, path.parent))
    attempts.sort(key=lambda item: item[0])
    if not attempts or len({entry[0] for entry in attempts}) != len(attempts):
        raise DelegationEvidenceError("最新 attempt 缺失或时间冲突")
    _created, attempt, directory = attempts[-1]
    result = read_object(directory / "result.json")
    if (type(result.get("schema_version")) is not int or result.get("schema_version") != 1 or any(result.get(name) != attempt.get(name) for name in ("run_id", "issue_id", "attempt_id"))
        or (require_elevated and (result.get("status") != "elevated" or attempt.get("status") != "elevated"))):
        raise DelegationEvidenceError("委派只接受最新 elevated/elevated attempt，不能覆盖失败或未应用修复")
    return {"issue": issue, "attempt": attempt, "result": result}


def current_request_candidate_binding(repo: Path, run_dir: Path, issue_id: str) -> dict[str, str]:
    return {"candidate_content_manifest_sha256": candidate_content_manifest_sha256(repo),
            "attempt_id": latest_issue_facts(run_dir, issue_id)["attempt"]["attempt_id"],
            "reviewed_scope_hash": frozen_contract(run_dir)["scope_hash"],
            "handoff_sha256": hashlib.sha256(regular_bytes(run_dir / "artifacts/handoff.md")).hexdigest()}


def content_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        raise DelegationEvidenceError("原生消息正文格式不支持")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise DelegationEvidenceError("原生消息正文格式不支持")
        if part.get("type") in {"text", "Text", "input_text", "output_text"} and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif part.get("type") not in {"image", "input_image", "local_image"}:
            raise DelegationEvidenceError("原生附件缺少可核对的文本；需从原生记录补证", status="waiting_for_machine_evidence")
    return "".join(texts)


def validate_native_authorization(snapshot: dict[str, Any], authorization: dict[str, Any], issue_id: str,
                                  follow_up: dict[str, Any] | None, target_receipt: dict[str, Any] | None, action: str, *, run_id: str) -> None:
    if snapshot.get("schema") != "rvf-historical-native-user-authorization-snapshot" or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1:
        raise DelegationEvidenceError("原生授权快照版本不支持")
    transcript = authorization.get("transcript_path")
    if (not isinstance(transcript, str) or not Path(transcript).is_absolute() or snapshot.get("original_transcript_path") != transcript
        or not any(marker in transcript for marker in ("/.codex/sessions/", "/.cline/kanban/codex-conversation-storage-home/sessions/"))):
        raise DelegationEvidenceError("原生授权归档来源路径不符")
    records = snapshot.get("records")
    if not isinstance(records, list) or not records:
        raise DelegationEvidenceError("缺少原生授权快照")
    if run_id not in canonical_json_bytes(records).decode("utf-8"):
        raise DelegationEvidenceError("原生授权上下文没有绑定该 run，不能复用其他审查的同名 issue 决定")
    session = None
    current_turn = None
    user_triggered = False
    user_messages: list[str] = []
    assistant_context: list[str] = []
    received_text: str | None = None
    native_provenance_texts: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise DelegationEvidenceError("原生记录格式不合法")
        payload = record["payload"]
        if record.get("type") == "session_meta":
            session = payload.get("id")
            if payload.get("originator") != "codex-tui" or payload.get("source") not in {"cli", "vscode"}:
                raise DelegationEvidenceError("原生会话格式未获支持，不能仅凭 user 标签接受授权")
        if record.get("type") == "response_item" and payload.get("role") == "user":
            metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
            if not isinstance(metadata, dict):
                raise DelegationEvidenceError("原生用户 provenance 元数据不是 object")
            provenance = metadata.get("content_item_metadata") or []
            if (metadata.get("turn_id") == authorization.get("native_turn_id") and isinstance(provenance, list) and provenance
                and all(isinstance(part, dict) and isinstance(part.get("provenance"), dict) and part["provenance"].get("type") == "user" for part in provenance)):
                native_provenance_texts.append(content_text(payload.get("content")))
        if record.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "task_started":
            current_turn = payload.get("turn_id")
            attribution = payload.get("turn_attribution") or {}
            if not isinstance(attribution, dict):
                raise DelegationEvidenceError("原生 turn 归属元数据不是 object")
            user_triggered = attribution.get("turn_trigger") == "user"
        if kind == "task_complete" and received_text is None and isinstance(payload.get("last_agent_message"), str):
            assistant_context.append(payload["last_agent_message"])
        item = payload.get("item") or {}
        if kind != "item_completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "AgentMessage" and received_text is None:
            assistant_context.append(content_text(item.get("content", [])) if "text" not in item else item["text"])
        if item.get("type") != "UserMessage":
            continue
        if payload.get("turn_id") == authorization.get("native_turn_id"):
            user_messages.append(str(item.get("id")))
        if item.get("id") != authorization.get("native_user_message_id"):
            continue
        if received_text is not None or not user_triggered or current_turn != authorization.get("native_turn_id") or payload.get("turn_id") != current_turn:
            raise DelegationEvidenceError("原生用户消息没有独立的 user-triggered turn")
        received_text = content_text(item.get("content"))
    if (session != authorization.get("session_id") or user_messages != [authorization.get("native_user_message_id")]
        or received_text is None or received_text != authorization.get("message_text") or received_text not in native_provenance_texts):
        raise DelegationEvidenceError("原生 session/turn/UserMessage/用户 provenance 或原话不符；role=user 本身不构成授权")
    if received_text.lstrip().startswith(("<hook_prompt", "<task-notification", "<environment_context>", "<task-initial-prompt")):
        raise DelegationEvidenceError("自动投递和 handoff 不能作为用户委派授权")
    if action == "revoke":
        if issue_id not in received_text or not re.search(r"撤销|取消.*委派|revoke|cancel.*delegat", received_text, re.I):
            raise DelegationEvidenceError("缺少针对 issue 的用户撤销原话")
        return
    if re.search(r"(?:^|[\n。；])\s*(?:Stop\b|停止|暂停|禁止|不得|不要(?:提交|同步|推送|执行|继续))", received_text, re.I):
        raise DelegationEvidenceError("用户请求包含执行禁止；委派不能解除它", status="execution_prohibited")
    if (re.search(r"不同意|不批准|不可以|拒绝|\bdisagree\b|\breject\b", received_text, re.I)
        or re.search(r"(?:不要|不能|不应|不得|禁止|do not|don't).{0,16}(?:委派|转交|交给|派到|创建|新建|delegat|assign|creat)", received_text, re.I)):
        raise DelegationEvidenceError("用户原话明确拒绝该委派")
    mode = authorization.get("mode")
    if mode == "direct_delegation":
        if (follow_up is None or issue_id not in received_text or follow_up["task_id"] not in received_text
            or not re.search(r"委派|转交|交给|派到|delegat|assign", received_text, re.I)):
            raise DelegationEvidenceError("直接委派须由用户原话绑定 issue 和目标 task")
    elif mode == "approve_proposed_delegation":
        quote = authorization.get("proposal_context_quote")
        if (not isinstance(quote, str) or issue_id not in quote or not assistant_context or quote not in assistant_context[-1]
            or not re.search(r"^\s*(?:可以|同意|批准|好的?|yes\b|I agree\b|approve\b)", received_text, re.I)
            or not (re.search(r"(?:创建|新建|开|建).{0,12}(?:task|任务)|delegat|assign", received_text, re.I)
                    or re.search(r"(?:委派|交给|转交|delegat|assign).{0,80}(?:task|任务)|(?:创建|新建|开|建).{0,12}(?:task|任务)", quote, re.I))):
            raise DelegationEvidenceError("用户批准链缺少原生提案、issue 或建卡授权")
        binding = authorization.get("target_binding_quote")
        task = (target_receipt or {}).get("task") or {}
        if not isinstance(binding, str) or len(binding.strip()) < 8 or binding not in quote or binding not in str(task.get("prompt", "")):
            raise DelegationEvidenceError("建卡回执未绑定用户批准的提案内容")
    else:
        raise DelegationEvidenceError("不支持的用户委派授权模式")


def capture_native_snapshot(authorization: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    archived = authorization.get("native_snapshot")
    if isinstance(archived, dict):
        expected = run_dir / DELEGATION_DIRECTORY / "native-evidence-snapshots" / f"{archived.get('sha256')}.json"
        assert_safe_artifact_path(expected, run_dir)
        if archived.get("path") != str(expected):
            raise DelegationEvidenceError("历史授权归档不属于原 run")
        snapshot, _sha = read_canonical(expected, archived.get("sha256"))
        return snapshot
    path = Path(str(authorization.get("transcript_path", "")))
    supported = any(marker in str(path) for marker in ("/.codex/sessions/", "/.cline/kanban/codex-conversation-storage-home/sessions/"))
    if not path.is_absolute() or not supported:
        raise DelegationEvidenceError("授权必须来自受支持的原生会话存储，不接受任意 role=user 文件")
    records = []
    found = False
    for line in regular_bytes(path).decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records.append(record)
        payload = record.get("payload") or {}
        item = payload.get("item") or {}
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed" and item.get("type") == "UserMessage" and item.get("id") == authorization.get("native_user_message_id"):
            found = True
            break
    if not found:
        raise DelegationEvidenceError("原生用户消息尚未持久化", status="waiting_for_machine_evidence")
    return {"schema": "rvf-historical-native-user-authorization-snapshot", "schema_version": 1,
            "original_transcript_path": str(path), "records": records}


def validate_target_identity(receipt: dict[str, Any], target: dict[str, Any]) -> None:
    task = receipt.get("task") or {}
    if not isinstance(task, dict):
        raise DelegationEvidenceError("目标控制面 task 不是 object")
    workspace_path = receipt.get("workspacePath") or task.get("workspacePath")
    if (receipt.get("ok") is not True or task.get("id") != target.get("task_id")
        or not isinstance(target.get("task_id"), str) or not IDENTIFIER.fullmatch(target["task_id"])
        or not isinstance(target.get("workspace_id"), str) or not IDENTIFIER.fullmatch(target["workspace_id"])
        or not isinstance(target.get("project_path"), str) or not Path(target["project_path"]).is_absolute()
        or str(Path(str(workspace_path)).resolve()) != target.get("project_path")
        or type(target.get("task_created_at")) is not int or target["task_created_at"] <= 0
        or task.get("createdAt") != target["task_created_at"]):
        raise DelegationEvidenceError("目标稳定 task/project/创建时间身份与原生回执不符")
    neighborhood = receipt.get("workUnitRelationNeighborhood") or {}
    summary = neighborhood.get("renderedNeighborhoodSummaryText", "")
    # 工作区 ID 不总是目录名；需要控制面返回的明确身份。
    observed_workspace_id = receipt.get("workspaceId")
    if observed_workspace_id is None and isinstance(summary, str) and f"[{target['workspace_id']}]" in summary:
        observed_workspace_id = target["workspace_id"]
    if observed_workspace_id != target["workspace_id"]:
        raise DelegationEvidenceError("目标 workspace_id 缺少控制面正向证据")


def read_ledger(run_dir: Path) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, str]]]]:
    root = run_dir / DELEGATION_DIRECTORY
    path = root / "ledger.json"
    assert_safe_artifact_path(path, run_dir)
    if not path.exists():
        return {"schema": "rvf-elevated-issue-delegation-ledger", "schema_version": 1, "events": []}, []
    encoded = regular_bytes(path)
    ledger = json.loads(encoded)
    if (not isinstance(ledger, dict) or encoded != canonical_json_bytes(ledger)
        or ledger.get("schema") != "rvf-elevated-issue-delegation-ledger" or type(ledger.get("schema_version")) is not int or ledger.get("schema_version") != 1
        or not isinstance(ledger.get("events"), list)):
        raise DelegationEvidenceError("处置账本格式不合法")
    previous = None
    events = []
    for sequence, reference in enumerate(ledger["events"], 1):
        if not isinstance(reference, dict) or not isinstance(reference.get("sha256"), str) or not SHA256.fullmatch(reference["sha256"]):
            raise DelegationEvidenceError("处置账本引用不合法")
        expected = root / "receipts" / f"{reference['sha256']}.json"
        assert_safe_artifact_path(expected, run_dir)
        if reference.get("path") != str(expected):
            raise DelegationEvidenceError("处置回执逃逸原 run")
        receipt, sha = read_canonical(expected, reference["sha256"])
        if (receipt.get("schema") != RECEIPT_SCHEMA or type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1
            or type(receipt.get("sequence")) is not int or receipt.get("sequence") != sequence or receipt.get("previous_receipt_sha256") != previous
            or receipt.get("action") not in {"delegate", "revoke"}):
            raise DelegationEvidenceError("处置追加链顺序或身份不符")
        previous = sha
        events.append((receipt, reference))
    if regular_bytes(path) != encoded:
        raise DelegationEvidenceError("处置账本在读取期间变化", status="waiting_for_machine_evidence")
    return ledger, events


def validate_receipt(receipt: dict[str, Any], run_dir: Path, repo_identity: dict[str, str], candidate_digest: str,
                     *, require_latest: bool = True) -> None:
    contract = frozen_contract(run_dir)
    facts = receipt.get("facts")
    if (receipt.get("run_id") != contract.get("run_id") or receipt.get("repo") != repo_identity
        or receipt.get("reviewed_scope_hash") != contract.get("scope_hash") or receipt.get("reviewed_contract_sha256") != digest(contract)
        or receipt.get("handoff_sha256") != hashlib.sha256(regular_bytes(run_dir / "artifacts/handoff.md")).hexdigest()
        or receipt.get("candidate_content_manifest_sha256") != candidate_digest
        or not isinstance(facts, dict) or receipt.get("facts_sha256") != digest(facts)):
        raise DelegationEvidenceError("委派回执的 run/repo/scope/handoff/候选或事实摘要不符")
    if require_latest and facts != latest_issue_facts(run_dir, receipt["issue_id"]):
        raise DelegationEvidenceError("委派未覆盖最新 elevated attempt")
    if (facts.get("attempt", {}).get("attempt_id") != receipt.get("attempt_id")
        or facts.get("issue", {}).get("issue_id") != receipt.get("issue_id")
        or facts.get("attempt", {}).get("status") != "elevated" or facts.get("result", {}).get("status") != "elevated"):
        raise DelegationEvidenceError("委派回执 issue/attempt 身份不符")
    if (any(not isinstance(facts.get(name), dict) or facts[name].get("run_id") != receipt["run_id"] or facts[name].get("issue_id") != receipt["issue_id"] for name in ("issue", "attempt", "result"))
        or facts["result"].get("attempt_id") != receipt["attempt_id"]
        or any(facts[name].get("schema_version") != 1 for name in ("attempt", "result"))):
        raise DelegationEvidenceError("历史处置快照的 run/issue/attempt 身份不符")
    authorization = receipt.get("authorization")
    if not isinstance(authorization, dict):
        raise DelegationEvidenceError("委派缺少原生用户授权")
    root = run_dir / DELEGATION_DIRECTORY
    snapshot_ref = authorization.get("native_snapshot") or {}
    snapshot_path = root / "native-evidence-snapshots" / f"{snapshot_ref.get('sha256')}.json"
    assert_safe_artifact_path(snapshot_path, run_dir)
    if snapshot_ref.get("path") != str(snapshot_path):
        raise DelegationEvidenceError("用户原生快照逃逸 run")
    snapshot, _sha = read_canonical(snapshot_path, snapshot_ref.get("sha256"))
    target_receipt = None
    follow_up = receipt.get("follow_up")
    if receipt["action"] == "delegate":
        if not isinstance(follow_up, dict):
            raise DelegationEvidenceError("委派缺少目标 task")
        target_ref = receipt.get("target_identity_snapshot") or {}
        target_path = root / "target-identity-snapshots" / f"{target_ref.get('sha256')}.json"
        assert_safe_artifact_path(target_path, run_dir)
        if target_ref.get("path") != str(target_path):
            raise DelegationEvidenceError("目标 task 快照逃逸 run")
        target_receipt, _sha = read_canonical(target_path, target_ref.get("sha256"))
        validate_target_identity(target_receipt, follow_up)
    validate_native_authorization(snapshot, authorization, receipt["issue_id"], follow_up, target_receipt, receipt["action"], run_id=receipt["run_id"])


def accepted_delegations(repo: Path, run_dir: Path, *, candidate_digest: str | None = None) -> list[dict[str, Any]]:
    identity = git_identity(repo)
    candidate = candidate_digest or candidate_content_manifest_sha256(repo)
    ledger, events = read_ledger(run_dir)
    active = {}
    for receipt, reference in events:
        # 历史事件按当时快照核验；当前候选及最新 attempt 仅约束仍生效的委派。
        validate_receipt(receipt, run_dir, identity, receipt.get("candidate_content_manifest_sha256"), require_latest=False)
        if receipt["action"] == "revoke":
            active.pop(receipt["issue_id"], None)
        else:
            active[receipt["issue_id"]] = (receipt, reference)
    accepted = []
    for receipt, reference in active.values():
        validate_receipt(receipt, run_dir, identity, candidate)
        accepted.append({"issue_id": receipt["issue_id"], "attempt_id": receipt["attempt_id"],
                         "receipt_path": reference["path"], "receipt_sha256": reference["sha256"], "follow_up": receipt["follow_up"]})
    if read_ledger(run_dir)[0] != ledger:
        raise DelegationEvidenceError("委派账本在核验期间变化", status="waiting_for_machine_evidence")
    return sorted(accepted, key=lambda item: item["issue_id"])


def validate_evidence_delegations(payload: dict[str, Any], contract: dict[str, Any]) -> None:
    review = payload["review"]
    if payload["schema_version"] != 3:
        if (review.get("user_delegated_elevated_issues") or payload.get("required_capabilities")
            or review.get("conclusion") == DELEGATION_CONCLUSION):
            raise DelegationEvidenceError("用户委派证据必须使用 version 3，禁止降级")
        return
    if payload.get("required_capabilities") != [DELEGATION_CAPABILITY] or review.get("conclusion") != DELEGATION_CONCLUSION:
        raise DelegationEvidenceError("v3 缺少委派能力或真实结论")
    counts = review.get("outcome_counts")
    expected_keys = {"fixed", "false_positive", "submitted_to_kanban_backlog", "elevated", "user_delegated", "blocking_elevated"}
    if (not isinstance(counts, dict) or set(counts) != expected_keys or any(type(value) is not int or value < 0 for value in counts.values())
        or counts["blocking_elevated"] != 0 or counts["elevated"] != counts["user_delegated"] or counts["user_delegated"] == 0
        or sum(counts[name] for name in ("fixed", "false_positive", "submitted_to_kanban_backlog", "elevated")) != review["canonical_issue_count"]):
        raise DelegationEvidenceError("v3 问题与委派计数不符")
    run_dir = Path(payload["scope"]["contract_path"]).parent.parent.parent
    accepted = accepted_delegations(Path(payload["subject"]["worktree_realpath"]), run_dir,
                                    candidate_digest=payload["subject"]["candidate_content_manifest_sha256"])
    if not accepted or accepted != review.get("user_delegated_elevated_issues") or len(accepted) != counts["user_delegated"]:
        raise DelegationEvidenceError("v3 委派引用与有效账本不符")
    from reviewed_candidate_completion_facts import completed_review_facts

    completed = completed_review_facts(run_dir, regular_bytes(run_dir / "artifacts/handoff.md").decode(),
                                       candidate_digest=payload["subject"]["candidate_content_manifest_sha256"])
    if completed["outcome_counts"] != counts or completed["canonical_issue_count"] != review["canonical_issue_count"]:
        raise DelegationEvidenceError("v3 计数与 canonical reviewer/merge/最新 attempt 完成事实不符")


def target_receipt_with_machine_recovery(request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    path_value = request.get("target_receipt_path")
    if isinstance(path_value, str):
        path = Path(path_value)
        if path.exists() or path.is_symlink():
            return read_object(path)
    target = request.get("follow_up") or {}
    if not isinstance(target, dict):
        raise DelegationEvidenceError("follow_up 必须为 object")
    if not all(isinstance(target.get(name), str) and target[name] for name in ("task_id", "project_path")):
        raise DelegationEvidenceError("缺少稳定目标身份，无法自动查询")
    import cline_kanban_client

    command = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", cline_kanban_client.DEFAULT_TASK_CMD)
    try:
        completed = subprocess.run([*shlex.split(command), "get", "--task-id", target["task_id"], "--project-path", target["project_path"]],
                                   cwd=run_dir, env=clean_git_environment(), capture_output=True, text=True, timeout=15)
        if completed.returncode:
            raise ValueError(completed.stderr[-1000:] or f"exit {completed.returncode}")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("目标控制面未返回 object")
        validate_target_identity(payload, target)
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, DelegationEvidenceError) and exc.status == "invalid_evidence":
            raise
        raise DelegationEvidenceError(f"目标回执尚缺且控制面查询暂不可用：{exc}", status="waiting_for_machine_evidence") from exc


@guard_normal_mutation("repo", "record-elevated-issue-delegation", "run_dir")
def record_delegation(repo: Path, run_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    if type(request.get("schema_version")) is not int or request.get("schema_version") != 1 or request.get("run_id") != frozen_contract(run_dir).get("run_id"):
        raise DelegationEvidenceError("委派请求版本或 run 身份不符")
    action = request.get("action", "delegate")
    if action not in {"delegate", "revoke"}:
        raise DelegationEvidenceError("委派 action 不合法")
    root = run_dir / DELEGATION_DIRECTORY
    assert_safe_artifact_path(root, run_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "mutation.lock"
    assert_safe_artifact_path(lock_path, run_dir)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger, events = read_ledger(run_dir)
        facts = latest_issue_facts(run_dir, request["issue_id"])
        if request.get("candidate_binding") is not None and request["candidate_binding"] != current_request_candidate_binding(repo, run_dir, request["issue_id"]):
            raise DelegationEvidenceError("委派请求绑定的候选、最新 attempt、scope 或 handoff 已变化，不能自动重绑旧决定")
        identity = git_identity(repo)
        if identity["worktree_realpath"] != str(Path(frozen_contract(run_dir)["repo"]).resolve()):
            raise DelegationEvidenceError("委派请求目标仓库与 run 不符")
        authorization = dict(request["authorization"])
        snapshot = capture_native_snapshot(authorization, run_dir)
        follow_up = request.get("follow_up") if action == "delegate" else None
        target_receipt = target_receipt_with_machine_recovery(request, run_dir) if action == "delegate" else None
        if action == "delegate":
            if not isinstance(follow_up, dict):
                raise DelegationEvidenceError("委派缺少 follow_up")
            validate_target_identity(target_receipt, follow_up)
        validate_native_authorization(snapshot, authorization, request["issue_id"], follow_up, target_receipt, action, run_id=facts["attempt"]["run_id"])
        authorization["native_snapshot"] = immutable_artifact(root, "native-evidence-snapshots", snapshot)
        contract = frozen_contract(run_dir)
        receipt = {"schema": RECEIPT_SCHEMA, "schema_version": 1, "sequence": len(events) + 1,
                   "previous_receipt_sha256": ledger["events"][-1]["sha256"] if events else None,
                   "action": action, "run_id": contract["run_id"], "issue_id": request["issue_id"],
                   "attempt_id": facts["attempt"]["attempt_id"], "repo": identity,
                   "candidate_content_manifest_sha256": candidate_content_manifest_sha256(repo),
                   "reviewed_scope_hash": contract["scope_hash"], "reviewed_contract_sha256": digest(contract),
                   "handoff_sha256": hashlib.sha256(regular_bytes(run_dir / "artifacts/handoff.md")).hexdigest(),
                   "facts": facts, "facts_sha256": digest(facts), "authorization": authorization, "follow_up": follow_up,
                   "target_identity_snapshot": immutable_artifact(root, "target-identity-snapshots", target_receipt) if target_receipt else None,
                   "request_fingerprint_sha256": digest(request)}
        # stdout/索引登记中断后重跑相同请求复用原事件，撤销后的同请求则追加重新委派。
        prior = next((event for event, reference in reversed(events) if event["issue_id"] == receipt["issue_id"]), None)
        if prior and all(prior.get(name) == receipt.get(name) for name in receipt if name not in {"sequence", "previous_receipt_sha256"}):
            reference = next(reference for event, reference in events if event == prior)
            return {"status": "accepted", "reused": True, "receipt_path": reference["path"], "receipt_sha256": reference["sha256"]}
        validate_receipt(receipt, run_dir, identity, receipt["candidate_content_manifest_sha256"])
        reference = immutable_artifact(root, "receipts", receipt)
        ledger["events"].append(reference)
        atomic_bytes(root / "ledger.json", canonical_json_bytes(ledger))
        return {"status": "accepted", "reused": False, "action": action,
                "receipt_path": reference["path"], "receipt_sha256": reference["sha256"], "follow_up": follow_up}


def recovery_input_fingerprint(repo: Path, run_dir: Path, request_file: Path) -> str:
    paths = [request_file, run_dir / "summary.json", run_dir / "artifacts/inputs/scope.contract.json", run_dir / "artifacts/handoff.md"]
    try:
        request = read_object(request_file)
        paths.extend([Path(request.get("target_receipt_path", ".")), Path(request.get("authorization", {}).get("transcript_path", "."))])
        paths.extend(sorted((run_dir / "artifacts/fix-attempts").glob("*/*.json")))
    except (OSError, ValueError):
        pass
    task_query_command = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", "")
    paths.extend(Path(argument) for argument in shlex.split(task_query_command)[1:] if Path(argument).is_file())
    return digest({"files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else "missing_or_unsafe" for path in paths},
                   "candidate": candidate_content_manifest_sha256(repo), "verifier_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "target_query_command": task_query_command,
                   "runtime_port": os.environ.get("KANBAN_RUNTIME_PORT")})


@guard_normal_mutation("repo", "resume-elevated-issue-delegation", "run_dir")
def resume_delegation(repo: Path, run_dir: Path, request_file: Path) -> dict[str, Any]:
    recovery_directory = run_dir / DELEGATION_DIRECTORY
    recovery_lock_path = recovery_directory / "recovery-state-update.lock"
    assert_safe_artifact_path(recovery_lock_path, run_dir)
    recovery_directory.mkdir(parents=True, exist_ok=True)
    with recovery_lock_path.open("a") as recovery_lock:
        fcntl.flock(recovery_lock, fcntl.LOCK_EX)
        return resume_delegation_with_exclusive_recovery_state_access(repo, run_dir, request_file)


def resume_delegation_with_exclusive_recovery_state_access(repo: Path, run_dir: Path, request_file: Path) -> dict[str, Any]:
    root = run_dir / DELEGATION_DIRECTORY
    request_snapshot = read_object(request_file)
    raw_request_digest = digest(request_snapshot)
    binding_reference_path = root / "request-bindings" / f"{raw_request_digest}.json"
    assert_safe_artifact_path(binding_reference_path, run_dir)
    root.mkdir(parents=True, exist_ok=True)
    binding_lock_path = root / "request-binding-registration.lock"
    assert_safe_artifact_path(binding_lock_path, run_dir)
    with binding_lock_path.open("a") as binding_lock:
        fcntl.flock(binding_lock, fcntl.LOCK_EX)
        if binding_reference_path.exists():
            stored = read_object(binding_reference_path)
            if stored.get("original_request_sha256") != raw_request_digest:
                raise DelegationEvidenceError("持久请求绑定身份不符")
            request_reference = stored["request_reference"]
            expected = root / "pending-requests" / f"{request_reference.get('sha256')}.json"
            assert_safe_artifact_path(expected, run_dir)
            if request_reference.get("path") != str(expected):
                raise DelegationEvidenceError("持久请求逃逸原 run")
            request_snapshot, _sha = read_canonical(expected, request_reference["sha256"])
        else:
            if "candidate_binding" not in request_snapshot:
                request_snapshot["candidate_binding"] = current_request_candidate_binding(repo, run_dir, request_snapshot["issue_id"])
            request_reference = immutable_artifact(root, "pending-requests", request_snapshot)
            atomic_bytes(binding_reference_path, canonical_json_bytes({"original_request_sha256": raw_request_digest, "request_reference": request_reference}))
    request_file = Path(request_reference["path"])
    fingerprint = recovery_input_fingerprint(repo, run_dir, request_file)
    state_path = root / "recovery-attempts" / f"{fingerprint}.json"
    assert_safe_artifact_path(state_path, run_dir)
    previous = read_object(state_path) if state_path.exists() else {"attempt_count": 0, "history": []}
    if previous.get("status") == "accepted":
        request = read_object(request_file)
        _ledger, events = read_ledger(run_dir)
        prior = next((event for event, reference in reversed(events) if event["issue_id"] == request.get("issue_id")), None)
        if prior and prior.get("request_fingerprint_sha256") == digest(request):
            validate_receipt(prior, run_dir, git_identity(repo), candidate_content_manifest_sha256(repo))
            return {**previous["result"], "reused": True}
    if previous.get("status") != "accepted" and previous["attempt_count"] >= 3:
        return {"status": previous["status"], "reason": previous["reason"], "automatic_retry_budget_exhausted": True,
                "recovery_condition": "请求、原生记录、目标回执、attempt 或候选输入变化后自动重验", "input_fingerprint_sha256": fingerprint}
    try:
        result = record_delegation(repo, run_dir, read_object(request_file))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"status": getattr(exc, "status", "invalid_evidence"), "reason": str(exc)}
    previous.update({"input_fingerprint_sha256": fingerprint, "attempt_count": previous["attempt_count"] + 1,
                     "request_file": str(request_file), "status": result["status"], "reason": result.get("reason"), "result": result,
                     "recovery_condition": "原生记录、目标回执、attempt、请求或候选输入变化", "updated_at": datetime.now(timezone.utc).isoformat()})
    previous["history"].append({"attempt_number": previous["attempt_count"], **result})
    atomic_bytes(state_path, canonical_json_bytes(previous))
    return {**result, "recovery_state_path": str(state_path), "attempt_count": previous["attempt_count"]}


def resume_pending_delegations_for_authorized_land(repo: Path, run_dir: Path) -> list[dict[str, Any]]:
    results = []
    seen: set[str] = set()
    for path in sorted((run_dir / DELEGATION_DIRECTORY / "recovery-attempts").glob("*.json")):
        state = read_object(path)
        request = state.get("request_file")
        if state.get("status") == "waiting_for_machine_evidence" and isinstance(request, str) and request not in seen:
            seen.add(request)
            results.append(resume_delegation(repo, run_dir, Path(request)))
    return results


def inspect_delegation_completion(repo: Path, run_dir: Path) -> dict[str, Any]:
    accepted = accepted_delegations(repo, run_dir)
    delegated_ids = {entry["issue_id"] for entry in accepted}
    outcomes = {name: 0 for name in ("fixed", "false_positive", "submitted_to_kanban_backlog", "elevated", "failed", "not_applied")}
    for path in sorted((run_dir / "artifacts/fix-issues").glob("*.json")):
        issue = read_object(path)
        facts = latest_issue_facts(run_dir, issue["issue_id"], require_elevated=False)
        status, result_status = facts["attempt"].get("status"), facts["result"].get("status")
        if status == "applied" and result_status == "fixed":
            outcomes["fixed"] += 1
        elif status == result_status and status in {"elevated", "failed", "false_positive"}:
            outcomes[status] += 1
        elif status == result_status == "validated_and_submitted_to_kanban_backlog":
            outcomes["submitted_to_kanban_backlog"] += 1
        else:
            outcomes["not_applied"] += 1
    outcomes.update({"user_delegated": len(delegated_ids), "blocking_elevated": outcomes["elevated"] - len(delegated_ids)})
    return {"status": "accepted", "user_delegated_elevated_issues": accepted, "outcome_counts": outcomes,
            "issues_remain_unresolved": bool(outcomes["elevated"] or outcomes["failed"] or outcomes["not_applied"]),
            "has_no_unhandled_blocking_issue_outcomes": not any(outcomes[name] for name in ("blocking_elevated", "failed", "not_applied"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录、核验并恢复用户委派的 elevated 问题，不授予执行权限。")
    parser.add_argument("subcommand", choices=("inspect", "record", "verify", "resume"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--request-file")
    args = parser.parse_args(argv)
    if args.subcommand in {"record", "resume"} and not args.request_file:
        parser.error("record/resume 必须显式传 --request-file")
    repo, run_dir = Path(args.repo).resolve(), Path(args.run_dir).resolve()
    try:
        if args.subcommand in {"record", "resume"}:
            # record 也留恢复输入及失败历史；resume 使用相同的输入预算。
            result = resume_delegation(repo, run_dir, Path(args.request_file).resolve())
        else:
            result = inspect_delegation_completion(repo, run_dir)
    except BackupRouteOwned as exc:
        result = {"status": "execution_prohibited", "reason": str(exc)}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {"status": getattr(exc, "status", "invalid_evidence"), "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"accepted": 0, "waiting_for_machine_evidence": 10, "invalid_evidence": 11, "execution_prohibited": 12}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
