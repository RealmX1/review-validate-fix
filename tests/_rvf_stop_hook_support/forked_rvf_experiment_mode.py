#!/usr/bin/env python3
"""forked RVF 实验模式 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_forked_rvf_session_gets_programmatic_handoff_advisory',
    'test_forked_rvf_session_waits_for_handoff_before_advisory',
    'test_forked_rvf_session_waits_when_handoff_message_missing',
    'test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message',
    'test_forked_rvf_marker_scan_skips_incomplete_earlier_marker',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_forked_rvf_session_gets_programmatic_handoff_advisory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    handoff = tmp_path / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    notifier_log = tmp_path / "notify.log"
    notifier = write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    event = {
        "cwd": str(tmp_path),
        "session_id": "child-session",
        "stop_hook_active": False,
        "last_user_message": fork_prompt,
        "last_assistant_message": f"完成。\nRVF_HANDOFF_FILE: {handoff}",
    }
    payload = parse_json(
        invoke(
            event,
            state_dir=state,
            extra_env={"RVF_TERMINAL_NOTIFIER_BIN": str(notifier)},
        )[0]
    )
    assert "decision" not in payload
    assert "reason=handoff_file_ready" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["handoff_path"] == str(handoff.resolve())
    assert summary["rvf_state_phase"] == "complete"
    assert summary["rvf_completion_gate"] == "handoff_file_ready"
    assert summary["rvf_handoff_path"] == str(handoff.resolve())
    assert summary["handoff_notify_result"]["notified"] is True
    calls = [
        json.loads(line)
        for line in notifier_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    assert "-title" in calls[0] and "RVF" in calls[0]
    # 非 kanban 来源 → 信息-only，不带 -open。
    assert "-open" not in calls[0]
    assert summary["handoff_task_url"] is None

    stdout, _ = invoke(
        event,
        state_dir=state,
        extra_env={"RVF_TERMINAL_NOTIFIER_BIN": str(notifier)},
    )
    payload = parse_json(stdout)
    summary = summary_from_payload(payload)
    assert summary["already_notified"] is True
    assert summary["handoff_notify_result"]["reason"] == "already_notified"
    # 去重：第二次 Stop 不应再调用 notifier。
    calls = [
        json.loads(line)
        for line in notifier_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1


def test_forked_rvf_session_waits_for_handoff_before_advisory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
            "last_assistant_message": "我还需要继续检查，尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-notified").exists()


def test_forked_rvf_session_waits_when_handoff_message_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-notified").exists()


def test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "repo", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {dirty}\n"
    )
    write_user_session_messages(
        transcript,
        "child-session",
        [
            fork_prompt,
            "后续用户消息遮住了最初的 fork marker。",
        ],
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "child-session",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "last_assistant_message": "尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert latest_pointer(state)["status"] == "skipped"


def test_forked_rvf_marker_scan_skips_incomplete_earlier_marker(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "repo", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {dirty}\n"
    )
    write_user_session_messages(
        transcript,
        "child-session",
        [
            "早先普通讨论里提到了 RVF_FORKED_REVIEW_VALIDATE_FIX，但没有完整 metadata。",
            fork_prompt,
            "后续用户消息遮住了最初的 fork marker。",
        ],
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "child-session",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "last_assistant_message": "尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert latest_pointer(state)["status"] == "skipped"

