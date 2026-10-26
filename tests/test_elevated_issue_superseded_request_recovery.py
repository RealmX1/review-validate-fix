"""旧原生决定的恢复与重放不能覆盖已经接受的较新用户决定。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

from test_user_delegated_elevated_issue_disposition import (
    append_native_issue_action_request,
    delegated_issue_fixture,
    delegation,
    run_delegation,
)
from test_reviewed_task_candidate_evidence import canonical_bytes, git
from test_rvf_land_single_step_prepare_and_commit import commit, prepare, record_validation


def record_native_revocation(tmp_path: Path, repo: Path, run_dir: Path, original: Path) -> Path:
    request = append_native_issue_action_request(
        tmp_path, original, action="revoke", message_text="撤销 RVF-002 的委派。", decision_id="newer-revocation"
    )
    completed = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return request


def immutable_receipt_bytes(run_dir: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (run_dir / delegation.DELEGATION_DIRECTORY / "receipts").glob("*")
    }


@pytest.mark.parametrize("trigger", ["manual_record", "land_auto_resume"])
def test_waiting_delegate_recovery_cannot_override_a_newer_native_revocation(
    tmp_path: Path, trigger: str
) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    (repo / "tracked.txt").write_text("已审候选内容\n")
    assert record_validation(tmp_path, repo, run_dir, "replay-recovery-validation", sys.executable, "-c", "print('通过')").returncode == 0
    request = json.loads(original.read_bytes())
    target = Path(request["target_receipt_path"])
    target_bytes = target.read_bytes()
    target.unlink()
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 10
    target.write_bytes(target_bytes)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    record_native_revocation(tmp_path, repo, run_dir, original)
    before = immutable_receipt_bytes(run_dir)
    head = git(repo, "rev-parse", "HEAD")
    if trigger == "manual_record":
        replayed = run_delegation(tmp_path, repo, run_dir, "record", original)
        assert replayed.returncode == 0, replayed.stdout
        assert json.loads(replayed.stdout)["superseded"] is True
    else:
        assert prepare(tmp_path, repo, run_dir).returncode == 0
        replayed = commit(tmp_path, repo, run_dir)
        assert replayed.returncode == 28, replayed.stdout + replayed.stderr
        assert git(repo, "rev-parse", "HEAD") == head
    assert [event["action"] for event, _ in delegation.read_ledger(run_dir)[1]] == ["delegate", "revoke"]
    assert delegation.accepted_delegations(repo, run_dir) == []
    assert immutable_receipt_bytes(run_dir) == before


def test_unaccepted_older_decision_is_persistently_superseded_instead_of_reactivated(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    record_native_revocation(tmp_path, repo, run_dir, original)
    for _ in range(2):
        result = run_delegation(tmp_path, repo, run_dir, "record", original)
        assert result.returncode == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["status"] == "superseded"
        assert payload["recovery_state_path"]
    assert [event["action"] for event, _ in delegation.read_ledger(run_dir)[1]] == ["revoke"]
    assert delegation.accepted_delegations(repo, run_dir) == []


def test_redelegation_requires_a_new_native_message_and_old_revocation_cannot_revoke_it(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    revoke = record_native_revocation(tmp_path, repo, run_dir, original)
    redelegate = append_native_issue_action_request(
        tmp_path, original, action="delegate", message_text="将 RVF-002 重新委派给 target-task 解决。", decision_id="newest-redelegation"
    )
    accepted = run_delegation(tmp_path, repo, run_dir, "record", redelegate)
    assert accepted.returncode == 0, accepted.stdout
    accepted_reference = delegation.accepted_delegations(repo, run_dir)
    assert len(accepted_reference) == 1
    for old_request in (original, revoke):
        replayed = run_delegation(tmp_path, repo, run_dir, "record", old_request)
        assert replayed.returncode == 0, replayed.stdout
        assert json.loads(replayed.stdout)["superseded"] is True
    assert len(delegation.read_ledger(run_dir)[1]) == 3
    assert delegation.accepted_delegations(repo, run_dir) == accepted_reference


def test_same_native_message_cannot_create_a_second_event_by_changing_request_metadata(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    first = json.loads(run_delegation(tmp_path, repo, run_dir, "record", original).stdout)
    request = json.loads(original.read_bytes())
    request["agent_replay_metadata"] = "不同进程重启后重放同一用户决定"
    replay_file = tmp_path / "same-native-message-changed-request.json"
    replay_file.write_bytes(canonical_bytes(request))
    repeated = run_delegation(tmp_path, repo, run_dir, "record", replay_file)
    assert repeated.returncode == 0, repeated.stdout
    result = json.loads(repeated.stdout)
    assert result["reused"] is True
    assert result["receipt_sha256"] == first["receipt_sha256"]
    assert len(delegation.read_ledger(run_dir)[1]) == 1


def test_historical_replay_preserves_revocation_after_live_native_and_target_sources_disappear(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    record_native_revocation(tmp_path, repo, run_dir, original)
    request = json.loads(original.read_bytes())
    Path(request["authorization"]["transcript_path"]).unlink()
    Path(request["target_receipt_path"]).unlink()
    replayed = run_delegation(tmp_path, repo, run_dir, "record", original)
    assert replayed.returncode == 0, replayed.stdout
    assert json.loads(replayed.stdout)["superseded"] is True
    assert len(delegation.read_ledger(run_dir)[1]) == 2
    assert delegation.accepted_delegations(repo, run_dir) == []


def test_same_native_message_cannot_be_reused_for_a_different_target(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    request = json.loads(original.read_bytes())
    request["follow_up"]["task_id"] = "another-target-task"
    target = json.loads(Path(request["target_receipt_path"]).read_bytes())
    target["task"]["id"] = "another-target-task"
    alternate_target_receipt = tmp_path / "another-target-receipt.json"
    alternate_target_receipt.write_text(json.dumps(target))
    request["target_receipt_path"] = str(alternate_target_receipt)
    altered = tmp_path / "reused-native-message-different-target.json"
    altered.write_bytes(canonical_bytes(request))
    result = run_delegation(tmp_path, repo, run_dir, "record", altered)
    assert result.returncode == 11, result.stdout
    assert len(delegation.read_ledger(run_dir)[1]) == 1
    assert delegation.accepted_delegations(repo, run_dir)[0]["follow_up"]["task_id"] == "target-task"


@pytest.mark.parametrize("unprovable_order", ["different_session", "rewritten_prefix"])
def test_unproven_native_order_does_not_override_a_known_revocation(
    tmp_path: Path, unprovable_order: str
) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    record_native_revocation(tmp_path, repo, run_dir, original)
    requested = append_native_issue_action_request(
        tmp_path, original, action="delegate", message_text="将 RVF-002 委派给 target-task 解决。", decision_id="unproven-order-delegation"
    )
    request = json.loads(requested.read_bytes())
    native_path = Path(request["authorization"]["transcript_path"])
    records = [json.loads(line) for line in native_path.read_text().splitlines()]
    original_native_records = copy.deepcopy(records)
    if unprovable_order == "different_session":
        request["authorization"]["session_id"] = "another-native-session"
        records[0]["payload"]["id"] = "another-native-session"
        records = records[:2] + records[-3:]
        native_path = native_path.with_name("another-native-session.jsonl")
        request["authorization"]["transcript_path"] = str(native_path)
    else:
        records[1]["timestamp"] = "2099-01-01T00:00:00Z"
    for record in records:
        record["timestamp"] = "2099-01-01T00:00:00Z"
    native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    requested.write_bytes(canonical_bytes(request))
    result = run_delegation(tmp_path, repo, run_dir, "record", requested)
    assert result.returncode == 10, result.stdout
    assert json.loads(result.stdout)["status"] == "waiting_for_machine_evidence"
    assert len(delegation.read_ledger(run_dir)[1]) == 2
    assert delegation.accepted_delegations(repo, run_dir) == []
    if unprovable_order == "rewritten_prefix":
        for _ in range(2):
            assert run_delegation(tmp_path, repo, run_dir, "resume", requested).returncode == 10
        exhausted = run_delegation(tmp_path, repo, run_dir, "resume", requested)
        assert json.loads(exhausted.stdout)["automatic_retry_budget_exhausted"] is True
        native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in original_native_records) + "\n")
        recovered = run_delegation(tmp_path, repo, run_dir, "resume", requested)
        assert recovered.returncode == 0, recovered.stdout
        assert len(delegation.read_ledger(run_dir)[1]) == 3
        assert len(delegation.accepted_delegations(repo, run_dir)) == 1


def test_historical_duplicate_receipt_does_not_reactivate_a_superseded_decision(tmp_path: Path) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    record_native_revocation(tmp_path, repo, run_dir, original)
    ledger, events = delegation.read_ledger(run_dir)
    duplicate = copy.deepcopy(events[0][0])
    duplicate.update(sequence=3, previous_receipt_sha256=ledger["events"][-1]["sha256"])
    root = run_dir / delegation.DELEGATION_DIRECTORY
    reference = delegation.immutable_artifact(root, "receipts", duplicate)
    ledger["events"].append(reference)
    delegation.atomic_bytes(root / "ledger.json", canonical_bytes(ledger))
    before = immutable_receipt_bytes(run_dir)
    assert delegation.accepted_delegations(repo, run_dir) == []
    assert len(delegation.read_ledger(run_dir)[1]) == 3
    assert immutable_receipt_bytes(run_dir) == before


def test_interrupted_recovery_registration_reuses_the_same_native_event_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    atomic_write = delegation.atomic_bytes

    def interrupt_recovery_state_registration(path: Path, content: bytes) -> None:
        if path.parent.name == "recovery-attempts":
            raise OSError("模拟回执已接受但恢复索引未登记时进程退出")
        atomic_write(path, content)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(delegation, "atomic_bytes", interrupt_recovery_state_registration)
        with pytest.raises(OSError):
            delegation.resume_delegation(repo, run_dir, original)
    assert len(delegation.read_ledger(run_dir)[1]) == 1
    before = immutable_receipt_bytes(run_dir)
    restarted = run_delegation(tmp_path, repo, run_dir, "record", original)
    assert restarted.returncode == 0, restarted.stdout
    assert json.loads(restarted.stdout)["reused"] is True
    assert immutable_receipt_bytes(run_dir) == before
