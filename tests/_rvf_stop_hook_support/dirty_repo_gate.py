#!/usr/bin/env python3
"""dirty repo 闸 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import re
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_dirty_repo_dry_run_prepares_legacy_gui_requests',
    'test_dirty_repo_manual_mode_only_prepares_prompt',
    'test_dirty_repo_fork_dry_run',
    'test_dirty_repo_fork_inherits_parent_cwd_inside_worktree',
    'test_dirty_repo_continuation_mode_reports_removed_fallback',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_dirty_repo_dry_run_prepares_legacy_gui_requests(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000002",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "dry-run"
    assert latest["mode"] == "dry-run"
    requests = app_server_requests(latest)
    prompt = prompt_text(latest)
    assert requests[0]["method"] == "thread/fork"
    assert requests[1]["method"] == "turn/start"
    assert "$review-validate-fix" in prompt
    assert str(dirty) in prompt


def test_dirty_repo_manual_mode_only_prepares_prompt(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
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
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: manual-prepared; reason=manual_prepared;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "manual-prepared"
    assert latest["rvf_backend"] == "manual"
    assert latest["rvf_state_phase"] == "prepare"
    assert Path(latest["prompt_path"]).exists()


def test_dirty_repo_fork_dry_run(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
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
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    prompt = prompt_text(latest)
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert "$review-validate-fix" in prompt
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt
    assert f"RVF_DISPATCH=token={prep_token}" in prompt
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt
    assert prep["origin_session_id"] == "00000000-0000-0000-0000-000000000003"
    assert prep["origin_repo"] == str(dirty.resolve())
    assert prep["target_flow"] == "flow-3-inplace"
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert str(dirty) in prompt
    assert "RVF_STOP_HOOK: off" in prompt
    assert "会话控制元数据" in prompt
    assert "不要把它们当成用户分配的代码任务" in prompt
    assert latest["suppress_child_stop_hook"] is False
    assert latest["model"] == "gpt-test"
    assert latest["reasoning_effort"] == "high"
    requests = app_server_requests(latest)
    assert requests[0]["method"] == "thread/fork"
    assert requests[0]["params"]["model"] == "gpt-test"
    assert requests[1]["method"] == "turn/start"
    assert requests[1]["params"]["model"] == "gpt-test"
    assert requests[1]["params"]["effort"] == "high"


def test_dirty_repo_fork_inherits_parent_cwd_inside_worktree(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    subdir = dirty / "nested"
    subdir.mkdir()
    state = tmp_path / "state"

    payload = parse_json(
        invoke(
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
        )[0]
    )

    assert "decision" not in payload
    latest = latest_summary(state)
    requests = app_server_requests(latest)
    prompt = prompt_text(latest)
    assert latest["cwd"] == str(subdir.resolve())
    assert requests[0]["params"]["cwd"] == str(subdir.resolve())
    assert requests[1]["params"]["cwd"] == str(subdir.resolve())
    assert f"RVF_PARENT_CWD: {subdir.resolve()}" in prompt
    assert f"RVF_TARGET_REPO: {dirty.resolve()}" in prompt


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
    assert "Stop continuation prompt 已禁用" in str(summary["message"])

