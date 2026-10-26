"""证据的历史真实性与当前收尾资格分别核验，不能删字段降级。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_reviewed_candidate_completion_qualification import completed_machine_issue_run
from test_reviewed_task_candidate_evidence import canonical_bytes, git, record_terminal_fix_attempt, run_writer
from test_user_delegated_elevated_issue_disposition import (
    append_native_issue_action_request,
    delegated_issue_fixture,
    delegation,
    evidence,
    run_delegation,
)


def publish_delegated_candidate(tmp_path: Path, *, expansion: bool = False) -> tuple[Path, Path, Path, dict, Path, str]:
    expansion_paths = ["reviewed-additional-file.txt"] if expansion else None
    repo, run_dir, request = delegated_issue_fixture(tmp_path, expansion_paths=expansion_paths)
    if expansion:
        git(repo, "add", "reviewed-additional-file.txt")
        git(repo, "commit", "-qm", "test: 提交隔离范围扩展候选")
    recorded = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    written = run_writer(repo, run_dir)
    assert written.returncode == 0, written.stdout + written.stderr
    payload, path, digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    return repo, run_dir, request, payload, path, digest


def rewrite_evidence(path: Path, payload: dict) -> None:
    delegation.atomic_bytes(path, canonical_bytes(payload))
    delegation.atomic_bytes(Path(f"{path}.sha256"), (delegation.digest(payload) + "\n").encode())


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("require_current_subject", [True, False])
def test_deleted_delegation_fields_cannot_downgrade_evidence(tmp_path: Path, version: int, require_current_subject: bool) -> None:
    repo, _run_dir, _request, payload, path, _digest = publish_delegated_candidate(tmp_path, expansion=version == 2)
    payload["schema_version"] = version
    payload["review"]["conclusion"] = "fixes_applied_and_validated"
    payload.pop("required_capabilities")
    payload["review"].pop("user_delegated_elevated_issues")
    payload["review"].pop("outcome_counts")
    rewrite_evidence(path, payload)
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo, require_current_subject=require_current_subject)


@pytest.mark.parametrize("expansion", [False, True])
def test_revoked_historical_evidence_can_be_replaced_by_a_new_native_delegation(tmp_path: Path, expansion: bool) -> None:
    repo, run_dir, original, payload, path, digest = publish_delegated_candidate(tmp_path, expansion=expansion)
    original_bytes = path.read_bytes()
    revoke = append_native_issue_action_request(tmp_path, original, action="revoke", message_text="撤销 RVF-002 的委派。", decision_id="revoke-delegation")
    revoked = run_delegation(tmp_path, repo, run_dir, "record", revoke)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)
    historical, _, historical_digest = evidence.validated_reviewed_task_candidate_evidence(repo, require_current_subject=False)
    assert historical == payload and historical_digest == digest

    redelegate = append_native_issue_action_request(tmp_path, original, action="delegate", message_text="将 RVF-002 委派给 target-task 解决。", decision_id="redelegate-issue")
    recorded = run_delegation(tmp_path, repo, run_dir, "record", redelegate)
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)
    rewritten = run_writer(repo, run_dir)
    assert rewritten.returncode == 0, rewritten.stdout + rewritten.stderr
    replacement, _, replacement_digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert replacement_digest != digest
    assert replacement["review"]["user_delegated_elevated_issues"] != payload["review"]["user_delegated_elevated_issues"]
    archived = path.parent / "immutable" / f"{digest}-reviewed-task-candidate-evidence.json"
    assert archived.read_bytes() == original_bytes
    assert replacement["receipts"]["parent_evidence_path"] == str(archived)
    assert replacement["receipts"]["parent_evidence_sha256"] == digest
    assert evidence.validated_reviewed_task_candidate_evidence(repo, archived, require_current_subject=False)[2] == digest
    repeated = run_writer(repo, run_dir)
    assert repeated.returncode == 0 and json.loads(repeated.stdout)["status"] == "reused", repeated.stdout + repeated.stderr
    assert len(delegation.read_ledger(run_dir)[1]) == 3


def test_newer_failed_attempt_changes_current_qualification_but_preserves_historical_delegation(tmp_path: Path) -> None:
    repo, run_dir, _request, _payload, path, digest = publish_delegated_candidate(tmp_path)
    record_terminal_fix_attempt(run_dir, "RVF-002", status="failed", attempt_number=2)
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)
    assert evidence.validated_reviewed_task_candidate_evidence(repo, path, require_current_subject=False)[2] == digest


@pytest.mark.parametrize("version", [1, 2])
def test_nondelegated_v1_v2_history_remains_readable_after_candidate_content_changes(tmp_path: Path, version: int) -> None:
    expansion_paths = ["reviewed-additional-file.txt"] if version == 2 else None
    repo, run_dir = completed_machine_issue_run(tmp_path, expansion_paths=expansion_paths)
    if expansion_paths:
        git(repo, "add", *expansion_paths)
        git(repo, "commit", "-qm", "test: 提交隔离范围扩展候选")
    written = run_writer(repo, run_dir)
    assert written.returncode == 0, written.stdout + written.stderr
    payload, path, digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == version
    (repo / "tracked.txt").write_text("历史接受之后的另一候选\n")
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)
    assert evidence.validated_reviewed_task_candidate_evidence(repo, path, require_current_subject=False)[2] == digest


@pytest.mark.parametrize("version", [1, 2])
def test_nondelegated_v1_v2_current_read_checks_latest_canonical_attempt(tmp_path: Path, version: int) -> None:
    expansion_paths = ["reviewed-additional-file.txt"] if version == 2 else None
    repo, run_dir = completed_machine_issue_run(tmp_path, expansion_paths=expansion_paths)
    if expansion_paths:
        git(repo, "add", *expansion_paths)
        git(repo, "commit", "-qm", "test: 提交隔离范围扩展候选")
    written = run_writer(repo, run_dir)
    assert written.returncode == 0, written.stdout + written.stderr
    assert evidence.validated_reviewed_task_candidate_evidence(repo)[0]["schema_version"] == version
    record_terminal_fix_attempt(run_dir, "RVF-001", status="failed", attempt_number=2)
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)


@pytest.mark.parametrize("mutation", ["target_identity", "receipt_digest", "canonical_issue_count", "outcome_counts", "native_snapshot", "native_authorization", "incomplete_frozen_handoff"])
def test_historical_read_still_verifies_frozen_delegation_proof(tmp_path: Path, mutation: str) -> None:
    repo, run_dir, _request, payload, path, _digest = publish_delegated_candidate(tmp_path)
    payload = copy.deepcopy(payload)
    reference = payload["review"]["user_delegated_elevated_issues"][0]
    if mutation == "target_identity":
        reference["follow_up"]["task_id"] = "another-target"
    elif mutation == "receipt_digest":
        reference["receipt_sha256"] = "0" * 64
    elif mutation == "canonical_issue_count":
        payload["review"]["canonical_issue_count"] += 1
    elif mutation == "outcome_counts":
        payload["review"]["outcome_counts"]["user_delegated"] += 1
    elif mutation == "native_snapshot":
        receipt = json.loads(Path(reference["receipt_path"]).read_bytes())
        snapshot_path = Path(receipt["authorization"]["native_snapshot"]["path"])
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")
    else:
        receipt = json.loads(Path(reference["receipt_path"]).read_bytes())
        root = run_dir / delegation.DELEGATION_DIRECTORY
        if mutation == "native_authorization":
            negative_text = "不同意这个方案，不要创建新 task。"
            receipt["authorization"]["message_text"] = negative_text
            snapshot = json.loads(Path(receipt["authorization"]["native_snapshot"]["path"]).read_bytes())
            for record in snapshot["records"]:
                native_payload = record.get("payload") or {}
                if record.get("type") == "response_item" and native_payload.get("role") == "user":
                    native_payload["content"] = [{"type": "input_text", "text": negative_text}]
                if (native_payload.get("item") or {}).get("type") == "UserMessage":
                    native_payload["item"]["content"] = [{"type": "text", "text": negative_text}]
            receipt["authorization"]["native_snapshot"] = delegation.immutable_artifact(root, "native-evidence-snapshots", snapshot)
        else:
            handoff = run_dir / "artifacts/handoff.md"
            handoff.write_text(handoff.read_text().replace("review_status: COMPLETED", "review_status: FAILED"))
            receipt["handoff_sha256"] = hashlib.sha256(handoff.read_bytes()).hexdigest()
        replaced = delegation.immutable_artifact(root, "receipts", receipt)
        ledger, _events = delegation.read_ledger(run_dir)
        ledger["events"][0] = replaced
        delegation.atomic_bytes(root / "ledger.json", canonical_bytes(ledger))
        reference.update({"receipt_path": replaced["path"], "receipt_sha256": replaced["sha256"]})
    rewrite_evidence(path, payload)
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo, require_current_subject=False)


@pytest.mark.parametrize("replay_kind", ["duplicate_after_revoke", "previously_unaccepted_old_decision"])
def test_historical_evidence_rejects_old_native_decision_replayed_after_revocation(tmp_path: Path, replay_kind: str) -> None:
    repo, run_dir, original, payload, evidence_path, _digest = publish_delegated_candidate(tmp_path)
    revoked = append_native_issue_action_request(tmp_path, original, action="revoke", message_text="撤销 RVF-002 的委派。", decision_id="newer-revocation")
    completed = run_delegation(tmp_path, repo, run_dir, "record", revoked)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    ledger, events = delegation.read_ledger(run_dir)
    root = run_dir / delegation.DELEGATION_DIRECTORY
    if replay_kind == "previously_unaccepted_old_decision":
        revocation = copy.deepcopy(events[-1][0])
        revocation.update(sequence=1, previous_receipt_sha256=None)
        ledger["events"] = [delegation.immutable_artifact(root, "receipts", revocation)]
    replayed = copy.deepcopy(events[0][0])
    replayed.update(sequence=len(ledger["events"]) + 1, previous_receipt_sha256=ledger["events"][-1]["sha256"])
    reference = delegation.immutable_artifact(root, "receipts", replayed)
    ledger["events"].append(reference)
    delegation.atomic_bytes(root / "ledger.json", canonical_bytes(ledger))
    payload["review"]["user_delegated_elevated_issues"][0].update(receipt_path=reference["path"], receipt_sha256=reference["sha256"])
    rewrite_evidence(evidence_path, payload)
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo, require_current_subject=False)
    assert delegation.accepted_delegations(repo, run_dir) == []
