"""用户委派保持未解决事实，同时使已授权 Land 能正常收尾。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_reviewed_candidate_completion_qualification import completed_machine_issue_run, source_and_index_snapshot
from test_reviewed_task_candidate_evidence import SCRIPT_DIRECTORY, canonical_bytes, git, record_terminal_fix_attempt, run_writer
from test_rvf_land_single_step_prepare_and_commit import commit, land_environment, prepare, record_validation, run_land

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import rvf_elevated_issue_delegation as delegation
import write_reviewed_task_candidate_evidence as evidence


def delegated_issue_fixture(tmp_path: Path, *, count: int = 2, mode: str = "approve_proposed_delegation", expansion_paths: list[str] | None = None) -> tuple[Path, Path, Path]:
    repo, run_dir = completed_machine_issue_run(tmp_path, count, expansion_paths=expansion_paths)
    attempt_dir = run_dir / "artifacts/fix-attempts/attempt-RVF-002-1"
    for name in ("attempt.json", "result.json"):
        path = attempt_dir / name
        payload = json.loads(path.read_bytes())
        payload["status"] = "elevated"
        path.write_text(json.dumps(payload))
    (run_dir / "artifacts/handoff.md").write_text(
        f"- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 {count} 条，{count-1} 真实修复 / 0 误报 / 1 升级\n"
        + "\n".join(f"- **[{'elevated' if number == 2 else 'fixed'}]** RVF-{number:03d}" for number in range(1, count + 1)) + "\n")
    follow_up_project = tmp_path / "follow-up-project"
    follow_up_project.mkdir()
    follow_up = {"project_path": str(follow_up_project), "workspace_id": "fixture-project", "task_id": "target-task", "task_created_at": 1792950703909}
    proposal = "针对 run rvf-candidate-evidence-test，建议将 RVF-002 的配置并发覆盖问题交给独立任务；采用独立副本和受控重载来可靠传播设置。"
    user_text = "可以，同意这个方案，请创建一个新 task 解决这个问题。" if mode == "approve_proposed_delegation" else "将 RVF-002 委派给 target-task 解决。"
    native = tmp_path / "isolated-home/.codex/sessions/source-native-session.jsonl"
    native.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "source-session", "originator": "codex-tui", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": proposal}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "source-turn", "turn_attribution": {"turn_trigger": "user"}}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": user_text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "source-turn", "content_item_metadata": [{"provenance": {"type": "user"}}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "source-turn", "item": {
            "type": "UserMessage", "id": "source-message", "content": [{"type": "text", "text": user_text}]}}},
    ]
    native.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    target_receipt = tmp_path / "target-receipt.json"
    target_receipt.write_text(json.dumps({"ok": True, "workspacePath": str(follow_up_project), "workspaceId": "fixture-project",
                                        "task": {"id": "target-task", "createdAt": follow_up["task_created_at"], "title": "后续修复", "prompt": proposal}}))
    authorization = {"transcript_path": str(native), "session_id": "source-session", "native_turn_id": "source-turn",
                     "native_user_message_id": "source-message", "message_text": user_text, "mode": mode,
                     "proposal_context_quote": proposal, "target_binding_quote": "独立副本和受控重载"}
    request = {"schema_version": 1, "run_id": "rvf-candidate-evidence-test", "issue_id": "RVF-002", "action": "delegate",
               "follow_up": follow_up, "target_receipt_path": str(target_receipt), "authorization": authorization}
    request_file = tmp_path / "delegation-request.json"
    request_file.write_bytes(canonical_bytes(request))
    return repo, run_dir, request_file


def run_delegation(tmp_path: Path, repo: Path, run_dir: Path, action: str, request_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    unavailable_control_plane = tmp_path / "isolated-kanban-control-plane-unavailable.py"
    unavailable_control_plane.write_text("import sys\nprint('隔离控制面暂不可用', file=sys.stderr)\nraise SystemExit(1)\n")
    return subprocess.run([sys.executable, str(SCRIPT_DIRECTORY / "rvf_command_line_shortcut_entrypoint.py"), "elevated-issue-delegation",
                           action, "--repo", str(repo), "--run-dir", str(run_dir), *(["--request-file", str(request_file)] if request_file else [])],
                          cwd=repo, env={**land_environment(tmp_path), "HOME": str(tmp_path / "isolated-home"),
                                         "CODEX_RVF_CLINE_KANBAN_TASK_CMD": shlex.join([sys.executable, str(unavailable_control_plane)])}, text=True, capture_output=True)


@pytest.mark.parametrize("mode", ["approve_proposed_delegation", "direct_delegation"])
def test_real_user_delegation_preserves_counts_and_allows_land_without_waiting_for_followup(tmp_path: Path, mode: str) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path, mode=mode)
    original_handoff = (run_dir / "artifacts/handoff.md").read_bytes()
    original_attempt = (run_dir / "artifacts/fix-attempts/attempt-RVF-002-1/attempt.json").read_bytes()
    (repo / "tracked.txt").write_text("reviewed candidate change\n")
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable, "-c", "print('通过')").returncode == 0
    refused = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert refused.returncode == 28
    recorded = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    assert run_delegation(tmp_path, repo, run_dir, "verify").returncode == 0
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 0, checked.stdout
    facts = json.loads(checked.stdout)
    assert facts["outcome_counts"] == {"fixed": 1, "false_positive": 0, "submitted_to_kanban_backlog": 0,
                                                        "elevated": 1, "user_delegated": 1, "blocking_elevated": 0}
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert result["status"] == "committed"
    assert result["issues_remain_unresolved"] is True
    assert result["base_sync_completed"] is False
    payload, _path, _sha = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == 3
    assert payload["review"]["conclusion"] == delegation.DELEGATION_CONCLUSION
    assert payload["required_capabilities"] == [delegation.DELEGATION_CAPABILITY]
    head = git(repo, "rev-parse", "HEAD")
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    repeated = commit(tmp_path, repo, run_dir)
    assert repeated.returncode == 0, repeated.stdout
    assert json.loads(repeated.stdout)["status"] == "nothing_to_commit"
    assert git(repo, "rev-parse", "HEAD") == head
    assert (run_dir / "artifacts/handoff.md").read_bytes() == original_handoff
    assert (run_dir / "artifacts/fix-attempts/attempt-RVF-002-1/attempt.json").read_bytes() == original_attempt


@pytest.mark.parametrize("mutation", ["no_native_provenance", "role_user_only", "agent_turn", "agent_created_task", "wrong_session", "wrong_task", "wrong_project", "wrong_workspace", "wrong_creation_time", "unrelated_proposal", "stop", "unapproved_proposal", "rejected_proposal", "rejected_delegation"])
def test_invalid_authorization_and_identity_never_bypass_land(tmp_path: Path, mutation: str) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    native_path = Path(request["authorization"]["transcript_path"])
    native = [json.loads(line) for line in native_path.read_text().splitlines()]
    if mutation == "no_native_provenance":
        native[3]["payload"].pop("internal_chat_message_metadata_passthrough")
    elif mutation == "role_user_only":
        native.pop()
    elif mutation == "agent_turn":
        native[2]["payload"]["turn_attribution"]["turn_trigger"] = "hook"
    elif mutation == "agent_created_task":
        native[-1]["payload"]["item"]["content"][0]["text"] = "已建卡，请参考 handoff。"
    elif mutation == "wrong_session":
        request["authorization"]["session_id"] = "other-session"
    elif mutation in {"wrong_task", "wrong_project", "wrong_workspace", "wrong_creation_time"}:
        name, value = {"wrong_task": ("task_id", "wrong-target"), "wrong_project": ("project_path", str(tmp_path)),
                       "wrong_workspace": ("workspace_id", "other-workspace"), "wrong_creation_time": ("task_created_at", 123)}[mutation]
        request["follow_up"][name] = value
    elif mutation == "unrelated_proposal":
        request["authorization"]["proposal_context_quote"] = "另一个 RVF-002 的委派建议"
    elif mutation in {"stop", "unapproved_proposal", "rejected_proposal", "rejected_delegation"}:
        text = {"stop": "停止。禁止继续。可以，请创建一个新 task。", "unapproved_proposal": "请解释这个方案。",
                "rejected_proposal": "不同意，请创建一个新 task 进一步调查。", "rejected_delegation": "不要将 RVF-002 委派给 target-task。"}[mutation]
        request["authorization"]["message_text"] = text
        native[3]["payload"]["content"][0]["text"] = text
        native[4]["payload"]["item"]["content"][0]["text"] = text
    native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in native) + "\n")
    request_file.write_bytes(canonical_bytes(request))
    before = source_and_index_snapshot(repo)
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == (12 if mutation == "stop" else 10 if mutation == "role_user_only" else 11), result.stdout + result.stderr
    assert run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir)).returncode == 28
    assert source_and_index_snapshot(repo) == before


@pytest.mark.parametrize("mutation", ["receipt_tamper", "snapshot_tamper", "candidate_change", "newer_failed", "newer_started", "target_reference_tamper", "counts_tamper", "downgrade"])
def test_reader_rejects_changed_binding_newer_failure_and_forged_green_counts(tmp_path: Path, mutation: str) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    assert run_writer(repo, run_dir).returncode == 0
    payload, _path, _sha = evidence.validated_reviewed_task_candidate_evidence(repo)
    reference = payload["review"]["user_delegated_elevated_issues"][0]
    if mutation in {"receipt_tamper", "snapshot_tamper"}:
        receipt_path = Path(reference["receipt_path"])
        if mutation == "snapshot_tamper":
            receipt = json.loads(receipt_path.read_bytes())
            receipt_path = Path(receipt["authorization"]["native_snapshot"]["path"])
        receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
    elif mutation == "candidate_change":
        (repo / "tracked.txt").write_text("changed after acceptance\n")
    elif mutation in {"newer_failed", "newer_started"}:
        directory = record_terminal_fix_attempt(run_dir, "RVF-002", status="failed", attempt_number=2)
        if mutation == "newer_started":
            (directory / "result.json").unlink()
    else:
        if mutation == "target_reference_tamper":
            reference["follow_up"]["task_id"] = "other-task"
        elif mutation == "counts_tamper":
            payload["review"]["outcome_counts"]["fixed"] = 2
        else:
            payload["schema_version"] = 1
            payload["review"]["conclusion"] = "fixes_applied_and_validated"
        delegation.atomic_bytes(_path, canonical_bytes(payload))
        delegation.atomic_bytes(Path(f"{_path}.sha256"), (delegation.digest(payload) + "\n").encode())
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)


def test_historical_acceptance_survives_later_turn_task_rename_and_missing_live_query(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 0, result.stdout
    request = json.loads(request_file.read_bytes())
    Path(request["authorization"]["transcript_path"]).unlink()
    Path(request["target_receipt_path"]).unlink()
    assert run_delegation(tmp_path, repo, run_dir, "verify").returncode == 0
    assert run_writer(repo, run_dir).returncode == 0
    assert len(delegation.read_ledger(run_dir)[1]) == 1


def test_missing_machine_evidence_resumes_with_bounded_budget_and_readonly_check_does_not_retry(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    target = Path(request["target_receipt_path"])
    target_bytes = target.read_bytes()
    target.unlink()
    for expected in (1, 2, 3):
        result = run_delegation(tmp_path, repo, run_dir, "resume", request_file)
        assert result.returncode == 10
        assert json.loads(result.stdout)["attempt_count"] == expected
    exhausted = json.loads(run_delegation(tmp_path, repo, run_dir, "resume", request_file).stdout)
    assert exhausted["automatic_retry_budget_exhausted"] is True
    state = {str(path): path.read_bytes() for path in (run_dir / delegation.DELEGATION_DIRECTORY).rglob("*") if path.is_file()}
    run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert state == {str(path): path.read_bytes() for path in (run_dir / delegation.DELEGATION_DIRECTORY).rglob("*") if path.is_file()}
    target.write_bytes(target_bytes)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout
    assert json.loads(landed.stdout)["elevated_issue_delegation_recovery"][0]["status"] == "accepted"


def test_partial_delegation_leaves_other_elevated_issue_blocking(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path, count=3)
    for name in ("attempt.json", "result.json"):
        path = run_dir / "artifacts/fix-attempts/attempt-RVF-003-1" / name
        value = json.loads(path.read_bytes()); value["status"] = "elevated"; path.write_text(json.dumps(value))
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text(handoff.read_text().replace("2 真实修复", "1 真实修复").replace("1 升级", "2 升级").replace("[fixed]** RVF-003", "[elevated]** RVF-003"))
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 28 and "RVF-003" in checked.stdout


def test_idempotent_record_and_revocation_append_history_and_reject_former_evidence(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    assert len(delegation.read_ledger(run_dir)[1]) == 1
    assert run_writer(repo, run_dir).returncode == 0
    request = json.loads(request_file.read_bytes())
    request["action"] = "revoke"
    request["authorization"].update({"native_user_message_id": "revoke-message", "native_turn_id": "revoke-turn", "message_text": "撤销 RVF-002 的委派。"})
    native = Path(request["authorization"]["transcript_path"])
    with native.open("a") as handle:
        for record in [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "revoke-turn", "turn_attribution": {"turn_trigger": "user"}}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "撤销 RVF-002 的委派。"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "revoke-turn", "content_item_metadata": [{"provenance": {"type": "user"}}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "revoke-turn", "item": {
                "type": "UserMessage", "id": "revoke-message", "content": [{"type": "input_text", "text": "撤销 RVF-002 的委派。"}]}}},
        ]:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    revoke_file = tmp_path / "revoke-request.json"; revoke_file.write_bytes(canonical_bytes(request))
    revoked = run_delegation(tmp_path, repo, run_dir, "record", revoke_file)
    assert revoked.returncode == 0, revoked.stdout
    assert len(delegation.read_ledger(run_dir)[1]) == 2
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)
    assert run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir)).returncode == 28


@pytest.mark.parametrize("mutation", ["valid", "missing_declaration", "patch_tamper", "receipt_digest_tamper"])
def test_v3_delegation_also_requires_complete_scope_expansion_proof(tmp_path: Path, mutation: str) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path, expansion_paths=["scope-expansion-test.txt"])
    if mutation == "missing_declaration":
        for filename in ("attempt.json", "result.json"):
            path = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1" / filename
            value = json.loads(path.read_bytes());value["scope_expansion"]["declared_paths"] = [];path.write_text(json.dumps(value))
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    if mutation == "missing_declaration":
        assert landed.returncode == 28
        return
    assert landed.returncode == 0, landed.stdout + landed.stderr
    payload, _path, _sha = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == 3
    assert payload["scope"]["applied_scope_expansion_receipt_path"]
    if mutation == "patch_tamper":
        (run_dir / "artifacts/fix-attempts/attempt-RVF-001-1/fix.patch").write_text("tampered patch\n")
    elif mutation == "receipt_digest_tamper":
        Path(payload["scope"]["applied_scope_expansion_receipt_path"]).write_bytes(b"{}")
    else:
        return
    with pytest.raises(ValueError):
        evidence.validated_reviewed_task_candidate_evidence(repo)


def test_interrupted_ledger_registration_reuses_content_addressed_receipt_and_redelegation_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    original_atomic_bytes = delegation.atomic_bytes
    def fail_before_ledger_registration(path: Path, encoded: bytes) -> None:
        if path.name == "ledger.json":
            raise OSError("模拟登记前进程中断")
        original_atomic_bytes(path, encoded)
    with monkeypatch.context() as fault:
        fault.setattr(delegation, "atomic_bytes", fail_before_ledger_registration)
        with pytest.raises(OSError):
            delegation.record_delegation(repo, run_dir, request)
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()
    receipts = list((run_dir / delegation.DELEGATION_DIRECTORY / "receipts").glob("*.json"))
    assert len(receipts) == 1
    result = delegation.record_delegation(repo, run_dir, request)
    assert result["status"] == "accepted"
    assert len(delegation.read_ledger(run_dir)[1]) == 1
    assert len(list(receipts[0].parent.glob("*.json"))) == 1


def test_native_capitalized_text_and_multi_part_attachments_use_event_time_snapshot(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    native_path = Path(request["authorization"]["transcript_path"])
    native = [json.loads(line) for line in native_path.read_text().splitlines()]
    proposal = request["authorization"]["proposal_context_quote"]
    native[1] = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "AgentMessage", "content": [{"type": "Text", "text": proposal}]}}}
    text = request["authorization"]["message_text"]
    native[-1]["payload"]["item"]["content"] = [{"type": "input_text", "text": text[:5]}, {"type": "text", "text": text[5:]}, {"type": "input_image", "image_url": "isolated-fixture"}]
    native.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later-turn", "turn_attribution": {"turn_trigger": "user"}}})
    native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in native) + "\n")
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 0, result.stdout
    assert run_writer(repo, run_dir).returncode == 0


def test_brief_user_approval_accepts_immediately_preceding_explicit_task_delegation_proposal(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    native_path = Path(request["authorization"]["transcript_path"])
    records = [json.loads(line) for line in native_path.read_text().splitlines()]
    request["authorization"]["message_text"] = "可以。"
    records[3]["payload"]["content"][0]["text"] = "可以。"
    records[4]["payload"]["item"]["content"][0]["text"] = "可以。"
    native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    request_file.write_bytes(canonical_bytes(request))
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 0, result.stdout


def test_waiting_request_does_not_automatically_rebind_to_changed_candidate(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    target = Path(request["target_receipt_path"])
    target_bytes = target.read_bytes(); target.unlink()
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 10
    target.write_bytes(target_bytes)
    (repo / "tracked.txt").write_text("changed while waiting for machine evidence\n")
    result = run_delegation(tmp_path, repo, run_dir, "resume", request_file)
    assert result.returncode == 11 and "不能自动重绑" in result.stdout


def test_local_control_plane_recovers_missing_receipt_after_query_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    target = Path(request["target_receipt_path"])
    target_bytes = target.read_text(); target.unlink()
    control_plane = tmp_path / "isolated-kanban-control-plane.py"
    control_plane.write_text("raise SystemExit(1)\n")
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", shlex.join([sys.executable, str(control_plane)]))
    for _ in range(3):
        assert delegation.resume_delegation(repo, run_dir, request_file)["status"] == "waiting_for_machine_evidence"
    assert delegation.resume_delegation(repo, run_dir, request_file)["automatic_retry_budget_exhausted"] is True
    control_plane.write_text("print(" + repr(target_bytes) + ")\n")
    assert delegation.resume_delegation(repo, run_dir, request_file)["status"] == "accepted"
    control_plane.unlink()
    assert delegation.accepted_delegations(repo, run_dir)[0]["follow_up"]["task_id"] == "target-task"


def test_delegation_mutation_respects_existing_unique_executor_takeover(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    from independent_backup_route import control_directory

    owner = control_directory(repo) / "active-owner"
    owner.mkdir(parents=True)
    (owner / "manual-operation-id").write_text("isolated-other-executor\n")
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 12, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "execution_prohibited"
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()


def test_concurrent_recovery_calls_share_the_same_three_attempt_budget(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    request = json.loads(request_file.read_bytes())
    Path(request["target_receipt_path"]).unlink()
    counter = tmp_path / "actual-control-plane-query-attempts.txt"
    control_plane = tmp_path / "isolated-counting-control-plane.py"
    control_plane.write_text("from pathlib import Path\nwith Path(" + repr(str(counter)) + ").open('a') as handle: handle.write('query\\n')\nraise SystemExit(1)\n")
    command = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_elevated_issue_delegation.py"), "resume", "--repo", str(repo), "--run-dir", str(run_dir), "--request-file", str(request_file)]
    environment = {**land_environment(tmp_path), "HOME": str(tmp_path / "isolated-home"),
                   "CODEX_RVF_CLINE_KANBAN_TASK_CMD": shlex.join([sys.executable, str(control_plane)])}
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: subprocess.run(command, env=environment, capture_output=True, text=True), range(5)))
    assert all(result.returncode == 10 for result in results), [result.stdout + result.stderr for result in results]
    assert len(counter.read_text().splitlines()) == 3
    states = list((run_dir / delegation.DELEGATION_DIRECTORY / "recovery-attempts").glob("*.json"))
    assert len(states) == 1 and json.loads(states[0].read_bytes())["attempt_count"] == 3


def test_native_approval_cannot_be_reused_for_another_run_with_same_issue_id(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    replacement_run_id = "rvf-other-review-with-the-same-issue-number"
    for path in [run_dir / "summary.json", *run_dir.glob("artifacts/**/*.json")]:
        encoded = path.read_text().replace("rvf-candidate-evidence-test", replacement_run_id)
        payload = json.loads(encoded)
        path.write_bytes(canonical_bytes(payload))
        if Path(f"{path}.sha256").exists():
            Path(f"{path}.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
    request = json.loads(request_file.read_bytes()); request["run_id"] = replacement_run_id
    request_file.write_bytes(canonical_bytes(request))
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 11, result.stdout + result.stderr
    assert "没有绑定该 run" in result.stdout
