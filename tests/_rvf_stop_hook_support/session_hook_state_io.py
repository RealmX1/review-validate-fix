#!/usr/bin/env python3
"""session-hook 状态 I/O 测试簇。

从 tests/test_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 由 aggregator（tests/test_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_session_hook_default_state_dir_is_user_local_runtime_state',
    'test_session_hook_state_dir_respects_state_dir_override',
    'test_session_hook_control_disables_current_session',
    'test_session_hook_control_status_reports_current_session',
    'test_session_hook_control_status_works_when_env_suppressed',
    'test_session_hook_control_reenables_current_session',
    'test_session_hook_control_reenable_starts_cline_kanban_task',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_session_hook_default_state_dir_is_user_local_runtime_state(tmp_path: Path) -> None:
    old_state = os.environ.pop("CODEX_RVF_STATE_DIR", None)
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    old_user_local_data_root = os.environ.get("RVF_USER_LOCAL_DATA_ROOT")
    user_local_data_root = tmp_path / "rvf-user-local-data"
    os.environ["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)
    try:
        module = load_hook_module()
        expected = user_local_data_root / "runtime-state/session-hook"
        assert module.session_hook_state_dir() == expected
    finally:
        if old_state is not None:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state
        if old_user_local_data_root is None:
            os.environ.pop("RVF_USER_LOCAL_DATA_ROOT", None)
        else:
            os.environ["RVF_USER_LOCAL_DATA_ROOT"] = old_user_local_data_root


def test_session_hook_state_dir_respects_state_dir_override(tmp_path: Path) -> None:
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state-root")
    try:
        module = load_hook_module()
        assert module.session_hook_state_dir() == tmp_path / "state-root" / "session-hook"
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_session_hook_control_disables_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-disabled",
        "先不要自动 review。\nRVF_STOP_HOOK: off",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_disabled"
    assert summary["control_action"] == "off"
    assert summary["session_hook_gate_state"] == "disabled"
    assert "disabled" in str(summary["message"])
    assert "不是关闭全局 Stop hook" in str(summary["message"])
    assert "dispatcher 仍会运行" in str(summary["message"])
    assert (state / "session-hook" / "session-disabled.json").exists()
    assert latest_pointer(state)["status"] == "session-hook-control"

    write_user_session(
        transcript,
        "session-disabled",
        "这次普通停止也不应触发 hook。",
    )
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )
    assert_skip_reason(stdout, "已禁用")
    assert latest_pointer(state)["status"] == "skipped"


def test_session_hook_control_status_reports_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-status",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )

    write_user_session(
        transcript,
        "session-status",
        "RVF_STOP_HOOK: status",
    )
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            state_dir=state,
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_status"
    assert summary["control_action"] == "status"
    assert summary["session_hook_gate_state"] == "disabled"
    assert "disabled" in str(summary["message"])
    assert "不表示全局 Stop hook 是否安装或运行" in str(summary["message"])
    assert "session-status" in str(summary["message"])


def test_session_hook_control_status_works_when_env_suppressed(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-status-suppressed",
        "RVF_STOP_HOOK: status",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={"RVF_SUPPRESS_STOP_HOOK": "1"},
            state_dir=state,
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_status"
    assert summary["session_hook_gate_state"] == "enabled"
    assert "enabled" in str(summary["message"])
    assert "session-status-suppressed" in str(summary["message"])


def test_session_hook_control_reenables_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-reenabled",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    assert (state / "session-hook" / "session-reenabled.json").exists()

    write_apply_patch_transcript(transcript, dirty, session_id="session-reenabled")
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_user_message": "RVF_STOP_HOOK: on",
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    # S4：re-enable 后 Stop 正常前进到 dirty repo 的决策点注入（不再自动 dry_run fork）。session-hook
    # 控制守卫（session_hook_control_continue 事件）仍生效、session 标记仍被清除。
    assert payload.get("decision") == "block", payload
    assert "RVF_DECISION_TOKEN=" in str(payload["reason"])
    assert "changed.txt" in str(payload["reason"]), payload["reason"]
    assert not (state / "session-hook" / "session-reenabled.json").exists()
    assert latest_pointer(state)["status"] == "decision-point-injected"
    events = latest_events(state)
    assert any(
        event["event"] == "session_hook_control_continue"
        and event["reason_code"] == "session_hook_gate_enabled"
        for event in events
    )


def test_session_hook_control_reenable_starts_cline_kanban_task(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-kanban-reenabled",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    assert (state / "session-hook" / "session-kanban-reenabled.json").exists()

    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-reenabled', 'workspace_path': '/tmp/task-worktree'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-reenabled', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    write_apply_patch_transcript(transcript, repo, session_id="session-kanban-reenabled")

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_user_message": "RVF_STOP_HOOK: on",
            },
            extra_env={
                "CODEX_RVF_FORK_MODE": "cline-kanban",
                "RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )[0]
    )

    # S4 迁移（原断言 cline_kanban_task_started）：re-enable 后 fresh-initiation（backend=kanban，非
    # kanban-task 内）→ **不再自动创建 Cline Kanban task**，改注入决策点；task 创建后置到 rvf-agent
    # dispatch。session 标记仍被清除。fake client 不应被调到 create/start。
    assert payload.get("decision") == "block", payload
    assert "RVF_DECISION_TOKEN=" in str(payload["reason"])
    assert "已注入 RVF 发起决策点" in str(payload.get("systemMessage", "")), payload
    assert not (state / "session-hook" / "session-kanban-reenabled.json").exists()
    latest = latest_summary(state)
    assert latest["status"] == "decision-point-injected"
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if client_calls.exists() else []
    assert not any(call["argv"][0] in ("create", "start") for call in calls), calls
    # session_hook_control_continue 守卫仍生效（re-enable 后继续走常规决策点路径）。
    events = latest_events(state)
    assert any(
        event["event"] == "session_hook_control_continue"
        and event["reason_code"] == "session_hook_gate_enabled"
        for event in events
    )
