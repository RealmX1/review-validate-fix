#!/usr/bin/env python3
"""dirty repo 闸 测试簇。

从 tests/test_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import re
from pathlib import Path

# 由 aggregator（tests/test_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_dirty_repo_dry_run_injects_decision_point',
    'test_dirty_repo_manual_mode_injects_decision_point',
    'test_dirty_repo_fork_dry_run_injects_decision_point',
    'test_dirty_repo_fork_inherits_parent_cwd_inside_worktree',
    'test_dirty_repo_continuation_mode_reports_removed_fallback',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_dirty_repo_dry_run_injects_decision_point(tmp_path: Path) -> None:
    """S4 迁移（原 ..._prepares_legacy_gui_requests）：dirty repo 的初始发起不再自动 fork（legacy-gui
    app-server thread/fork + turn/start 那套 prep 已随 auto-fork 一起退场），改为注入决策点给主 agent。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "00000000-0000-0000-0000-000000000002",
            "stop_hook_active": False,
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )
    _payload, _token = assert_decision_point_injection(stdout, expect_paths=("changed.txt",))
    assert str(dirty) in str(_payload["reason"])


def test_dirty_repo_manual_mode_injects_decision_point(tmp_path: Path) -> None:
    """S4 迁移（原 ..._only_prepares_prompt）：manual backend 也是 fresh-initiation → 注入决策点。
    manual 的「只准备 prompt 不 fork」重量级准备职能已后置到 rvf-agent CLI 的 start/dispatch。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "00000000-0000-0000-0000-000000000022",
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_MODE": "fork",
            "CODEX_RVF_FORK_MODE": "manual",
        },
        state_dir=state,
    )
    _payload, _token = assert_decision_point_injection(stdout, expect_paths=("changed.txt",))


def test_dirty_repo_fork_dry_run_injects_decision_point(tmp_path: Path) -> None:
    """S4 迁移（原 test_dirty_repo_fork_dry_run）：dirty repo 的初始 fork 链（→
    run_review_validate_fix_child_fork + dispatch-prep）已退场，改注入决策点；注入 reason 携带
    决策 token、CLI 命令样例与目标 repo。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "00000000-0000-0000-0000-000000000003",
            "model": "gpt-test",
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_MODE": "fork",
            "CODEX_RVF_FORK_MODE": "dry-run",
            "CODEX_RVF_FORK_REASONING_EFFORT": "high",
        },
        state_dir=state,
    )
    _payload, token = assert_decision_point_injection(stdout, expect_paths=("changed.txt",))
    reason = str(_payload["reason"])
    # 注入携带可复制的 rvf-agent CLI 命令样例（start / dispatch / decline）。
    assert f"start --token {token}" in reason, reason
    assert f"dispatch --token {token}" in reason, reason
    assert f"decline --token {token}" in reason, reason
    assert str(dirty) in reason


def test_dirty_repo_fork_inherits_parent_cwd_inside_worktree(tmp_path: Path) -> None:
    """S4 迁移：cwd 在 worktree 子目录时，候选摘要仍以仓库根为目标 worktree；注入决策点而非 fork。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    subdir = dirty / "nested"
    subdir.mkdir()
    state = tmp_path / "state"

    stdout, _ = invoke(
        {
            "cwd": str(subdir),
            "session_id": "00000000-0000-0000-0000-000000000103",
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_MODE": "fork",
            "CODEX_RVF_FORK_MODE": "dry-run",
        },
        state_dir=state,
    )

    _payload, _token = assert_decision_point_injection(stdout, expect_paths=("changed.txt",))
    # 目标 worktree 解析到仓库根（而非 cwd 子目录）。
    assert f"目标 worktree: {dirty.resolve()}" in str(_payload["reason"]), _payload["reason"]


def test_dirty_repo_continuation_mode_reports_removed_fallback(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000004",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_MODE": "continuation"},
        )[0]
    )
    assert "decision" not in payload
    assert payload["continue"] is True
    assert "reason=continuation_disabled" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert "$review-validate-fix" in str(summary["message"])
    assert str(dirty) in str(summary["message"])
    # 去-codex：report-only（刻意不 fork）的 host-中性消息不得泄漏任何 Codex 专属 fork 机制。
    assert "Codex" not in str(summary["message"])
    assert "Stop continuation prompt 已禁用" in str(summary["message"])

