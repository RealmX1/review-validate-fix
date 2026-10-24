"""捕获与 reader 共用取证范围；宿主失败不推进 token，也不诱导同 turn 重投。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))
import _rvf_pyroot  # noqa: F401, E402
import rvf_automatic_closeout_bridge as bridge  # noqa: E402
from adapters.codex.review_start_independent_user_turn_evidence import read_independent_user_turn_boundary_evidence  # noqa: E402
from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified  # noqa: E402


@pytest.fixture
def capture_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    transcript = tmp_path / "synthetic-native-transcript.jsonl"
    payload = {"token": "0123456789abcdef", "session_id": "synthetic-session", "worktree": str(tmp_path / "worktree"), "candidate_summary": {}}
    receipt = {"decision_token": payload["token"], "session_id": payload["session_id"], "worktree": payload["worktree"], "transcript_path": str(transcript), "native_turn_id": "boundary-turn", "native_user_message_id": "boundary-message"}
    response = {"status": "captured", "receipt": receipt}
    state = {"response": response, "returncode": 0, "stderr": "", "calls": []}

    def run(command, **kwargs):
        state["calls"].append(command)
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=str(tmp_path / "main" / ".git"), stderr="")
        raw = state["response"]
        return subprocess.CompletedProcess(command, state["returncode"], stdout=raw if isinstance(raw, str) else json.dumps(raw), stderr=state["stderr"])

    monkeypatch.setattr(bridge.kanban, "run_command", run)
    monkeypatch.setattr(bridge.kanban, "task_runtime_port", lambda **kwargs: 12345)

    def write_transcript(metadata, extra_message=False):
        records = [
            {"type": "session_meta", "payload": {"id": payload["session_id"], **metadata}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "boundary-turn", "turn_attribution": {"turn_trigger": "user"}}},
            {"timestamp": "2026-10-21T16:02:02.905Z", "type": "event_msg", "payload": {"type": "item_completed", "turn_id": "boundary-turn", "item": {"type": "UserMessage", "id": "boundary-message", "content": [{"type": "text", "text": "合成独立消息"}]}}},
        ]
        if extra_message:
            records.append({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "boundary-turn", "item": {"type": "UserMessage", "id": "steering-message", "content": [{"type": "text", "text": "同 turn 第二条消息"}]}}})
        transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    write_transcript({"originator": "codex-tui", "source": "cli", "cli_version": "0.163.0"})
    return payload, receipt, state, write_transcript


@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_verified_tui_capture_and_reader_agree(capture_fixture, source):
    payload, receipt, state, write_transcript = capture_fixture
    write_transcript({"originator": "codex-tui", "source": source, "cli_version": "0.163.0"})
    assert read_independent_user_turn_boundary_evidence(receipt=receipt)["independent_user_turn_verified"] is True
    assert bridge.capture_before_start(payload, transcript_path=receipt["transcript_path"], task_id="task") == receipt
    assert len(state["calls"]) == 2


@pytest.mark.parametrize("version", ["0.164.0", "future-native-cli", None])
def test_native_protocol_evidence_does_not_depend_on_recorded_cli_version(capture_fixture, version):
    payload, receipt, state, write_transcript = capture_fixture
    metadata = {"originator": "codex-tui", "source": "vscode"}
    if version is not None:
        metadata["cli_version"] = version
    write_transcript(metadata)
    assert read_independent_user_turn_boundary_evidence(receipt=receipt)["independent_user_turn_verified"] is True
    assert bridge.capture_before_start(payload, transcript_path=receipt["transcript_path"], task_id="task") == receipt


@pytest.mark.parametrize("metadata", [
    {"originator": "Codex Desktop", "source": "vscode", "cli_version": "0.163.0"},
    {"originator": "codex_exec", "source": "exec", "cli_version": "0.163.0"},
    {"originator": "codex_cli_rs", "source": "cli", "cli_version": "0.163.0"},
    {"originator": "other-harness", "source": "cli", "cli_version": "0.163.0"},
    {"originator": "codex-tui", "source": {}, "cli_version": "0.163.0"},
])
def test_unverified_host_never_attempts_capture(capture_fixture, metadata):
    payload, receipt, state, write_transcript = capture_fixture
    write_transcript(metadata)
    assert read_independent_user_turn_boundary_evidence(receipt=receipt)["capability_unverified"] is True
    with pytest.raises(ReviewStartBoundaryNotVerified) as error:
        bridge.capture_before_start(payload, transcript_path=receipt["transcript_path"], task_id="task")
    assert error.value.reason_code == "native_rewind_capability_unverified"
    assert error.value.recovery_action == "repair_host_capability"
    assert state["calls"] == []


def test_same_turn_messages_keep_invalidation_and_defer_new_boundary(capture_fixture):
    _, receipt, _, write_transcript = capture_fixture
    write_transcript({"originator": "codex-tui", "source": "vscode", "cli_version": "0.163.0"}, extra_message=True)
    evidence = read_independent_user_turn_boundary_evidence(receipt=receipt)
    assert evidence["independent_user_turn_verified"] is False
    assert evidence["reason_code"] == "native_user_turn_not_independent"
    assert evidence["recovery_action"] == "request_new_boundary_after_turn_completion"


@pytest.mark.parametrize("changed_field,changed_value", [
    ("decision_token", "fedcba9876543210"),
    ("session_id", "another-session"),
    ("worktree", None),
])
def test_captured_identity_mismatch_stays_invalidated(capture_fixture, changed_field, changed_value):
    payload, receipt, state, _ = capture_fixture
    state["response"] = {"status": "captured", "receipt": {**receipt, changed_field: changed_value}}
    with pytest.raises(ReviewStartBoundaryNotVerified) as error:
        bridge.capture_before_start(payload, transcript_path=receipt["transcript_path"], task_id="task")
    assert error.value.status == "review_start_boundary_invalidated"
    assert error.value.reason_code == "captured_boundary_identity_mismatch"


@pytest.mark.parametrize("response,returncode,reason_code,recovery_action", [
    ("", 0, "host_cli_response_invalid", "repair_host_capability"),
    ({"receipt": {}}, 0, "host_cli_response_invalid", "repair_host_capability"),
    ({"status": "pending", "reason": "等待原生回合完成", "reason_code": "native_turn_not_completed", "recovery_action": "wait_for_native_turn_completion"}, 12, "native_turn_not_completed", "wait_for_native_turn_completion"),
    ({"status": "invalidated", "reason": "同 turn 消息混入", "reason_code": "native_user_turn_not_independent", "recovery_action": "request_new_boundary_after_turn_completion"}, 12, "native_user_turn_not_independent", "request_new_boundary_after_turn_completion"),
])
def test_native_capture_failure_retains_typed_reason_without_claiming_capture(capture_fixture, response, returncode, reason_code, recovery_action):
    payload, receipt, state, _ = capture_fixture
    state.update(response=response, returncode=returncode)
    with pytest.raises(ReviewStartBoundaryNotVerified) as error:
        bridge.capture_before_start(payload, transcript_path=receipt["transcript_path"], task_id="task")
    assert error.value.reason_code == reason_code
    assert error.value.recovery_action == recovery_action


def test_delivery_client_binds_strict_independent_turn_to_expected_session(capture_fixture):
    _, _, state, _ = capture_fixture
    state["response"] = {"task_id": "task", "message_id": "message", "terminal_delivery_status": "accepted_pending_submit_confirmation"}
    bridge.kanban.send_task_message(task_cmd="kanban task", repo=Path("."), task_id="task", prompt="合成消息", prompt_file=None, source="review-validate-fix", idempotency_key="key", attempt_id=None, deliver_when_agent_idle=True, max_wait_for_agent_idle_ms=10000, required_native_independent_user_turn_session_id="synthetic-session")
    command = state["calls"][0]
    assert command[command.index("--require-native-independent-user-turn-for-session") + 1] == "synthetic-session"


def test_initial_stop_delivery_requires_target_native_session(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import stop_review_validate_fix as stop
    from core.run_ledger.run_ledger import RunLedger

    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-local-data"))
    ledger = RunLedger(component="synthetic-boundary-delivery", run_dir=tmp_path / "isolated-delivery-run")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"message_id": "synthetic-message"}), stderr="")
    monkeypatch.setattr(stop.subprocess, "run", run)
    stop.start_cline_kanban_followup_message(
        project_path=str(tmp_path), task_id="synthetic-task", attempt_id=None,
        prompt="合成首投分界", ledger=ledger, deliver_when_agent_idle=True,
        decision_token_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
        required_native_independent_user_turn_session_id="synthetic-native-session",
    )
    command = calls[0]
    assert command[command.index("--require-native-independent-user-turn-for-session") + 1] == "synthetic-native-session"


@pytest.mark.parametrize("session_id", [None, ""])
def test_initial_stop_without_native_session_never_launches_delivery(tmp_path, monkeypatch, session_id):
    import stop_review_validate_fix as stop
    from core.run_ledger.run_ledger import RunLedger

    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-local-data"))
    ledger = RunLedger(component="synthetic-boundary-refusal", run_dir=tmp_path / "isolated-delivery-run")
    def forbidden(*args, **kwargs):
        pytest.fail("缺少绑定不得启动宿主投递")
    monkeypatch.setattr(stop.subprocess, "run", forbidden)
    with pytest.raises(stop.KanbanFollowupDeliveryNotAttemptedError):
        stop.start_cline_kanban_followup_message(
            project_path=str(tmp_path), task_id="synthetic-task", attempt_id=None,
            prompt="合成缺失身份", ledger=ledger, deliver_when_agent_idle=True,
            decision_token_expires_at="2099-01-01T00:00:00Z",
            required_native_independent_user_turn_session_id=session_id,
        )
