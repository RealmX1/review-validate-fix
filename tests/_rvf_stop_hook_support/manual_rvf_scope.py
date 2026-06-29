#!/usr/bin/env python3
"""manual RVF scope 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_manual_rvf_session_marker_write_read_clear_preserves_hook_state',
    'test_manual_rvf_session_marker_skips_before_fork_gate',
    'test_manual_rvf_session_marker_dirty_change_does_not_suppress',
    'test_manual_rvf_session_marker_expired_does_not_read',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_manual_rvf_session_marker_write_read_clear_preserves_hook_state(tmp_path: Path) -> None:
    module = load_hook_module()
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state-root")
    try:
        module.set_session_hook_enabled(
            session_id="manual/session",
            enabled=False,
            latest_user="RVF_STOP_HOOK: off",
        )
        path = module.write_manual_rvf_session_marker(
            session_id="manual/session",
            run_id="rvf-manual-run",
            completed_at="2999-04-30T00:00:00+00:00",
        )
        assert path == tmp_path / "state-root" / "session-hook" / "manual_session.json"

        marker = module.read_manual_rvf_session_marker("manual/session")
        assert marker is not None
        assert marker["manual_rvf_run_id"] == "rvf-manual-run"
        assert marker["manual_rvf_completed_at"] == "2999-04-30T00:00:00+00:00"
        assert module.session_hook_disabled("manual/session") is True

        cleared = module.clear_manual_rvf_session_marker("manual/session")
        assert cleared == path
        assert module.read_manual_rvf_session_marker("manual/session") is None
        assert module.session_hook_disabled("manual/session") is True
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_manual_rvf_session_marker_skips_before_fork_gate(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    state = tmp_path / "state"
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(state)
    try:
        module.set_session_hook_enabled(
            session_id="manual-rvf-session",
            enabled=False,
            latest_user="RVF_STOP_HOOK: off",
        )
        module.write_manual_rvf_session_marker(
            session_id="manual-rvf-session",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2999-04-30T00:00:00+00:00",
        )
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "manual-rvf-session",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )

    assert "decision" not in payload
    assert "reason=manual_rvf_already_ran" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "manual_rvf_already_ran"
    assert summary["manual_rvf_run_id"] == "rvf-manual-run"
    assert summary["manual_rvf_completed_at"] == "2999-04-30T00:00:00+00:00"
    assert summary["manual_rvf_repo"] == str(dirty.resolve())
    assert summary["manual_rvf_dirty_hash"]
    assert "app_server_requests_path" not in summary
    assert latest_pointer(state)["reason_code"] == "manual_rvf_already_ran"


def test_manual_rvf_session_marker_dirty_change_does_not_suppress(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    state = tmp_path / "state"
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(state)
    try:
        module.write_manual_rvf_session_marker(
            session_id="manual-rvf-session-dirty-changed",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2999-04-30T00:00:00+00:00",
        )
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state

    (dirty / "changed.txt").write_text("new dirty content\n", encoding="utf-8")
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "manual-rvf-session-dirty-changed",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )

    assert "reason=manual_rvf_already_ran" not in payload["systemMessage"]
    assert "reason=dry_run" in payload["systemMessage"]


def test_manual_rvf_session_marker_expired_does_not_read(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state")
    try:
        module.write_manual_rvf_session_marker(
            session_id="manual-expired",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2000-01-01T00:00:00+00:00",
            ttl_seconds=1,
        )
        assert module.read_manual_rvf_session_marker("manual-expired", dirty) is None
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state

