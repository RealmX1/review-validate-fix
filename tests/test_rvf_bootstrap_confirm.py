#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from _rvf_test_support.loader import load_script_module as _load


def _confirm():
    return _load("rvf_bootstrap_confirm")


def _user_prompt_submit():
    _load("rvf_prep_file")
    return _load("rvf_user_prompt_submit")


def _bootstrap_payload(*, kind: str, unattributed: list[str], unattributed_bytes: int, total: int = None):
    if total is None:
        total = unattributed_bytes
    return {
        "bootstrap_kind": kind,
        "unattributed_dirty_paths": list(unattributed),
        "unattributed_path_count": len(unattributed),
        "unattributed_bytes": unattributed_bytes,
        "owned_dirty_paths": list(unattributed),
        "total_bootstrap_bytes": total,
    }


def test_decision_below_threshold_returns_no_confirmation(tmp_path: Path) -> None:
    mod = _confirm()
    decision = mod.compute_decision(
        _bootstrap_payload(kind="full-dirty", unattributed=["a.py", "b.py"], unattributed_bytes=100),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    assert decision.needs_confirmation is False
    assert decision.reason == "below_threshold"


def test_decision_triggers_on_path_count(tmp_path: Path) -> None:
    mod = _confirm()
    paths = [f"p{i}.py" for i in range(15)]
    decision = mod.compute_decision(
        _bootstrap_payload(kind="full-dirty", unattributed=paths, unattributed_bytes=10),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    assert decision.needs_confirmation is True
    assert "paths" in decision.reason
    assert decision.unattributed_path_count == 15


def test_decision_triggers_on_byte_size(tmp_path: Path) -> None:
    mod = _confirm()
    decision = mod.compute_decision(
        _bootstrap_payload(kind="full-dirty", unattributed=["big.bin"], unattributed_bytes=2 * 1024 * 1024),
        thresholds=mod.Thresholds(paths=100, bytes=1024 * 1024),
    )
    assert decision.needs_confirmation is True
    assert "bytes" in decision.reason


def test_decision_exempts_kanban_followup_even_when_oversize(tmp_path: Path) -> None:
    mod = _confirm()
    paths = [f"p{i}.py" for i in range(20)]
    decision = mod.compute_decision(
        _bootstrap_payload(kind="full-dirty", unattributed=paths, unattributed_bytes=10),
        thresholds=mod.Thresholds(paths=5, bytes=1),
        exempt_kanban_followup=True,
    )
    assert decision.needs_confirmation is False
    assert decision.exempt is True
    assert decision.exempt_reason


def test_decision_skips_when_no_unattributed(tmp_path: Path) -> None:
    mod = _confirm()
    decision = mod.compute_decision(
        _bootstrap_payload(kind="session-owned-only", unattributed=[], unattributed_bytes=0),
        thresholds=mod.Thresholds(paths=10, bytes=1),
    )
    assert decision.needs_confirmation is False
    assert decision.reason == "no_unattributed_dirty"


def test_marker_round_trip_and_yes_literal(tmp_path: Path) -> None:
    mod = _confirm()
    state_root = tmp_path / "state"
    decision = mod.compute_decision(
        _bootstrap_payload(
            kind="full-dirty",
            unattributed=[f"p{i}.py" for i in range(12)],
            unattributed_bytes=10,
        ),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    marker = mod.write_marker(
        state_root,
        session_id="abc-123",
        token="0123456789abcdef",
        decision=decision,
        dispatch_context={"cwd": "/repo", "parent_session_id": "abc-123"},
        ttl_seconds=600,
    )
    assert marker.exists()
    payload = mod.read_marker(state_root, "abc-123")
    assert payload is not None
    assert payload["token"] == "0123456789abcdef"
    assert payload["dispatch_context"]["cwd"] == "/repo"
    assert mod.marker_is_expired(payload) is False

    assert mod.is_yes_literal("yes") is True
    assert mod.is_yes_literal("Yes") is True
    assert mod.is_yes_literal("YES") is True
    assert mod.is_yes_literal("yes please") is False
    assert mod.is_yes_literal("y") is False
    assert mod.is_yes_literal("好") is False

    assert mod.delete_marker(state_root, "abc-123") is True
    assert mod.read_marker(state_root, "abc-123") is None


def test_sweep_expired_drops_old_markers(tmp_path: Path) -> None:
    mod = _confirm()
    state_root = tmp_path / "state"
    decision = mod.compute_decision(
        _bootstrap_payload(
            kind="full-dirty",
            unattributed=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py", "h.py", "i.py", "j.py", "k.py"],
            unattributed_bytes=10,
        ),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    mod.write_marker(
        state_root,
        session_id="fresh",
        token="0000000000000001",
        decision=decision,
        dispatch_context={"cwd": "/repo", "parent_session_id": "fresh"},
        ttl_seconds=600,
    )
    mod.write_marker(
        state_root,
        session_id="stale",
        token="0000000000000002",
        decision=decision,
        dispatch_context={"cwd": "/repo", "parent_session_id": "stale"},
        ttl_seconds=600,
    )
    stale_path = mod.marker_path(state_root, "stale")
    stale_payload = json.loads(stale_path.read_text(encoding="utf-8"))
    stale_payload["expires_at"] = time.time() - 1
    stale_path.write_text(json.dumps(stale_payload) + "\n", encoding="utf-8")

    removed = mod.sweep_expired(state_root)
    assert any("stale" in r for r in removed)
    assert mod.marker_path(state_root, "fresh").exists()
    assert not mod.marker_path(state_root, "stale").exists()


def test_user_prompt_submit_resumes_on_yes(tmp_path: Path, monkeypatch) -> None:
    mod = _confirm()
    ups = _user_prompt_submit()
    state_root = tmp_path / "state"

    decision = mod.compute_decision(
        _bootstrap_payload(
            kind="full-dirty",
            unattributed=[f"p{i}.py" for i in range(12)],
            unattributed_bytes=10,
        ),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    mod.write_marker(
        state_root,
        session_id="sess-yes",
        token="0a0b0c0d0e0f1011",
        decision=decision,
        dispatch_context={
            "cwd": "/repo",
            "parent_session_id": "sess-yes",
            "task_title": "demo",
            "base_ref": "abc",
            "worktree_mode": "branch",
            "prompt_path": "/tmp/prompt.txt",
            "dispatch_prep_file_path": "/tmp/prep.json",
            "run_id": "rvf-test",
            "run_dir": "/tmp/run",
        },
        ttl_seconds=600,
    )

    called: dict = {}

    class FakeStopHook:
        @staticmethod
        def resume_dispatch_from_confirmation_marker(payload):
            called["payload"] = payload
            return {"status": "cline-kanban-started", "cline_kanban_task_id": "T1"}

    monkeypatch.setitem(sys.modules, "codex_stop_review_validate_fix", FakeStopHook)

    result = ups.inspect_user_prompt_submit(
        {"prompt": "yes", "session_id": "sess-yes"},
        bootstrap_confirm_state_root=state_root,
    )
    assert result["status"] == "bootstrap_confirm_resumed"
    assert called["payload"]["token"] == "0a0b0c0d0e0f1011"
    assert "已收到 yes 确认" in result["systemMessage"]
    assert not mod.marker_path(state_root, "sess-yes").exists()


def test_user_prompt_submit_cancels_on_other(tmp_path: Path, monkeypatch) -> None:
    mod = _confirm()
    ups = _user_prompt_submit()
    state_root = tmp_path / "state"
    decision = mod.compute_decision(
        _bootstrap_payload(
            kind="full-dirty",
            unattributed=[f"p{i}.py" for i in range(12)],
            unattributed_bytes=10,
        ),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    mod.write_marker(
        state_root,
        session_id="sess-no",
        token="0a0b0c0d0e0f1012",
        decision=decision,
        dispatch_context={"cwd": "/repo", "parent_session_id": "sess-no"},
        ttl_seconds=600,
    )

    class FakeStopHook:
        @staticmethod
        def resume_dispatch_from_confirmation_marker(payload):
            raise AssertionError("must not be called on cancel")

    monkeypatch.setitem(sys.modules, "codex_stop_review_validate_fix", FakeStopHook)

    result = ups.inspect_user_prompt_submit(
        {"prompt": "actually let's not", "session_id": "sess-no"},
        bootstrap_confirm_state_root=state_root,
    )
    assert result["status"] == "bootstrap_confirm_cancelled"
    assert "取消" in result["systemMessage"]
    assert not mod.marker_path(state_root, "sess-no").exists()


def test_user_prompt_submit_expired_marker_is_gced(tmp_path: Path) -> None:
    mod = _confirm()
    ups = _user_prompt_submit()
    state_root = tmp_path / "state"
    decision = mod.compute_decision(
        _bootstrap_payload(
            kind="full-dirty",
            unattributed=[f"p{i}.py" for i in range(12)],
            unattributed_bytes=10,
        ),
        thresholds=mod.Thresholds(paths=10, bytes=1024 * 1024),
    )
    mod.write_marker(
        state_root,
        session_id="sess-exp",
        token="0a0b0c0d0e0f1013",
        decision=decision,
        dispatch_context={"cwd": "/repo", "parent_session_id": "sess-exp"},
        ttl_seconds=1,
    )
    marker_path = mod.marker_path(state_root, "sess-exp")
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["expires_at"] = time.time() - 10
    marker_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = ups.inspect_user_prompt_submit(
        {"prompt": "yes", "session_id": "sess-exp"},
        bootstrap_confirm_state_root=state_root,
    )
    # Sweep happens at the top of _handle_bootstrap_confirmation, so when we get
    # here the marker was swept and the function returns None (no marker handling).
    # The prompt then falls through to normal handling (no_token/no manual trigger).
    # Either way: marker is gone.
    assert not marker_path.exists()


def test_user_prompt_submit_no_marker_falls_through(tmp_path: Path) -> None:
    ups = _user_prompt_submit()
    state_root = tmp_path / "state"
    result = ups.inspect_user_prompt_submit(
        {"prompt": "hello", "session_id": "sess-none"},
        bootstrap_confirm_state_root=state_root,
    )
    # No marker means we fall through to the regular dispatcher;
    # without a token or manual trigger the regular path returns "no_token".
    assert result["status"] == "no_token"
