#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from pathlib import Path

from _rvf_test_support.loader import load_script_module as _load


def _confirm():
    return _load("rvf_bootstrap_confirm")


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


