"""结构性谢绝的指纹冷却必须限定在作出判断的会话内。"""

from __future__ import annotations

import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))


@pytest.fixture
def isolated_cooldown_state(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local-data"))
    return importlib.import_module("rvf_decision_token_store")


def mint_candidate(tokens, session_id, **overrides):
    fields = dict(worktree="/isolated-worktree", session_id=session_id, candidate_fingerprint="same-candidate")
    fields.update(overrides)
    return tokens.mint_decision_token(**fields)


def decline_candidate(tokens, session_id):
    minted = mint_candidate(tokens, session_id)
    tokens.mark_declined(
        minted.token, decline_kind="structural_not_needed", reason="这些改动不是本会话产生的"
    )
    return minted.token


def test_fork_decline_does_not_suppress_original_implementation_session(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    decline_candidate(tokens, "fork-research-session")
    assert mint_candidate(tokens, "fork-research-session").outcome == "declined_cooldown"
    assert mint_candidate(tokens, "original-implementation-session").outcome == "minted"


def test_foreign_decline_is_neither_cleared_nor_reported_as_manual_override(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    decline_candidate(tokens, "fork-research-session")
    manual = mint_candidate(
        tokens, "original-implementation-session", initiated_by_explicit_user_request=True,
        explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit",
    )
    assert manual.outcome == "minted"
    assert not manual.explicit_user_request_overrode_agent_declined_cooldown
    assert mint_candidate(tokens, "fork-research-session").outcome == "declined_cooldown"


def test_independent_sessions_keep_separate_cooldown_records(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    with ThreadPoolExecutor(max_workers=2) as executor:
        declined_tokens = list(executor.map(lambda session: decline_candidate(tokens, session), ["parent", "fork"]))
    for session, decision_token in zip(["parent", "fork"], declined_tokens):
        record = tokens.read_declined_fingerprint(
            worktree="/isolated-worktree", session_id=session, candidate_fingerprint="same-candidate"
        )
        assert record["session_id"] == session
        assert record["token"] == decision_token
    assert tokens.clear_declined_fingerprint(
        worktree="/isolated-worktree", session_id="parent", candidate_fingerprint="same-candidate"
    )
    assert mint_candidate(tokens, "parent").outcome == "minted"
    assert mint_candidate(tokens, "fork").outcome == "declined_cooldown"


@pytest.mark.parametrize("session_id", ["", "   "])
def test_missing_session_identity_keeps_audit_without_shared_cooldown(isolated_cooldown_state, session_id):
    tokens = isolated_cooldown_state
    decision_token = decline_candidate(tokens, session_id)
    assert tokens.read_decision_token(decision_token).state == "declined"
    assert not list(tokens.declined_fingerprint_root().rglob("*.json"))
    fields = dict(worktree="/isolated-worktree", session_id=session_id, candidate_fingerprint="same-candidate")
    assert tokens.record_declined_fingerprint(**fields) is None
    assert tokens.read_declined_fingerprint(**fields) is None
    assert not tokens.is_fingerprint_in_declined_cooldown(**fields)
    assert not tokens.clear_declined_fingerprint(**fields)
    assert mint_candidate(tokens, session_id).outcome == "minted"
    assert mint_candidate(tokens, "identified-session").outcome == "minted"


def test_old_global_cooldown_records_are_ignored_without_rewriting_history(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    root = tokens.declined_fingerprint_root()
    root.mkdir(parents=True)
    old_record = root / (tokens._digest("/isolated-worktree", "same-candidate") + ".json")
    old_content = json.dumps({"worktree": "/isolated-worktree", "candidate_fingerprint": "same-candidate", "reason": "历史谢绝"})
    old_record.write_text(old_content, encoding="utf-8")
    assert mint_candidate(tokens, "new-session").outcome == "minted"
    assert old_record.read_text(encoding="utf-8") == old_content


def test_mismatched_cooldown_payload_cannot_impersonate_current_session(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    decline_candidate(tokens, "parent")
    record_path = next(tokens.declined_fingerprint_root().rglob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["session_id"] = "foreign-session"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert mint_candidate(tokens, "parent").outcome == "minted"


def test_manual_override_clear_failure_only_blocks_the_declining_session(isolated_cooldown_state, monkeypatch):
    tokens = isolated_cooldown_state
    decline_candidate(tokens, "parent")
    root = tokens.declined_fingerprint_root().resolve()
    original_unlink = Path.unlink

    def refuse_cooldown_removal(path, *args, **kwargs):
        if path.suffix == ".json" and root in path.resolve().parents:
            raise PermissionError("合成冷却清除失败")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", refuse_cooldown_removal)
        manual = mint_candidate(
            tokens, "parent", initiated_by_explicit_user_request=True,
            explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit",
        )
    assert manual.explicit_user_request_overrode_agent_declined_cooldown
    assert manual.declined_cooldown_record_still_blocks_automatic_path
    assert mint_candidate(tokens, "fork").outcome == "minted"
    tokens.mark_declined(manual.token, decline_kind="deferred_until_condition", reason="等后续验证")
    assert mint_candidate(tokens, "parent").outcome == "declined_cooldown"


@pytest.mark.parametrize("session_id", ["parent", "fork"])
def test_manual_user_prompt_reports_only_current_session_override(isolated_cooldown_state, monkeypatch, tmp_path, session_id):
    tokens = isolated_cooldown_state
    decline_candidate(tokens, "parent")
    summary = {
        "repo": "/isolated-worktree", "session_id": session_id, "candidate_path_count": 1,
        "candidate_fingerprint": "same-candidate", "candidates": [],
    }
    summary_module = importlib.import_module("stop_decision_candidate_summary")
    monkeypatch.setattr(summary_module, "build_candidate_summary", lambda **fields: dict(summary))
    submit = importlib.import_module("rvf_user_prompt_submit")
    payload = submit.inspect_user_prompt_submit(
        {"cwd": str(tmp_path), "session_id": session_id, "hook_event_name": "UserPromptSubmit", "prompt": "/review-validate-fix"},
        prep_root=tmp_path / "isolated-dispatch-preparation",
    )
    assert payload["status"] == "manual_decision_injected", payload
    assert ("已推翻" in payload["systemMessage"]) == (session_id == "parent")
    assert tokens.is_fingerprint_in_declined_cooldown(
        worktree="/isolated-worktree", session_id="parent", candidate_fingerprint="same-candidate"
    ) == (session_id == "fork")


@pytest.mark.parametrize("transport", ["self-injection", "out-of-band"])
def test_real_cli_and_stop_hook_keep_parent_and_fork_decisions_separate(tmp_path, transport):
    # 复用已有隔离子进程驱动；带外发送指向本地替身，不接触真实 Kanban 会话。
    import test_stop_review_validate_fix as stop_suite
    from _rvf_stop_hook_support import decision_point_out_of_band_delivery as delivery_suite

    repo = stop_suite.init_repo(tmp_path / "isolated-repository", dirty=True)
    state = tmp_path / "isolated-hook-state"
    parent_event = {"cwd": str(repo), "session_id": "parent-session", "stop_hook_active": False}
    stdout, _ = stop_suite.invoke(parent_event, state_dir=state)
    _, parent_token = stop_suite.assert_decision_point_injection(stdout)
    declined = stop_suite._run_rvf_agent(
        state, repo, "decline", "--token", parent_token, "--decline-kind", "structural_not_needed",
        "--reason", "这些改动不是本会话产生的",
    )
    assert declined.returncode == 0, declined.stdout + declined.stderr
    assert "本会话" in declined.stdout
    stop_suite.invoke(parent_event, state_dir=state)
    assert stop_suite.latest_summary(state)["reason_code"] == "decision_point_declined_cooldown"
    cooldown_event = next(event for event in stop_suite.latest_events(state) if event["event"] == "decision_point_declined_cooldown")
    assert cooldown_event["declined_by_session_id"] == "parent-session"
    assert cooldown_event["declined_decision_token"] == parent_token
    assert cooldown_event["declined_at"]
    assert cooldown_event["decline_reason"] == "这些改动不是本会话产生的"
    emitted = next(event for event in stop_suite.latest_events(state) if event["event"] == "stop_hook_payload_emitted")
    assert emitted["stop_hook_output_audience"] == "developer_only"

    extra_env = {}
    if transport == "out-of-band":
        stub = delivery_suite._write_stub_client(tmp_path / "isolated-delivery-stub.py")
        extra_env = delivery_suite._out_of_band_env(stub_client=stub, task_id="isolated-task")
    stdout, _ = stop_suite.invoke(
        {**parent_event, "session_id": "fork-session", "parent_thread_id": "parent-session"},
        state_dir=state, extra_env=extra_env,
    )
    if transport == "self-injection":
        stop_suite.assert_decision_point_injection(stdout)
    else:
        assert stop_suite.latest_summary(state)["reason_code"] == "decision_point_delivered_out_of_band"
    records = stop_suite.decision_token_records(state)
    assert {record["session_id"] for record in records} == {"parent-session", "fork-session"}


def test_cli_without_session_reports_missing_cooldown_but_preserves_decline_reason(isolated_cooldown_state):
    tokens = isolated_cooldown_state
    cli = importlib.import_module("rvf_agent_decision_cli")
    decision_token = mint_candidate(tokens, "").token
    result = cli.run_decline(token=decision_token, reason="缺少会话身份仍记录谢绝", decline_kind="structural_not_needed")
    assert result.status == "ok"
    assert result.detail["session_scoped_declined_candidate_fingerprint_recorded"] is False
    assert "未建立本会话指纹冷却" in result.message
    ledger = Path(result.detail["declined_reason_ledger"])
    assert "缺少会话身份仍记录谢绝" in ledger.read_text(encoding="utf-8")
