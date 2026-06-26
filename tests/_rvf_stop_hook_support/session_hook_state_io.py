#!/usr/bin/env python3
"""session-hook 状态 I/O 测试簇。

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
    'test_session_hook_default_state_dir_is_skill_state_session_hook',
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


def test_session_hook_default_state_dir_is_skill_state_session_hook(tmp_path: Path) -> None:
    old_state = os.environ.pop("CODEX_RVF_STATE_DIR", None)
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    try:
        module = load_hook_module()
        expected = SCRIPT.parents[1] / "state" / "session-hook"
        assert module.session_hook_state_dir() == expected
    finally:
        if old_state is not None:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


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
            extra_env={"CODEX_RVF_SUPPRESS_STOP_HOOK": "1"},
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
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "dry_run"
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    assert not (state / "session-hook" / "session-reenabled.json").exists()
    assert latest_pointer(state)["status"] == "dry-run"
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
                "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )[0]
    )

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    assert not (state / "session-hook" / "session-kanban-reenabled.json").exists()
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert latest["cline_kanban_task_id"] == "task-reenabled"
    prep = dispatch_prep_payload(latest)
    prep_tracker_scope = prep["rvf_run"]["tracker_scope_path"]
    assert isinstance(prep_tracker_scope, str)
    assert prep_tracker_scope.endswith("artifacts/tracker-scope.json")
    assert prep["rvf_run"]["tracker_lease_id"]
    assert prep["rvf_run"]["tracker_scope_hash"]
    tracker_scope_payload = json.loads(Path(prep_tracker_scope).read_text(encoding="utf-8"))
    assert tracker_scope_payload["lease_ttl_seconds"] > 0
    startup_command = json.loads(
        (Path(latest["artifacts_dir"]) / "cline-kanban-dispatch-prepare-command.json").read_text(
            encoding="utf-8"
        )
    )
    command = startup_command["command"]
    assert command[command.index("--tracker-scope") + 1] == prep_tracker_scope
    startup_metadata = read_json_artifact(latest, "startup_prepare_metadata_path")
    assert isinstance(startup_metadata, dict)
    assert startup_metadata["input_tracker_scope_file"].endswith("artifacts/inputs/tracker-scope.json")
    assert startup_metadata["tracker_scope_file"].endswith("artifacts/tracker-scope.json")
    scope_contract = json.loads(Path(startup_metadata["scope_contract"]).read_text(encoding="utf-8"))
    assert scope_contract["primary_files"] == ["changed.txt"]
    assert scope_contract["fix_allowlist"] == ["changed.txt"]
    assert scope_contract["primary_units"]
    assert scope_contract["tracker_lease_id"]
    assert scope_contract["tracker_scope_hash"]
    assert startup_metadata["worktree_bootstrap_metadata"]["owned_dirty_paths"] == ["changed.txt"]
    packet_metadata = json.loads(
        Path(startup_metadata["review_packet_metadata"]).read_text(encoding="utf-8")
    )
    assert packet_metadata["tracker_scope_present"] is True
    assert packet_metadata["tracker_scope_unit_count"] == len(scope_contract["primary_units"])
    events = latest_events(state)
    assert any(
        event["event"] == "session_hook_control_continue"
        and event["reason_code"] == "session_hook_gate_enabled"
        for event in events
    )
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["ensure", "create", "start"]

