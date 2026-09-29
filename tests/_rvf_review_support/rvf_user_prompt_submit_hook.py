#!/usr/bin/env python3
"""rvf_user_prompt_submit hook 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/_committed_round_*/路径常量）仍归 aggregator 所有，经 inject()
在注册表运行前推入本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 ->
注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from _rvf_test_support.kanban_followup_lock_marker_name import in_progress_lock_marker_name

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_rvf_user_prompt_submit_dispatches_shared_workflow',
    'test_rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist',
    'test_rvf_user_prompt_submit_declines_reviving_stale_prep',
    'test_rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing',
    'test_rvf_user_prompt_submit_marker_without_token',
    'test_rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery',
    'test_rvf_user_prompt_submit_clears_pending_on_delivery',
    'test_rvf_user_prompt_submit_structured_manual_detection_catches_namespaced',
    'test_rvf_user_prompt_submit_manual_mints_decision_token_and_injects_via_additional_context',
    'test_rvf_user_prompt_submit_manual_dedup_retries_minted_delivery_receipt',
    'test_rvf_user_prompt_submit_hook_stdout_confirms_manual_delivery',
    'test_rvf_user_prompt_submit_manual_scope_directive_surfaces_hint_in_injection',
    'test_rvf_user_prompt_submit_manual_no_candidates_emits_systemmessage_no_ledger',
    'test_rvf_user_prompt_submit_manual_declined_cooldown_is_overridden_by_user',
    'test_rvf_user_prompt_submit_manual_declined_cooldown_override_survives_clear_failure',
    'test_rvf_user_prompt_submit_manual_is_exempt_from_mint_circuit_breaker',
    'test_rvf_user_prompt_submit_manual_retired_packet_prep_functions_removed',
    'test_rvf_user_prompt_submit_bootstrap_confirm_gate_abolished',
    'test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking',
    'test_rvf_user_prompt_submit_backfills_child_session',
    'test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode',
    'test_rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage',
    'test_rvf_user_prompt_submit_render_hook_payload_merges_channels',
    'test_rvf_user_prompt_submit_captures_round_baseline',
    'test_rvf_user_prompt_submit_observes_delivered_decision_point_banner',
    'test_rvf_user_prompt_submit_decision_point_banner_never_arms_in_flight_lock',
    'test_rvf_user_prompt_submit_decision_point_banner_misses_every_other_detector',
    'test_rvf_user_prompt_submit_withdrawn_decision_point_banner_advances_nothing',
    'test_rvf_user_prompt_submit_expired_banner_never_claims_token_was_advanced',
    'test_rvf_user_prompt_submit_resubmitted_decision_point_marker_never_reinjects_body',
    'test_rvf_user_prompt_submit_decision_point_marker_without_body_text_advances_nothing',
    'test_rvf_user_prompt_submit_concurrent_decision_point_marker_arrivals_inject_body_exactly_once',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_rvf_user_prompt_submit_dispatches_shared_workflow(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")
        record = prep.write_prep_file(
            {
                "origin_session_id": "session-a",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-test", "run_dir": str(tmp_path / "run")},
            },
            root=root,
            token="aaaaaaaaaaaaaaaa",
            now=now,
            ttl_seconds=300,
        )

        prepare_calls: list[dict[str, object]] = []

        def fake_prepare(record_arg, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            assert record_arg.token == record.token
            prepare_calls.append(
                {
                    "token": record_arg.token,
                    "timeout_seconds": timeout_seconds,
                    "excerpt": user_prompt_excerpt,
                }
            )
            state = {
                "started_at": "2026-05-07T00:01:00Z",
                "completed_at": "2026-05-07T00:01:01Z",
                "status": "completed",
                "target_flow": record_arg.payload.get("target_flow"),
                "artifacts": {"review_env": "/tmp/review-env.sh"},
            }
            new_rvf_run = dict(record_arg.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record_arg, {"rvf_run": new_rvf_run})
            return state

        # Replace the lazy-imported prepare_run_from_prep_file with a stub.
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            no_token_payload = submit.inspect_user_prompt_submit(
                {"prompt": "ordinary prompt"}, prep_root=root
            )
            assert no_token_payload["status"] == "no_token"
            assert no_token_payload["continue"] is True
            assert prepare_calls == []
            # 普通 prompt：既无 user-facing systemMessage，也无 model-facing additionalContext。
            assert "systemMessage" not in no_token_payload
            assert "hookSpecificOutput" not in no_token_payload

            valid_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert valid_payload["status"] == "valid"
            assert valid_payload["workflow_started"] is True
            assert valid_payload["shared_workflow_state"]["status"] == "completed"
            assert valid_payload["prep_file_path"] == str(root / "aaaaaaaaaaaaaaaa.json")
            assert len(prepare_calls) == 1
            # token 派发成功：给用户可见行（含 run_id），但**不**给 agent additionalContext。
            assert isinstance(valid_payload.get("systemMessage"), str)
            assert "rvf-test" in valid_payload["systemMessage"]
            assert "hookSpecificOutput" not in valid_payload

            # Idempotent: state is now completed, second invocation should not re-run prepare.
            second_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                },
                prep_root=root,
                now="2026-05-07T00:01:30Z",
            )
            assert second_payload["status"] == "valid"
            assert second_payload["workflow_started"] is False
            assert second_payload["shared_workflow_state"]["status"] == "completed"
            assert len(prepare_calls) == 1, "second call should be cached"
            # already_completed 幂等路径同样给用户可见行、无 additionalContext。
            assert isinstance(second_payload.get("systemMessage"), str)
            assert "rvf-test" in second_payload["systemMessage"]
            assert "hookSpecificOutput" not in second_payload

            diagnostics_path = root / "diagnostics" / "aaaaaaaaaaaaaaaa.jsonl"
            diagnostics = read_jsonl(diagnostics_path)
            statuses = [event["status"] for event in diagnostics]
            assert "valid" in statuses
            assert any(
                event.get("event") == "user_prompt_submit_shared_workflow_skipped"
                for event in diagnostics
            )
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist(
    tmp_path: Path,
) -> None:
    """FU-1：dispatch token 在场、prep 已过 TTL，但 run_dir 仍在 → 就地续期、走 valid 派发
    路径（workflow 启动），而非静默丢整轮 followup。"""
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)  # run artifacts 仍在
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        t0 = prep.parse_timestamp("2026-05-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-revive",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-revive", "run_dir": str(run_dir)},
            },
            root=root,
            token="aaaaaaaaaaaaaaaa",
            now=t0,
            ttl_seconds=300,
        )
        prepare_calls: list[str] = []

        def fake_prepare(record_arg, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            prepare_calls.append(record_arg.token)
            state = {
                "started_at": "2026-05-07T00:10:01Z",
                "completed_at": "2026-05-07T00:10:02Z",
                "status": "completed",
                "target_flow": record_arg.payload.get("target_flow"),
                "artifacts": {"review_env": "/tmp/review-env.sh"},
            }
            new_rvf_run = dict(record_arg.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record_arg, {"rvf_run": new_rvf_run})
            return state

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            # now 越过 expiry（00:05:00）：read 判 expired，但 run_dir 在 → 续期 → valid 派发。
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
                now="2026-05-07T00:10:00Z",
            )
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
        assert payload["status"] == "valid", payload  # 续期后走 valid 路径
        assert payload.get("prep_revived") is True
        assert payload["workflow_started"] is True
        assert prepare_calls == ["aaaaaaaaaaaaaaaa"]  # 确实派发，而非静默丢
        # 非 dispatch_no_prep：systemMessage 含 run_id（valid 行），不是「未跑」诊断。
        assert "rvf-revive" in (payload.get("systemMessage") or "")
        diagnostics = read_jsonl(root / "diagnostics" / "aaaaaaaaaaaaaaaa.jsonl")
        assert any(
            d.get("event") == "user_prompt_submit_dispatch_prep_revived" for d in diagnostics
        ), diagnostics
        # prep 文件已就地续期：在 00:12:00 仍 valid（原 300s TTL 早已过）。
        again = prep.read_prep_file(
            "aaaaaaaaaaaaaaaa", root=root, now=prep.parse_timestamp("2026-05-07T00:12:00Z")
        )
        assert again.status == "valid", again
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing(
    tmp_path: Path,
) -> None:
    """FU-1 负例：prep 已过 TTL 且 run_dir 不在（run 已清理 / 真过期）→ 不复活，仍走原
    dispatch_no_prep 早返回（不复活已消失的 run）。"""
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    missing_run_dir = tmp_path / "run-gone"  # 故意不创建
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        t0 = prep.parse_timestamp("2026-05-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-gone",
                "origin_repo": str(tmp_path),
                "rvf_run": {"run_id": "rvf-gone", "run_dir": str(missing_run_dir)},
            },
            root=root,
            token="aaaaaaaaaaaaaaaa",
            now=t0,
            ttl_seconds=300,
        )
        prepare_calls: list[str] = []

        def fake_prepare(record_arg, **_):
            prepare_calls.append(record_arg.token)
            return {"status": "completed"}

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                },
                prep_root=root,
                now="2026-05-07T00:10:00Z",
            )
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
        assert payload["status"] == "expired", payload  # 未复活
        assert "prep_revived" not in payload
        assert payload["workflow_started"] is False
        assert prepare_calls == []  # 未派发
        assert (
            isinstance(payload.get("systemMessage"), str)
            and "aaaaaaaaaaaaaaaa" in payload["systemMessage"]
        )
        diagnostics = read_jsonl(root / "diagnostics" / "aaaaaaaaaaaaaaaa.jsonl")
        assert all(
            d.get("event") != "user_prompt_submit_dispatch_prep_revived" for d in diagnostics
        ), diagnostics
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_marker_without_token(tmp_path: Path) -> None:
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    payload = submit.inspect_user_prompt_submit(
        {
            "prompt": "Stop hook fork prompt body referencing RVF_FORKED_REVIEW_VALIDATE_FIX without token",
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
        },
        prep_root=root,
    )
    assert payload["status"] == "dispatch_marker_without_token"
    assert payload["origin_marker"] == "fork"
    assert payload["continue"] is True
    # 自注入近失：给用户一条可见诊断行，但不给 agent additionalContext。
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"]
    assert "fork" in payload["systemMessage"]
    assert "hookSpecificOutput" not in payload


def test_rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery(tmp_path: Path) -> None:
    """UPS hook 在 kanban-followup trigger 真正投递落地时 arm in-progress 锁；非 followup 不 arm。

    这是把锁的 arm 从 Stop hook（dispatch 时乐观预 arm）移到 UserPromptSubmit 的核心回归：
    只有注入的 follow-up trigger 真的成为一个 prompt（即本 hook fire）才上锁，治本 squat。
    """
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    lock_root = tmp_path / "followup-lock"
    # 锁 key 含仓身份 → origin_repo 必须是**真实** git 仓，否则 marker_paths 空、根本不落锁。
    repo = init_repo(tmp_path / "repo")  # noqa: F821 — inject() 注入
    prev_prep = os.environ.get("CODEX_RVF_PREP_ROOT")
    prev_lock = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(lock_root)
    try:
        now = prep.parse_timestamp("2026-06-04T00:00:00Z")
        # kanban-followup 风格 prep：target_kanban_task_id + flow-1-self-rising + run 信息。
        # 预置 shared_workflow_state=completed，使 inspect 在 arm 之后短路返回（无需 stub prepare）。
        prep.write_prep_file(
            {
                "origin_session_id": "session-fu",
                "origin_repo": str(tmp_path / "repo"),
                "origin_cwd": str(tmp_path / "repo"),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path / "repo"),
                "target_kanban_task_id": "task-fu",
                "rvf_run": {
                    "run_id": "rvf-fu-delivered",
                    "run_dir": str(tmp_path / "run"),
                    "shared_workflow_state": {"status": "completed"},
                },
            },
            root=root,
            token="bbbbbbbbbbbbbbbb",
            now=now,
            ttl_seconds=300,
        )
        # 投递落地的 follow-up prompt：同时带 dispatch token 与 kanban-followup marker。
        followup_prompt = (
            "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER\n"
            "RVF_DISPATCH=token=bbbbbbbbbbbbbbbb\n"
        )
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": followup_prompt,
                "cwd": str(tmp_path / "repo"),
                "session_id": "session-fu",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
            now="2026-06-04T00:01:00Z",
        )
        assert payload["status"] == "valid"
        marker_path = lock_root / in_progress_lock_marker_name(
            load_kanban_followup_lock_module(), repo=repo, task_id="task-fu"
        )
        assert payload.get("kanban_followup_in_progress_marker_path") == str(marker_path)
        assert marker_path.exists()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        # 投递落地只落 awaiting 态：提交 ≠ agent 接手。升格成 in_progress 由 Stop 读侧在确认
        # trigger 仍在 transcript 祖先链上时做（见 stop hook 侧 promote/rewind 对账测试）。
        assert marker["state"] == "delivered_awaiting_agent_decision"
        assert marker["kanban_task_id"] == "task-fu"
        assert marker["run_id"] == "rvf-fu-delivered"
        assert marker["expires_at"] > marker["armed_at"]
        # 对账锚点必须落盘：历史 payload 三个 id 字段全 None，锁自己都不知道对应哪个回合。
        assert marker["dispatch_token"] == "bbbbbbbbbbbbbbbb"
        assert marker["delivered_session_id"] == "session-fu"

        # 负例：带 token 但**非** kanban-followup（无 RVF_KANBAN_FOLLOWUP_TRIGGER，这里是 fork）
        # → 不 arm followup 锁。
        prep.write_prep_file(
            {
                "origin_session_id": "session-fu",
                "origin_repo": str(tmp_path / "repo"),
                "origin_cwd": str(tmp_path / "repo"),
                "target_flow": "flow-2-branch",
                "target_kanban_task_id": "task-other",
                "rvf_run": {
                    "run_id": "rvf-other",
                    "run_dir": str(tmp_path / "run2"),
                    "shared_workflow_state": {"status": "completed"},
                },
            },
            root=root,
            token="cccccccccccccccc",
            now=now,
            ttl_seconds=300,
        )
        other_payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "RVF_FORKED_REVIEW_VALIDATE_FIX\nRVF_DISPATCH=token=cccccccccccccccc",
                "cwd": str(tmp_path / "repo"),
                "session_id": "session-fu",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
            now="2026-06-04T00:01:00Z",
        )
        assert other_payload["status"] == "valid"
        assert "kanban_followup_in_progress_marker_path" not in other_payload
        assert not (lock_root / "task-task-other.json").exists()
    finally:
        if prev_prep is None:
            os.environ.pop("CODEX_RVF_PREP_ROOT", None)
        else:
            os.environ["CODEX_RVF_PREP_ROOT"] = prev_prep
        if prev_lock is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = prev_lock


def test_rvf_user_prompt_submit_clears_pending_on_delivery(tmp_path: Path) -> None:
    """投递落地：UPS arm in-progress 锁的同时，按 token 清掉 Stop 写的 pending(dispatched-unconfirmed)。

    这样投递真正落地后，下一次该 task 的 Stop 不会把那条 pending 误判为静默丢投而重投。
    """
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    k = load_kanban_followup_lock_module()
    root = tmp_path / "prep-root"
    lock_root = tmp_path / "followup-lock"
    # 锁 key 含仓身份 → origin_repo 必须是**真实** git 仓，否则 marker_paths 空、根本不落锁。
    repo = init_repo(tmp_path / "repo")  # noqa: F821 — inject() 注入
    prev_prep = os.environ.get("CODEX_RVF_PREP_ROOT")
    prev_lock = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(lock_root)
    try:
        now = prep.parse_timestamp("2026-06-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-fu",
                "origin_repo": str(tmp_path / "repo"),
                "origin_cwd": str(tmp_path / "repo"),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path / "repo"),
                "target_kanban_task_id": "task-fu",
                "rvf_run": {
                    "run_id": "rvf-fu-delivered",
                    "run_dir": str(tmp_path / "run"),
                    "shared_workflow_state": {"status": "completed"},
                },
            },
            root=root,
            token="dddddddddddddddd",
            now=now,
            ttl_seconds=300,
        )
        # 预置 Stop 在「未确认投递」时写下的 pending（同 token、同 task）。
        pending_path = k.write_pending_marker(
            task_id="task-fu",
            session_id="session-fu",
            run_id="rvf-fu-delivered",
            run_dir=str(tmp_path / "run"),
            repo=str(tmp_path / "repo"),
            cwd=str(tmp_path / "repo"),
            token="dddddddddddddddd",
            delivery_channel="terminal",
            message_id="terminal:task-fu:rvf-fu-delivered",
        )
        assert pending_path is not None and pending_path.exists()
        followup_prompt = (
            "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER\n"
            "RVF_DISPATCH=token=dddddddddddddddd\n"
        )
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": followup_prompt,
                "cwd": str(tmp_path / "repo"),
                "session_id": "session-fu",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
            now="2026-06-07T00:01:00Z",
        )
        assert payload["status"] == "valid"
        # in-progress 锁已 arm（投递落地的权威信号）。
        marker_path = lock_root / in_progress_lock_marker_name(
            load_kanban_followup_lock_module(), repo=repo, task_id="task-fu"
        )
        assert payload.get("kanban_followup_in_progress_marker_path") == str(marker_path)
        assert marker_path.exists()
        # 同 token 的 pending 已被清。
        assert not pending_path.exists()
        assert k.read_pending_marker(task_id="task-fu") is None
    finally:
        if prev_prep is None:
            os.environ.pop("CODEX_RVF_PREP_ROOT", None)
        else:
            os.environ["CODEX_RVF_PREP_ROOT"] = prev_prep
        if prev_lock is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = prev_lock


def test_rvf_user_prompt_submit_structured_manual_detection_catches_namespaced(tmp_path: Path) -> None:
    """RVF 采纳 vendored codex_invoked_skill：结构化检测命中正则漏掉的命名空间形态。

    `$rvf:review-validate-fix` 的 `:review-validate-fix` 前缀非词边界，旧锚定正则 MISS；
    经 rollout text_elements 的结构化读取能命中。回退正则对 Claude / 缺 transcript 仍有效。
    """
    submit = load_rvf_user_prompt_submit_module()
    # 该 vendored 模块必须可被 RVF 加载（否则结构化路径静默退化）。
    assert submit.codex_invoked_skill is not None

    tmp_path.mkdir(parents=True, exist_ok=True)  # registry 传入的子目录可能尚未创建
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "$rvf:review-validate-fix",
                    "text_elements": [
                        {"byte_range": {"start": 0, "end": 24}, "placeholder": "$rvf:review-validate-fix"}
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    namespaced = "$rvf:review-validate-fix"
    # 旧正则确实漏掉命名空间形态：
    assert submit.detect_manual_trigger(namespaced) is False
    # 结构化路径（rollout text_elements）命中它：
    event = {
        "transcript_path": str(rollout),
        "prompt": namespaced,
        "hook_event_name": "UserPromptSubmit",
    }
    assert submit._review_validate_fix_manually_invoked(event, namespaced) is True
    # 回退正则在无 rollout（Claude / 缺 transcript）时仍生效：
    assert submit._review_validate_fix_manually_invoked(
        {"prompt": "$review-validate-fix"}, "$review-validate-fix"
    ) is True
    # 散文里提到不构成触发（锚定 + 无 rollout 命中）：
    assert submit._review_validate_fix_manually_invoked(
        {}, "document the review-validate-fix tool"
    ) is False


# ---------------------------------------------------------------------------
# S5（R22）manual 决策点：铸决策 token + additionalContext 注入的测试簇 helper
# ---------------------------------------------------------------------------

def _load_manual_decision_modules():
    """加载 manual 决策点用到的 S1/S4/S2 模块（决策 token store / 注入尝试 ledger / 候选摘要）。

    仅需 SCRIPT_DIR 在 sys.path 上（候选摘要模块自理 pyroot + core.* import）。
    """
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import rvf_decision_token_store as _dts  # noqa: PLC0415
    import agentic_initiation_injection_attempt_ledger as _il  # noqa: PLC0415
    import stop_decision_candidate_summary as _summary  # noqa: PLC0415

    return _dts, _il, _summary


def _manual_candidate_repo(tmp_path: Path, *, dirty: bool = True) -> Path:
    """初始化隔离 git repo（单文件 base 提交）；``dirty=True`` 时叠加一处未提交改动 → 1 个候选。"""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    if dirty:
        (repo / "f.txt").write_text("base\ndirty-line\n", encoding="utf-8")
    return repo


def _manual_decision_env(tmp_path: Path):
    """上下文：把 RVF 落盘根隔离到 tmp（RVF_HOME 统辖 token/ledger/冷却/熔断；CODEX_RVF_LOG_ROOT
    供候选摘要默认下界 resolver），并清掉环境里可能干扰 task_id 解析的 Kanban env，退出后原样还原。
    绝不写真实 ``~/.rvf``。"""
    import contextlib  # noqa: PLC0415

    @contextlib.contextmanager
    def _ctx():
        overrides = {
            "RVF_HOME": str(tmp_path / "rvf-home"),
            "CODEX_RVF_LOG_ROOT": str(tmp_path / "rvf-log-root"),
        }
        kanban_keys = ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID")
        saved = {k: os.environ.get(k) for k in (*overrides, *kanban_keys)}
        for key, value in overrides.items():
            os.environ[key] = value
        for key in kanban_keys:
            os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _ctx()


def test_rvf_user_prompt_submit_manual_mints_decision_token_and_injects_via_additional_context(
    tmp_path: Path,
) -> None:
    """S5·R22 正路径：manual ``/review-validate-fix`` → 铸决策 token（S1 三闸）→ 经
    ``additionalContext`` 注入「候选摘要 + token + rvf-agent start/scope/dispatch/decline 样例命令 +
    指引」；同时写 injection-attempt ledger（负路径不变量：候选非空 ⇒ 有记录），且**不再**预置 packet。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)  # 1 个 dirty 候选
    prep_root = tmp_path / "prep-root"
    with _manual_decision_env(tmp_path):
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "/review-validate-fix please review my work",
                "cwd": str(repo),
                "session_id": "manual-sess",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=str(prep_root),
            now="2026-07-11T00:00:00Z",
        )
        assert payload["status"] == "manual_decision_injected", payload
        assert payload["dispatch_origin"] == "post_user_prompt_manual"
        assert payload["candidate_path_count"] == 1
        token = payload["token"]
        assert dts.TOKEN_RE.fullmatch(token), token

        # additionalContext（model-facing）：候选摘要 + token + rvf-agent 四类命令样例 + 指引。
        hook_specific = payload["hookSpecificOutput"]
        assert hook_specific["hookEventName"] == "UserPromptSubmit"
        additional_context = hook_specific["additionalContext"]
        assert f"RVF_DECISION_TOKEN={token}" in additional_context
        assert f"start --token {token}" in additional_context
        assert "scope exclude --token" in additional_context
        assert f"dispatch --token {token}" in additional_context
        assert f"decline --token {token}" in additional_context
        assert "简析后立刻 start" in additional_context
        assert "你手动触发了 /review-validate-fix" in additional_context

        # systemMessage（user-facing）用「已铸决策 token，等待 agent 决策」口径（区分旧「审查已开始」）。
        assert isinstance(payload.get("systemMessage"), str)
        assert "已铸决策 token" in payload["systemMessage"]
        assert "post_user_prompt_manual" in payload["systemMessage"]

        # inspect 只构造待 emit payload：stdout 尚未 flush，token/ledger 不得乐观推进。
        lookup = dts.read_decision_token(token, now=dts.parse_timestamp("2026-07-11T00:01:00Z"))
        assert lookup.status == "valid", lookup
        assert lookup.state == dts.STATE_MINTED
        assert submit._RVF_PENDING_MANUAL_INJECTION_RECEIPT in payload
        assert il.read_injection_attempts(worktree=str(repo.resolve())) == []

        # 模拟 main() 已成功 print+flush：此后才推进 injected 并写真实投递回执。
        submit._confirm_manual_decision_delivery(payload, now="2026-07-11T00:00:00Z")
        assert submit._RVF_PENDING_MANUAL_INJECTION_RECEIPT not in payload
        lookup = dts.read_decision_token(token, now=dts.parse_timestamp("2026-07-11T00:01:00Z"))
        assert lookup.state == dts.STATE_INJECTED

        # 负路径不变量：候选非空 ⇒ injection-attempt ledger 有一条 injected 记录。
        records = il.read_injection_attempts(worktree=str(repo.resolve()))
        assert len(records) == 1, records
        assert records[0]["mint_outcome"] == il.INJECTION_OUTCOME_INJECTED
        assert records[0]["injected"] is True
        assert records[0]["candidate_path_count"] == 1
        assert records[0]["decision_token"] == token
        assert records[0]["has_committed_but_unreviewed"] is False

    # 退役 packet：manual 不再预置 prep/packet（prep_root 从未被创建），也无 shared_workflow_state。
    assert not prep_root.exists()
    assert "shared_workflow_state" not in payload
    assert "prep_file_path" not in payload


def test_rvf_user_prompt_submit_manual_dedup_retries_minted_delivery_receipt(
    tmp_path: Path,
) -> None:
    """首轮在 emit 前中断留下 minted；同指纹 manual dedup 重投后，stdout 回执应推进
    原 token，并把本次 additionalContext 如实记为 ``injected=True``。"""
    submit = load_rvf_user_prompt_submit_module()
    dts, il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    event = {
        "prompt": "/review-validate-fix",
        "cwd": str(repo),
        "session_id": "manual-dedup-sess",
        "hook_event_name": "UserPromptSubmit",
    }
    with _manual_decision_env(tmp_path):
        first = submit.inspect_user_prompt_submit(event, now="2026-07-11T00:00:00Z")
        token = first["token"]
        assert dts.read_decision_token(
            token, now=dts.parse_timestamp("2026-07-11T00:00:00Z")
        ).state == dts.STATE_MINTED
        assert il.read_injection_attempts(worktree=str(repo.resolve())) == []

        second = submit.inspect_user_prompt_submit(event, now="2026-07-11T00:00:01Z")
        assert second["status"] == "manual_decision_deduplicated", second
        assert second["token"] == token
        submit._confirm_manual_decision_delivery(second, now="2026-07-11T00:00:01Z")

        assert dts.read_decision_token(
            token, now=dts.parse_timestamp("2026-07-11T00:00:02Z")
        ).state == dts.STATE_INJECTED
        records = il.read_injection_attempts(worktree=str(repo.resolve()))
        assert len(records) == 1, records
        assert records[0]["mint_outcome"] == il.INJECTION_OUTCOME_DEDUPLICATED_PENDING
        assert records[0]["injected"] is True
        assert records[0]["decision_token"] == token


def test_rvf_user_prompt_submit_hook_stdout_confirms_manual_delivery(
    tmp_path: Path,
) -> None:
    """真实 hook-mode 子进程 stdout 成功后，manual token 与 ledger 回执均已兑现。"""
    dts, il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    event = {
        "prompt": "/review-validate-fix",
        "cwd": str(repo),
        "session_id": "manual-hook-stdout-sess",
        "hook_event_name": "UserPromptSubmit",
    }
    with _manual_decision_env(tmp_path):
        completed = subprocess.run(
            [
                sys.executable,
                str(RVF_USER_PROMPT_SUBMIT),
                "--prep-root",
                str(tmp_path / "prep-root"),
                "--now",
                "2026-07-11T00:00:00Z",
            ],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            check=True,
            env=os.environ.copy(),
        )
        output = json.loads(completed.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        token = context.split("RVF_DECISION_TOKEN=", 1)[1].splitlines()[0].strip()
        assert dts.read_decision_token(
            token, now=dts.parse_timestamp("2026-07-11T00:01:00Z")
        ).state == dts.STATE_INJECTED
        records = il.read_injection_attempts(worktree=str(repo.resolve()))
        assert len(records) == 1 and records[0]["injected"] is True, records


def test_rvf_user_prompt_submit_manual_scope_directive_surfaces_hint_in_injection(
    tmp_path: Path,
) -> None:
    """manual 内联 ``scope:`` → 决策点 additionalContext 附「内联 scope 提示」（不再喂 prepare；
    scope 现由 rvf-agent CLI scope 阶段定稿）。``manual_scope_files`` 仍回填供诊断。"""
    submit = load_rvf_user_prompt_submit_module()
    _dts, _il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "/review-validate-fix please review scope: src/a.py, src/b.py",
                "cwd": str(repo),
                "session_id": "manual-scope-sess",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=str(tmp_path / "prep-root"),
            now="2026-07-11T00:00:00Z",
        )
    assert payload["status"] == "manual_decision_injected", payload
    assert payload["manual_scope_files"] == ["src/a.py", "src/b.py"]
    additional_context = payload["hookSpecificOutput"]["additionalContext"]
    assert "内联指定了 scope: src/a.py, src/b.py" in additional_context
    assert "rvf-agent scope include/exclude" in additional_context


def test_rvf_user_prompt_submit_manual_no_candidates_emits_systemmessage_no_ledger(
    tmp_path: Path,
) -> None:
    """manual 触发但 clean worktree（自上次 review 无可审候选）→ 只出 user-facing systemMessage，
    不铸 token、不写 injection ledger（保持 canary 分母干净，与 Stop hook 无候选路径一致）。"""
    submit = load_rvf_user_prompt_submit_module()
    dts, il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path, dirty=False)  # clean：无候选
    with _manual_decision_env(tmp_path):
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "/review-validate-fix",
                "cwd": str(repo),
                "session_id": "clean-sess",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=str(tmp_path / "prep-root"),
            now="2026-07-11T00:00:00Z",
        )
        assert payload["status"] == "manual_decision_no_candidates", payload
        assert payload["candidate_path_count"] == 0
        assert "hookSpecificOutput" not in payload  # 无候选不注入 additionalContext
        assert isinstance(payload.get("systemMessage"), str)
        assert "无可审候选" in payload["systemMessage"]
        # 无 token 落盘、无 injection ledger 记录。
        token_dir = dts.decision_token_root()
        assert not token_dir.exists() or not list(token_dir.glob("*.json"))
        assert il.read_injection_attempts(worktree=str(repo.resolve())) == []


def test_rvf_user_prompt_submit_manual_declined_cooldown_is_overridden_by_user(
    tmp_path: Path,
) -> None:
    """用户手动触发**推翻** agent 早前的谢绝：候选指纹虽处于 declined 冷却（改动未变），manual
    路径仍照常铸 token 并注入 additionalContext；冷却记录被就地清除，且 additionalContext 明确
    告知 agent「你上次的 decline 已被用户推翻」——否则它会照上次判断再谢绝一次。"""
    submit = load_rvf_user_prompt_submit_module()
    dts, il, summary_mod = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        # 先算出该候选集指纹，写入 declined 冷却表（模拟 agent 早前对同一候选 decline）。
        summ = summary_mod.build_candidate_summary(
            event={"cwd": str(repo)}, cwd=str(repo), repo=str(repo), ledger=None
        )
        fingerprint = summ["candidate_fingerprint"]
        dts.record_declined_fingerprint(
            worktree=str(repo.resolve()),
            candidate_fingerprint=fingerprint,
            reason="agent declined earlier",
        )
        assert dts.is_fingerprint_in_declined_cooldown(
            worktree=str(repo.resolve()), candidate_fingerprint=fingerprint
        )
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "/review-validate-fix",
                "cwd": str(repo),
                "session_id": "cooldown-sess",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=str(tmp_path / "prep-root"),
            now="2026-07-11T00:00:00Z",
        )
        assert payload["status"] == "manual_decision_injected", payload
        token = payload["token"]
        assert dts.TOKEN_RE.fullmatch(token), token

        # 冷却记录被清除：后续自动 Stop 路径也不再被这条陈旧 decline 挡住。
        assert not dts.is_fingerprint_in_declined_cooldown(
            worktree=str(repo.resolve()), candidate_fingerprint=fingerprint
        )

        # additionalContext 必须告知 agent 用户推翻了它早前的谢绝。
        additional_context = payload["hookSpecificOutput"]["additionalContext"]
        assert "推翻" in additional_context, additional_context
        assert "不要以" in additional_context
        # user-facing systemMessage 也如实说明推翻 + 冷却记录已被清除。
        assert "已推翻" in payload["systemMessage"], payload["systemMessage"]
        assert "已清除对应的谢绝冷却记录" in payload["systemMessage"], payload["systemMessage"]

        # 投递回执落地后，ledger 记的是 injected（而非 declined_cooldown）。
        submit._confirm_manual_decision_delivery(payload, now="2026-07-11T00:00:01Z")
        records = il.read_injection_attempts(worktree=str(repo.resolve()))
        assert len(records) == 1, records
        assert records[0]["mint_outcome"] == il.INJECTION_OUTCOME_INJECTED
        assert records[0]["injected"] is True


def test_rvf_user_prompt_submit_manual_declined_cooldown_override_survives_clear_failure(
    tmp_path: Path,
) -> None:
    """RVF-001 回归：冷却记录 unlink 因 I/O 失败（被 ``clear_declined_fingerprint`` 吞成 False）时，
    「用户显式推翻了 agent 早前的谢绝」这条**告知**仍必须照发——additionalContext 里的推翻提示、
    systemMessage 里的说明都不许被 I/O 失败静默吞掉；同时 systemMessage 不得谎称「已清除冷却」，
    而要如实说明记录清除失败、后续自动 Stop 仍可能被它挡住。"""
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, summary_mod = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        summ = summary_mod.build_candidate_summary(
            event={"cwd": str(repo)}, cwd=str(repo), repo=str(repo), ledger=None
        )
        fingerprint = summ["candidate_fingerprint"]
        dts.record_declined_fingerprint(
            worktree=str(repo.resolve()),
            candidate_fingerprint=fingerprint,
            reason="agent declined earlier",
        )

        # 只让「冷却表目录下的文件」删除失败，其余 unlink 照常，避免误伤 hook 其它落盘。
        blocked_dir = dts.declined_fingerprint_root().resolve()
        original_unlink = Path.unlink

        def unlink_failing_only_inside_declined_cooldown_dir(self, *args, **kwargs):
            if self.resolve().parent == blocked_dir:
                raise PermissionError(13, "Permission denied", str(self))
            return original_unlink(self, *args, **kwargs)

        Path.unlink = unlink_failing_only_inside_declined_cooldown_dir
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "/review-validate-fix",
                    "cwd": str(repo),
                    "session_id": "cooldown-clear-fail-sess",
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=str(tmp_path / "prep-root"),
                now="2026-07-11T00:00:00Z",
            )
        finally:
            Path.unlink = original_unlink

        assert payload["status"] == "manual_decision_injected", payload
        # 冷却记录确实没删掉（复现了 unlink 失败），自动路径仍被挡。
        assert dts.is_fingerprint_in_declined_cooldown(
            worktree=str(repo.resolve()), candidate_fingerprint=fingerprint
        )
        # 关键回归：告知 agent 的那段不得因清除失败而消失。
        additional_context = payload["hookSpecificOutput"]["additionalContext"]
        assert "推翻" in additional_context, additional_context
        assert "不要以" in additional_context
        # 用户可见文案：如实说明推翻 + 清除失败，绝不谎称已清除。
        system_message = payload["systemMessage"]
        assert "已推翻" in system_message, system_message
        assert "谢绝冷却记录清除失败" in system_message, system_message
        assert "已清除对应的谢绝冷却记录" not in system_message, system_message


def test_rvf_user_prompt_submit_manual_is_exempt_from_mint_circuit_breaker(
    tmp_path: Path,
) -> None:
    """铸造频率熔断（R24）防的是 agent 递归自发起，对用户显式手动触发不适用：同一 session 连打
    远超阈值的次数，每次都要铸得出 token，且一行 mint-request ledger 都不该写（不占自动路径配额）。"""
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    repo = _manual_candidate_repo(tmp_path)
    assert dts.DEFAULT_MINT_CIRCUIT_BREAKER_MAX_REQUESTS < 5
    with _manual_decision_env(tmp_path):
        for index in range(5):
            # 每轮换一次候选内容 ⇒ 指纹变化，绕开闸一去重，逼真地逐次请求铸造。
            (repo / "f.txt").write_text(f"base\nmanual round {index}\n", encoding="utf-8")
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "/review-validate-fix",
                    "cwd": str(repo),
                    "session_id": "breaker-exempt-sess",
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=str(tmp_path / "prep-root"),
                now="2026-07-11T00:00:00Z",
            )
            assert payload["status"] == "manual_decision_injected", (index, payload)
            assert payload.get("token")
        # 手动路径不注册铸造请求 ⇒ mint-request ledger 一行都没有。
        assert (
            dts.count_recent_mint_requests(
                "breaker-exempt-sess",
                now=dts.parse_timestamp("2026-07-11T00:00:00Z"),
                window_seconds=dts.DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS,
            )
            == 0
        )


def test_rvf_user_prompt_submit_manual_retired_packet_prep_functions_removed(
    tmp_path: Path,
) -> None:
    """退役坐实：manual 路径旧的 packet/prepare 预置逻辑（``_create_manual_prep_file`` /
    ``_manual_additional_context_text``）与 bootstrap 独立确认闸（``_handle_bootstrap_confirmation`` /
    ``_bootstrap_confirm_state_root``）已从 UPS 模块删除；``inspect_user_prompt_submit`` 不再接受
    ``bootstrap_confirm_state_root`` 参数。"""
    import inspect as _inspect  # noqa: PLC0415

    submit = load_rvf_user_prompt_submit_module()
    for retired in (
        "_create_manual_prep_file",
        "_manual_additional_context_text",
        "_handle_bootstrap_confirmation",
        "_bootstrap_confirm_state_root",
    ):
        assert not hasattr(submit, retired), f"{retired} 应已退役删除"
    signature = _inspect.signature(submit.inspect_user_prompt_submit)
    assert "bootstrap_confirm_state_root" not in signature.parameters
    # 新决策点入口就位。
    assert hasattr(submit, "_handle_manual_decision_point")
    assert hasattr(submit, "_manual_decision_additional_context_text")


def test_rvf_user_prompt_submit_bootstrap_confirm_gate_abolished(tmp_path: Path) -> None:
    """bootstrap 独立确认闸废除后的行为：裸 ``yes`` prompt 不再被 UPS 拦截去 resume dispatch，
    直接按普通 prompt 处理（无 token、无 manual 触发 → ``no_token``，静默无 systemMessage）。"""
    submit = load_rvf_user_prompt_submit_module()
    with _manual_decision_env(tmp_path):
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "yes",
                "cwd": str(tmp_path),
                "session_id": "sess-yes",
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=str(tmp_path / "prep-root"),
            now="2026-07-11T00:00:00Z",
        )
    assert payload["status"] == "no_token", payload
    assert payload["continue"] is True
    assert "systemMessage" not in payload
    assert "hookSpecificOutput" not in payload


def test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger(tmp_path: Path) -> None:
    """Quoted/embedded references to the trigger literal must not create a manual prep."""

    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"

    # A normal-conversation reference to the trigger literal (preceded by a
    # word character, not whitespace/start-of-line) must not fire. Without the
    # word-boundary regex, plain substring matching would fire here.
    embedded_payloads = [
        "see RVF_DOC[/review-validate-fix] for details",
        "FOO/review-validate-fix BAR",
        "x:review-validate-fix",
        "abc$review-validate-fixyz",
    ]
    for prompt in embedded_payloads:
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": prompt,
                "cwd": str(tmp_path),
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
        )
        assert payload["status"] == "no_token", (prompt, payload)
        assert "hookSpecificOutput" not in payload

    # detect_manual_trigger should also positively recognize the legitimate
    # trigger forms (line-start or whitespace-prefixed).
    assert submit.detect_manual_trigger("/review-validate-fix") is True
    assert submit.detect_manual_trigger("$review-validate-fix") is True
    assert submit.detect_manual_trigger(":review-validate-fix") is True
    assert submit.detect_manual_trigger("please run /review-validate-fix now") is True
    assert submit.detect_manual_trigger("first line\n/review-validate-fix") is True
    # And reject quoted / embedded uses.
    assert submit.detect_manual_trigger("see /review-validate-fixtool docs") is False
    assert submit.detect_manual_trigger("FOO/review-validate-fix BAR") is False
    assert submit.detect_manual_trigger("RVF_DOC[/review-validate-fix]") is False


def test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger(tmp_path: Path) -> None:
    """姊妹命令 / 粘贴的 handoff 正文里的 review-validate-fix 字面量不得启动新 review。

    复现的 bug：`/rvf-land` + 粘贴 handoff 正文里出现 `/review-validate-fix` 字面量，
    旧 `detect_manual_trigger`（对整段 prompt 任意位置匹配）误判为 manual，新建 manual
    prep 并派发。修复后这类应被识别为 handoff 正文 / 姊妹命令参数而抑制，且抑制是
    **位置无关**的；同时保留「输入框残留前缀把合法触发顶离行首」的假阴守卫。
    """
    submit = load_rvf_user_prompt_submit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"

    run_id = "rvf-20260609T124718Z-user-prompt-submit-manual-412bcf4a"
    handoff_body = (
        "# Review-validate-fix 交接上下文\n\n"
        "## 状态\n"
        f"- run id: {run_id}\n\n"
        "## Validate/fix 分组\n"
        "- RVF-G1: 见 /review-validate-fix 工具说明\n\n"
        "## 验证\n"
        "- python3 -m pytest: ok\n"
    )

    # (a) 四个姊妹 skill 前导 + 含字面量的 handoff 正文 → 抑制（manual_trigger_suppressed），
    #     不新建 prep、不注入 additionalContext，但给一条 user-facing systemMessage。
    for sibling in ("/rvf-land", "$rvf-handoff-intake", ":rvf-reopen", "/rvf-analyze"):
        prompt = f"{sibling}\n\n{handoff_body}"
        payload = submit.inspect_user_prompt_submit(
            {"prompt": prompt, "cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit"},
            prep_root=root,
        )
        assert payload["status"] == "manual_trigger_suppressed", (sibling, payload)
        assert "hookSpecificOutput" not in payload, sibling
        assert isinstance(payload.get("systemMessage"), str) and "未启动 review" in payload["systemMessage"]
        assert submit._classify_manual_trigger({}, prompt) == "suppressed", sibling

    # (c) 无前导命令、仅靠 run-id + handoff 章节 + 字面量的纯 handoff 正文 → 抑制。
    assert submit._classify_manual_trigger({}, handoff_body) == "suppressed"

    # (d) 裸触发仍是 manual。
    assert submit._classify_manual_trigger({}, "/review-validate-fix") == "manual"
    # (e) 输入框残留前缀把合法触发顶离行首 → 仍 manual（假阴守卫，位置无关）。
    assert submit._classify_manual_trigger({}, "todo: 买牛奶\n/review-validate-fix") == "manual"
    # (f) 合法 review 请求里顺嘴提了 run id 但无 handoff 章节 → 仍 manual（防过度抑制）。
    runid_only = f"/review-validate-fix 请复核 run {run_id}"
    assert submit._classify_manual_trigger({}, runid_only) == "manual"

    # 纯文本谓词单测（位置无关）。
    assert submit._leading_sibling_command("/rvf-land paste...") is True
    assert submit._leading_sibling_command("   $rvf-reopen") is True
    assert submit._leading_sibling_command("/review-validate-fix") is False
    assert submit._looks_like_handoff_body(handoff_body) is True
    assert submit._looks_like_handoff_body(f"提一句 {run_id} 没别的") is False


def test_rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger(tmp_path: Path) -> None:
    """调用 `rvf-*` 姊妹子 skill 的命名空间形态不得误触发 manual review。

    复现的 bug：在 Claude Code 里调用任意 RVF 子 skill（如
    `/review-validate-fix:rvf-local-deploy`、`/review-validate-fix:rvf-land`）时，UPS hook
    的检测正则只看到前缀 `/review-validate-fix`（`\\b` 在冒号处即成立），把它误判为「用户手动
    触发主 RVF workflow」，于是 bootstrap manual prep 并派发。尤其当子 skill 出现在 prompt
    **句中**时，连历史的开头锚定姊妹抑制（`_leading_sibling_command` 用 `.match`）都够不着。

    修复后检测正则带负向先行断言 `(?!:rvf-)`：主 skill（裸 `/review-validate-fix` 或命名空间
    `/review-validate-fix:review-validate-fix`）仍命中；`…:rvf-<name>` 子 skill 一律不命中、
    走静默 `none`（不抑制、不发 systemMessage、不建 prep），且判定**位置无关**。
    """
    submit = load_rvf_user_prompt_submit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()

    # (a) 报告中的原始复现样本（子 skill 出现在句中）→ 静默 none，不视为 manual。
    example = (
        "Can I now try to do /review-validate-fix:rvf-local-deploy ? "
        "or Are there any additional work to be done?"
    )
    assert submit._classify_manual_trigger({}, example) == "none", example
    assert submit.detect_manual_trigger(example) is False, example

    # (b) 全部 6 个 `rvf-*` 子 skill 的命名空间形态，开头与句中各一 → 均静默 none。
    subskills = (
        "rvf-land",
        "rvf-local-deploy",
        "rvf-analyze",
        "rvf-handoff-intake",
        "rvf-handoff-commit",
        "rvf-reopen",
    )
    for name in subskills:
        leading = f"/review-validate-fix:{name}"
        mid_sentence = f"please now run /review-validate-fix:{name} for this branch"
        for prompt in (leading, mid_sentence):
            assert submit._classify_manual_trigger({}, prompt) == "none", prompt
            assert submit.detect_manual_trigger(prompt) is False, prompt

    # (c) 假阴守卫：主 skill 的裸形态与命名空间形态（后缀 `:review-` 而非 `:rvf-`）仍是 manual。
    assert submit._classify_manual_trigger({}, "/review-validate-fix") == "manual"
    assert (
        submit._classify_manual_trigger({}, "/review-validate-fix:review-validate-fix")
        == "manual"
    )
    # 主 skill 句中出现同样应触发（检测位置无关）。
    assert (
        submit._classify_manual_trigger({}, "hey please /review-validate-fix this branch")
        == "manual"
    )

    # (d) 端到端：对原始复现样本跑 inspect_user_prompt_submit → 不启动 workflow、不建 prep。
    payload = submit.inspect_user_prompt_submit(
        {"prompt": example, "cwd": str(repo), "hook_event_name": "UserPromptSubmit"},
        prep_root=root,
    )
    assert payload["status"] == "no_token", payload
    assert payload.get("workflow_started") is False, payload
    assert "hookSpecificOutput" not in payload, payload
    # 不应留下任何 manual prep 痕迹（子 skill 调用是静默 none，不是 suppressed）。
    assert not root.exists() or not any(root.iterdir()), list(root.iterdir()) if root.exists() else []


def test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-a",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-fail", "run_dir": str(tmp_path / "run")},
            },
            root=root,
            token="cccccccccccccccc",
            now=now,
            ttl_seconds=300,
        )

        def boom(record, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            raise RuntimeError("prepare boom")

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = boom
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "go RVF_DISPATCH=token=cccccccccccccccc",
                    "cwd": str(tmp_path),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert payload["continue"] is True
            assert payload["workflow_started"] is False
            assert payload["shared_workflow_state"]["status"] == "failed"
            assert "prepare boom" in payload["shared_workflow_state"]["error"]
            # The prep file on disk must reflect the failed state.
            stored = json.loads((root / "cccccccccccccccc.json").read_text(encoding="utf-8"))
            assert stored["rvf_run"]["shared_workflow_state"]["status"] == "failed"
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_backfills_child_session(tmp_path: Path) -> None:
    """Cline Kanban dispatch: the task agent's UserPromptSubmit hook must
    self-backfill child_session_id / child_transcript_path into both the prep
    payload and the persistent origin.json, and skip when same-session."""
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")

        run_dir = tmp_path / "run"
        (run_dir / "artifacts").mkdir(parents=True)
        origin_json = run_dir / "artifacts" / "origin.json"
        origin_json.write_text(
            json.dumps(
                {"session_id": "parent-codex", "transcript_path": "/parent/codex.jsonl"}
            ),
            encoding="utf-8",
        )
        child_transcript = tmp_path / "child_claude.jsonl"
        child_transcript.write_text("{}\n", encoding="utf-8")

        prep.write_prep_file(
            {
                "origin_session_id": "parent-codex",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "origin_metadata_path": str(origin_json),
                "target_flow": "flow-2-branch",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-kanban", "run_dir": str(run_dir)},
            },
            root=root,
            token="dddddddddddddddd",
            now=now,
            ttl_seconds=300,
        )

        def fake_prepare(record_arg, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            state = {
                "started_at": "2026-05-07T00:01:00Z",
                "completed_at": "2026-05-07T00:01:01Z",
                "status": "completed",
                "artifacts": {"review_env": "/tmp/review-env.sh"},
            }
            new_rvf_run = dict(record_arg.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record_arg, {"rvf_run": new_rvf_run})
            return state

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "task: RVF_DISPATCH=token=dddddddddddddddd",
                    "cwd": str(tmp_path),
                    "session_id": "child-claude",
                    "transcript_path": str(child_transcript),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert payload["status"] == "valid"
            assert payload["child_session_id"] == "child-claude"
            assert payload["child_transcript_path"] == str(child_transcript.resolve())

            # Persistent channel: origin.json merged child fields, parent intact.
            merged_origin = json.loads(origin_json.read_text(encoding="utf-8"))
            assert merged_origin["session_id"] == "parent-codex"
            assert merged_origin["transcript_path"] == "/parent/codex.jsonl"
            assert merged_origin["child_session_id"] == "child-claude"
            assert merged_origin["child_transcript_path"] == str(child_transcript.resolve())

            # Prep payload also records the child fields.
            stored = json.loads((root / "dddddddddddddddd.json").read_text(encoding="utf-8"))
            assert stored["child_session_id"] == "child-claude"
            assert stored["child_transcript_path"] == str(child_transcript.resolve())

            diagnostics = read_jsonl(root / "diagnostics" / "dddddddddddddddd.jsonl")
            assert any(
                event.get("event") == "user_prompt_submit_child_session_backfill"
                and event.get("status") == "ok"
                for event in diagnostics
            )

            # Same-session guard: child == origin → no backfill.
            same_origin = run_dir / "artifacts" / "origin-same.json"
            same_origin.write_text(
                json.dumps({"session_id": "same-sess"}), encoding="utf-8"
            )
            prep.write_prep_file(
                {
                    "origin_session_id": "same-sess",
                    "origin_repo": str(tmp_path),
                    "origin_cwd": str(tmp_path),
                    "origin_metadata_path": str(same_origin),
                    "target_flow": "flow-manual",
                    "target_worktree": str(tmp_path),
                    "rvf_run": {"run_id": "rvf-same", "run_dir": str(run_dir)},
                },
                root=root,
                token="eeeeeeeeeeeeeeee",
                now=now,
                ttl_seconds=300,
            )
            same_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "again RVF_DISPATCH=token=eeeeeeeeeeeeeeee",
                    "cwd": str(tmp_path),
                    "session_id": "same-sess",
                    "transcript_path": str(child_transcript),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert same_payload["status"] == "valid"
            assert "child_session_id" not in same_payload
            same_origin_after = json.loads(same_origin.read_text(encoding="utf-8"))
            assert "child_session_id" not in same_origin_after

            # Layer 1 (declared, not-yet-flushed): no origin_metadata_path →
            # derive origin.json from rvf_run.run_dir. The child's first
            # UserPromptSubmit names the transcript before the host flushes it;
            # the declared path is recorded (non-null) and flagged
            # not-yet-existent rather than dropped — capture_run re-checks
            # .is_file() at the child's Stop, by when it exists.
            run_dir2 = tmp_path / "run2"
            (run_dir2 / "artifacts").mkdir(parents=True)
            (run_dir2 / "artifacts" / "origin.json").write_text(
                json.dumps({"session_id": "parent2"}), encoding="utf-8"
            )
            prep.write_prep_file(
                {
                    "origin_session_id": "parent2",
                    "origin_repo": str(tmp_path),
                    "origin_cwd": str(tmp_path),
                    "target_flow": "flow-2-inplace",
                    "target_worktree": str(tmp_path),
                    "rvf_run": {"run_id": "rvf-fb", "run_dir": str(run_dir2)},
                },
                root=root,
                token="ffffffffffffffff",
                now=now,
                ttl_seconds=300,
            )
            declared_missing = tmp_path / "does_not_exist.jsonl"
            fb_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "fb RVF_DISPATCH=token=ffffffffffffffff",
                    "cwd": str(tmp_path),
                    "session_id": "child2",
                    "transcript_path": str(declared_missing),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert fb_payload["status"] == "valid"
            assert fb_payload["child_session_id"] == "child2"
            assert fb_payload["child_transcript_path"] == str(declared_missing.resolve())
            fb_origin = json.loads(
                (run_dir2 / "artifacts" / "origin.json").read_text(encoding="utf-8")
            )
            assert fb_origin["session_id"] == "parent2"
            assert fb_origin["child_session_id"] == "child2"
            assert fb_origin["child_transcript_path"] == str(declared_missing.resolve())
            fb_stored = json.loads((root / "ffffffffffffffff.json").read_text(encoding="utf-8"))
            assert fb_stored["child_session_id"] == "child2"
            assert fb_stored["child_transcript_path"] == str(declared_missing.resolve())
            fb_diags = read_jsonl(root / "diagnostics" / "ffffffffffffffff.jsonl")
            assert any(
                ev.get("event") == "user_prompt_submit_child_session_backfill"
                and ev.get("transcript_source") == "declared"
                and ev.get("child_transcript_exists") is False
                for ev in fb_diags
            )

            # Layer 2 (derived from session_id): event carries NO transcript
            # path at all; the child is Claude, so reconstruct
            # <CLAUDE_CONFIG_DIR>/projects/<cwd-slug>/<sid>.jsonl — taken only
            # because that project dir already exists (flush-independent signal).
            claude_home = tmp_path / "claude-home"
            run_dir3 = tmp_path / "run3"
            (run_dir3 / "artifacts").mkdir(parents=True)
            (run_dir3 / "artifacts" / "origin.json").write_text(
                json.dumps({"session_id": "parent3"}), encoding="utf-8"
            )
            prep.write_prep_file(
                {
                    "origin_session_id": "parent3",
                    "origin_repo": str(tmp_path),
                    "origin_cwd": str(tmp_path),
                    "target_flow": "flow-2-branch",
                    "target_worktree": str(tmp_path),
                    "rvf_run": {"run_id": "rvf-derive", "run_dir": str(run_dir3)},
                },
                root=root,
                token="0000000000000000",
                now=now,
                ttl_seconds=300,
            )
            child3_cwd = str(tmp_path / "child-cwd")
            project_dir = claude_home / "projects" / submit._claude_project_slug(child3_cwd)
            project_dir.mkdir(parents=True)
            expected_derived = (project_dir / "child3.jsonl").resolve()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
            try:
                d_payload = submit.inspect_user_prompt_submit(
                    {
                        "prompt": "derive RVF_DISPATCH=token=0000000000000000",
                        "cwd": child3_cwd,
                        "session_id": "child3",
                    },
                    prep_root=root,
                    now="2026-05-07T00:01:00Z",
                )
            finally:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            assert d_payload["status"] == "valid"
            assert d_payload["child_session_id"] == "child3"
            assert d_payload["child_transcript_path"] == str(expected_derived)
            d_origin = json.loads(
                (run_dir3 / "artifacts" / "origin.json").read_text(encoding="utf-8")
            )
            assert d_origin["child_transcript_path"] == str(expected_derived)
            d_diags = read_jsonl(root / "diagnostics" / "0000000000000000.jsonl")
            assert any(
                ev.get("event") == "user_prompt_submit_child_session_backfill"
                and ev.get("transcript_source") == "derived"
                for ev in d_diags
            )
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    now = prep.parse_timestamp("2026-05-07T00:00:00Z")
    prep.write_prep_file(
        {"origin_session_id": "session-a", "origin_repo": str(tmp_path), "target_flow": "flow-1-self-rising"},
        root=root,
        token="aaaaaaaaaaaaaaaa",
        now=now,
        ttl_seconds=300,
    )
    actual_hook = run(
        [
            sys.executable,
            str(RVF_USER_PROMPT_SUBMIT),
            "--prep-root",
            str(root),
            "--now",
            "2026-05-07T00:01:00Z",
        ],
        input_text=json.dumps({"prompt": "ordinary prompt without trigger"}, ensure_ascii=False),
    )
    assert actual_hook.stdout == ""
    assert actual_hook.stderr == ""


def test_rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage(tmp_path: Path) -> None:
    # 端到端真子进程（非 --json）：prompt 带 dispatch token 但 prep 不可读（坏
    # root）。选项 C 下此路径改为对**用户**可见（stdout 含 systemMessage），但
    # **不**注入模型上下文（无 hookSpecificOutput）。证明 main() 的合并 emit 会
    # 把 user-facing systemMessage 打到 stdout，而 token 路径不泄漏 additionalContext。
    tmp_path.mkdir(parents=True, exist_ok=True)
    bad_root = tmp_path / "not-a-directory"
    bad_root.write_text("not a directory\n", encoding="utf-8")

    actual_hook = run(
        [
            sys.executable,
            str(RVF_USER_PROMPT_SUBMIT),
            "--prep-root",
            str(bad_root),
        ],
        input_text=json.dumps({"prompt": "RVF_DISPATCH=token=bbbbbbbbbbbbbbbb"}, ensure_ascii=False),
    )
    assert actual_hook.stderr == ""
    payload = json.loads(actual_hook.stdout)
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"]
    assert "bbbbbbbbbbbbbbbb" in payload["systemMessage"]
    assert "hookSpecificOutput" not in payload
    assert payload.get("continue") is True


def test_rvf_user_prompt_submit_render_hook_payload_merges_channels(tmp_path: Path) -> None:
    # 直接单测 main() 抽出的纯合并函数：systemMessage（user-facing）与
    # hookSpecificOutput（model-facing）共存、各自单独、二者皆无 → 静默(None)。
    # 确定性、不跑真 prepare —— 覆盖旧互斥 elif 会丢 manual additionalContext 的回归。
    submit = load_rvf_user_prompt_submit_module()
    render = submit._render_hook_payload

    hook_block = {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}
    formatted_success = submit._trigger_system_message(
        kind="success",
        dispatch_origin="post_user_prompt_manual",
        run_id="rvf-test",
        status="completed",
        token="aaaaaaaaaaaaaaaa",
    )
    assert "=== RVF USER-PROMPT HOOK ===" in formatted_success
    assert "status=completed" in formatted_success
    assert "origin=post_user_prompt_manual" in formatted_success
    assert "run=rvf-test" in formatted_success
    assert "token=aaaaaaaaaaaaaaaa" in formatted_success
    # 1) 两通道并存（manual 成功路径）：合并、不互相顶掉。
    both = render({"systemMessage": formatted_success, "hookSpecificOutput": hook_block, "continue": True})
    assert both is not None
    assert both["systemMessage"] == formatted_success
    assert "=== RVF USER-PROMPT HOOK ===" in both["systemMessage"]
    assert both["hookSpecificOutput"] == hook_block
    assert both["continue"] is True
    # 2) 仅 systemMessage（token 派发 / marker / invalid）：user-facing 行，无 hook 块。
    sys_only = render({"systemMessage": "RVF UPS：自注入 marker 'fork' 无 token"})
    assert sys_only == {"continue": True, "systemMessage": "RVF UPS：自注入 marker 'fork' 无 token"}
    # 3) 仅 hookSpecificOutput：保留并补 continue。
    hook_only = render({"hookSpecificOutput": hook_block})
    assert hook_only == {"hookSpecificOutput": hook_block, "continue": True}
    # 4) 普通 prompt：两者皆无 → None → 不打印 → 静默。
    assert render({"status": "no_token", "continue": True}) is None
    assert render({"systemMessage": ""}) is None


def test_rvf_user_prompt_submit_captures_round_baseline(tmp: Path) -> None:
    """A genuine user prompt records HEAD as the next round's baseline marker;
    the captured value matches the repo HEAD."""
    submit = load_rvf_user_prompt_submit_module()
    _dt, _sm, rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    # advance HEAD so the captured baseline is the post-advance HEAD.
    (repo / "f.txt").write_text("base\nmore\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "advance"], cwd=repo)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    state_root = tmp / "state"
    # Isolate from any ambient Kanban task id in the runner env so the marker is
    # deterministically session-keyed (task_id takes precedence when present).
    kanban_env_keys = ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID")
    saved_env = {k: os.environ.get(k) for k in (*kanban_env_keys, "CODEX_RVF_LOG_ROOT")}
    for k in kanban_env_keys:
        os.environ.pop(k, None)
    os.environ["CODEX_RVF_LOG_ROOT"] = str(state_root)
    try:
        event = {
            "session_id": "sess-capture",
            "cwd": str(repo),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "please refactor the parser",
        }
        submit.inspect_user_prompt_submit(event, prep_root=tmp / "prep")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    marker = rbm.read_round_baseline_marker(
        task_id=None, session_id="sess-capture", repo=str(repo), root=state_root
    )
    assert marker is not None, "expected a round-baseline marker to be written"
    assert marker["baseline_head"] == head, (marker.get("baseline_head"), head)


def test_rvf_user_prompt_submit_declines_reviving_stale_prep(tmp_path: Path) -> None:
    """过期 prep 的复活有时效上界：会话早已走远的旧派发不得被原地唤醒。

    复现动机：09:08 创建的 prep 在 49 分钟后（用户已中断、已 rewind、已改做别的事）被无条件
    复活并上锁，于是一次从未被 agent 看到的 RVF 被强制续跑。超界即不复活，改记
    ``dispatch_prep_stale_not_revived``、走既有 ``dispatch_no_prep`` 早返回。
    """
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        t0 = prep.parse_timestamp("2026-05-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-stale",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-stale", "run_dir": str(run_dir)},
            },
            root=root,
            token="cccccccccccccccc",
            now=t0,
            ttl_seconds=300,
        )
        # 距创建 49 分钟（> 默认 15min 上界）：run_dir 仍在，但不再复活。
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": "run RVF_DISPATCH=token=cccccccccccccccc",
                "cwd": str(tmp_path),
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
            now="2026-05-07T00:49:00Z",
        )
        assert payload["status"] == "expired", payload
        assert payload.get("prep_revived") is not True
        veto = payload.get("prep_revive_declined")
        assert veto and veto["reason"] == "prep_older_than_revive_max_age", payload
        assert payload["workflow_started"] is False
        diagnostics = read_jsonl(root / "diagnostics" / "cccccccccccccccc.jsonl")
        assert any(
            d.get("event") == "dispatch_prep_stale_not_revived" for d in diagnostics
        ), diagnostics
        # prep 未被续期：之后读仍是 expired（没有被偷偷刷新 TTL）。
        again = prep.read_prep_file(
            "cccccccccccccccc", root=root, now=prep.parse_timestamp("2026-05-07T00:50:00Z")
        )
        assert again.status == "expired", again

        # 对照：同一份 prep 在上界之内（10 分钟）仍照常复活——闸门只切「会话已走远」的那一段。
        payload_in_window = submit.inspect_user_prompt_submit(
            {
                "prompt": "run RVF_DISPATCH=token=cccccccccccccccc",
                "cwd": str(tmp_path),
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
            now="2026-05-07T00:10:00Z",
        )
        assert payload_in_window.get("prep_revived") is True, payload_in_window
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


# --- 带外投递的发起横幅落地：INV-A 的达成信号 --------------------------------------

def _delivered_decision_point_boundary_marker_prompt(stop_hook, token: str) -> str:
    """用**生产渲染函数**造出落地的那条 user message（一行边界标记），绝不在测试里手抄。

    手抄等于给自己发一份永远不会漂移的假正文——「改文案会不会让标记把自己判成手动触发」
    正是这几条用例要守的东西。
    """
    return stop_hook._render_decision_point_out_of_band_boundary_marker(token=token)


def _mint_out_of_band_decision_token(
    dts, stop_hook, repo: Path, session_id: str, *, persist_body_text_for_agent: bool = True
):
    """铸一枚带外 transport 的 token，按生产次序把正文写进 payload，返回 ``(token, marker)``。

    ``persist_body_text_for_agent=False`` 专供「正文缺失」那条出口：生产上 Stop 侧写不进正文会
    当场回落自注入、不投标记，这里直接造出那个本不该出现的组合来钉 UPS 的防御。
    """
    import stop_decision_candidate_summary  # noqa: PLC0415

    summary = stop_decision_candidate_summary.build_candidate_summary(
        event={"cwd": str(repo), "session_id": session_id},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
    )
    mint = dts.mint_decision_token(
        worktree=str(repo.resolve()),
        session_id=session_id,
        candidate_fingerprint=str(summary.get("candidate_fingerprint") or ""),
        candidate_summary=summary,
        delivery_transport=dts.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
    )
    assert mint.outcome == dts.MINT_OUTCOME_MINTED, mint
    if persist_body_text_for_agent:
        dts.record_decision_point_banner_body_text_for_agent(
            mint.token,
            body_text=stop_hook._render_decision_point_injection_prompt(
                summary=summary,
                token=mint.token,
                fork_context=None,
                delivery_transport=dts.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
            ),
        )
    return mint.token, _delivered_decision_point_boundary_marker_prompt(stop_hook, mint.token)


def _load_stop_hook_module_for_ups_tests():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import stop_review_validate_fix as _stop  # noqa: PLC0415

    return _stop


def test_rvf_user_prompt_submit_observes_delivered_decision_point_banner(
    tmp_path: Path,
) -> None:
    """INV-A 的达成信号：横幅作为一条真实 user prompt 到达 ⇒ UPS 观测到 needle ⇒ token 推进
    ``injected``，投递意图被清。

    这条 needle 的价值就在这里——**一条真实 user prompt 必然触发 UPS**，所以 RVF 自己就能观测到
    「横幅确实成了 user message」，比查任何外部 ref 都可靠。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(tmp_path / "locks")
        try:
            token, banner = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-sess"
            )
            import kanban_followup_lock as kfl  # noqa: PLC0415

            intent_path = kfl.write_decision_point_delivery_intent(
                task_id="task-banner",
                session_id="banner-sess",
                worktree=str(repo.resolve()),
                cwd=str(repo),
                decision_token=token,
                candidate_fingerprint="fp",
                kanban_project_path=str(repo),
                idempotency_key=f"rvf-decision-point-{token}",
                state=kfl.DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
            )
            assert intent_path is not None and Path(intent_path).is_file()

            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": banner,
                    "cwd": str(repo),
                    "session_id": "banner-sess",
                    "hook_event_name": "UserPromptSubmit",
                    "kanban_task_id": "task-banner",
                }
            )
            assert payload["status"] == "decision_point_banner_delivered", payload
            assert payload["decision_token"] == token, payload
            assert payload["decision_token_state"] == dts.STATE_INJECTED, payload
            assert dts.read_decision_token(token).state == dts.STATE_INJECTED
            assert not Path(intent_path).exists(), "投递意图应在观测后被清掉"
            assert "真实 user message" in str(payload.get("systemMessage") or "")
            # 标记是 RVF 自己的机器正文，不是用户的工作轮边界 ⇒ 不得前移 round baseline。
            assert "round_baseline" not in payload, payload
            # 决策正文**只**经 hook 的 model-facing 通道注入（粘贴通道只送了那一行标记）。
            body_for_agent = dts.read_decision_token(token).payload[
                dts.DECISION_POINT_BANNER_BODY_TEXT_FOR_AGENT_PAYLOAD_KEY
            ]
            assert payload["hookSpecificOutput"] == {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": body_for_agent,
            }, payload
            assert body_for_agent.splitlines()[0] == stop_hook.DECISION_POINT_BANNER_TITLE_LINE
            assert "开发者已预先授权你独立做这个决定" in body_for_agent
            rendered = submit._render_hook_payload(payload)
            assert rendered["hookSpecificOutput"]["additionalContext"] == body_for_agent, rendered
            assert isinstance(rendered.get("systemMessage"), str), rendered
        finally:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)


def test_rvf_user_prompt_submit_decision_point_banner_never_arms_in_flight_lock(
    tmp_path: Path,
) -> None:
    """横幅落地**绝不** arm 在途锁。

    锁里 ``decision_token`` 的语义是「agent 已在决策点上**显式接受**」，而 awaiting 锁一旦在祖先链上
    发现 needle 就会升格 ``in_progress`` ⇒ 会对一个**从未接受**的 agent 发「继续收尾这一轮」的 nudge。
    ``rvf-agent start`` 仍是 arm 接受凭证的唯一入口。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    lock_root = tmp_path / "locks"
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(lock_root)
        try:
            token, banner = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-noarm-sess"
            )
            submit.inspect_user_prompt_submit(
                {
                    "prompt": banner,
                    "cwd": str(repo),
                    "session_id": "banner-noarm-sess",
                    "hook_event_name": "UserPromptSubmit",
                    "kanban_task_id": "task-banner-noarm",
                }
            )
        finally:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
    armed = sorted(lock_root.glob("*.json")) if lock_root.is_dir() else []
    assert armed == [], armed


def test_rvf_user_prompt_submit_decision_point_banner_misses_every_other_detector(
    tmp_path: Path,
) -> None:
    """横幅正文必须**只**命中决策点检测器，其余全 miss。

    今天正文里两处 ``$review-validate-fix`` 只因被反引号包着才没命中手动触发正则（它要求前面是
    行首或空白）——而 D9 正要改这段文案。没有这条 gate，一次无心的换行就会让横幅把自己判成手动
    触发、再铸一枚 token 再注入一遍。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        token, banner = _mint_out_of_band_decision_token(
            dts, stop_hook, repo, "banner-detector-sess"
        )
    event = {
        "prompt": banner,
        "cwd": str(repo),
        "session_id": "banner-detector-sess",
        "hook_event_name": "UserPromptSubmit",
    }
    assert submit.decision_point_token_from_text(banner) == token
    assert submit.dispatch_token_from_text(banner) is None
    assert submit.detect_origin_marker(banner) is None
    assert submit.detect_manual_trigger(banner) is False
    assert submit._classify_manual_trigger(event, banner) == "none"
    for name, pattern in submit.DISPATCH_ORIGIN_MARKERS:
        assert pattern.search(banner) is None, name
    # 带外正文经 UPS 注入，不进 prompt，但它会进 transcript（hook 附件）——同样不得命中其它检测器。
    with _manual_decision_env(tmp_path):
        body_for_agent = dts.read_decision_token(token).payload[
            dts.DECISION_POINT_BANNER_BODY_TEXT_FOR_AGENT_PAYLOAD_KEY
        ]
    assert submit.decision_point_token_from_text(body_for_agent) == token
    assert submit.dispatch_token_from_text(body_for_agent) is None
    assert submit.detect_manual_trigger(body_for_agent) is False
    # 自注入变体（transport=None）同样不得命中——两条 transport 用的是同一份正文。
    with _manual_decision_env(tmp_path):
        import stop_decision_candidate_summary  # noqa: PLC0415

        summary = stop_decision_candidate_summary.build_candidate_summary(
            event={"cwd": str(repo), "session_id": "banner-detector-sess"},
            cwd=str(repo),
            repo=str(repo),
            ledger=None,
        )
    self_injected = stop_hook._render_decision_point_injection_prompt(
        summary=summary, token=token, fork_context=None, delivery_transport=None
    )
    assert submit.detect_manual_trigger(self_injected) is False
    assert submit.dispatch_token_from_text(self_injected) is None


def test_rvf_user_prompt_submit_withdrawn_decision_point_banner_advances_nothing(
    tmp_path: Path,
) -> None:
    """一份**已撤回**的横幅迟到落地：不推进、不清意图，只如实报一句。

    这正是 D6 要的效果——撤回的权威是 token store 自身，``rvf-agent start`` 读到终态会硬失败。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        token, banner = _mint_out_of_band_decision_token(
            dts, stop_hook, repo, "banner-withdrawn-sess"
        )
        dts.mark_withdrawn(token, reason="test-withdrawal")
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": banner,
                "cwd": str(repo),
                "session_id": "banner-withdrawn-sess",
                "hook_event_name": "UserPromptSubmit",
            }
        )
        assert payload["decision_token_terminal_on_arrival"] is True, payload
        assert dts.read_decision_token(token).state == dts.STATE_WITHDRAWN
        # 撤回后 start 必须硬失败（既有非法迁移路径）。
        try:
            dts.acknowledge_decision_token(token)
        except dts.DecisionTokenStateError:
            pass
        else:  # pragma: no cover - 失败即回归
            raise AssertionError("撤回后的 token 不得还能 acknowledge")


def test_rvf_user_prompt_submit_expired_banner_never_claims_token_was_advanced(
    tmp_path: Path,
) -> None:
    """TTL 已过的横幅落地：``mark_injected`` 抛异常 ⇒ **绝不**输出「推进为 injected」那句断言。

    构造上完全可达：带外横幅异步到达，回执 ``delivered_queued_behind_active_agent_turn`` 排在一个
    长回合之后就能越过 1200s TTL。历史实现只对 ``withdrawn/dispatched/declined`` 做了早返回，其余
    一律往下走——``DecisionTokenExpiredError`` / ``DecisionTokenNotFoundError`` 被吞进
    ``decision_token_error``，出口却仍是那条逐字写着「决策 token 推进为 injected（等待 agent 显式
    start / decline）」的 systemMessage。于是磁盘 ``state`` 还停在 ``minted``、``rvf-agent start``
    实际会硬失败，**开发者却被告知已经推进了**。

    顺带钉住善后：token 没能推进时**不清投递意图**——意图是后续对账（到期撤回 + 重新征询）唯一的
    抓手，清掉它等于把善后线索一起删了。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(tmp_path / "locks")
        try:
            token, banner = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-expired-sess"
            )
            import kanban_followup_lock as kfl  # noqa: PLC0415

            intent_path = kfl.write_decision_point_delivery_intent(
                task_id="task-banner-expired",
                session_id="banner-expired-sess",
                worktree=str(repo.resolve()),
                cwd=str(repo),
                decision_token=token,
                candidate_fingerprint="fp",
                kanban_project_path=str(repo),
                idempotency_key=f"rvf-decision-point-{token}",
                state=kfl.DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
            )
            assert intent_path is not None and Path(intent_path).is_file()

            # 把 TTL 推到过去 = 横幅排在一个长回合之后才落地。
            token_path = dts.decision_token_path(token)
            on_disk = json.loads(Path(token_path).read_text(encoding="utf-8"))
            on_disk["expires_at"] = "2000-01-01T00:00:00+00:00"
            Path(token_path).write_text(
                json.dumps(on_disk, ensure_ascii=False), encoding="utf-8"
            )

            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": banner,
                    "cwd": str(repo),
                    "session_id": "banner-expired-sess",
                    "hook_event_name": "UserPromptSubmit",
                    "kanban_task_id": "task-banner-expired",
                }
            )

            # ① 出口文案不得断言「已推进」。
            system_message = str(payload.get("systemMessage") or "")
            assert "推进为 injected" not in system_message, system_message
            assert "未能推进" in system_message, system_message
            assert "DecisionTokenExpiredError" in system_message, system_message
            # ② 结构化字段如实：没推进就是没推进，且保留具体异常类型。
            assert payload.get("decision_token_advanced_to_injected") is False, payload
            assert payload.get("decision_token_state") is None, payload
            assert str(payload.get("decision_token_error") or "").startswith(
                "DecisionTokenExpiredError"
            ), payload
            assert payload.get("decision_token_status") == "expired", payload
            # ③ 磁盘 state 仍是 minted —— 这正是那句断言性文案与事实相悖之处。
            assert dts.read_decision_token(token).state == dts.STATE_MINTED
            # ④ 投递意图未被误清，留给后续对账。
            assert Path(intent_path).is_file(), "未推进时不得清掉投递意图"
            assert payload.get("cleared_decision_point_delivery_intent_paths") is None, payload
        finally:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)


def test_rvf_user_prompt_submit_resubmitted_decision_point_marker_never_reinjects_body(
    tmp_path: Path,
) -> None:
    """同一枚 token 的标记第二次到达 ⇒ **不再注入正文**，也不改任何状态。

    实测（Claude Code 2.1.284 交互式 TUI）：双 ESC rewind 到标记之前，标记会被放回输入框；用户再
    回车就是第二次到达。rewind 的本意就是去掉这次征询，若照样注入正文就等于 rewind 没生效。
    已 acknowledged（agent 已 start）的 token 同理，也不得落进「本该推进但没推成」的文案。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    event_base = {
        "cwd": str(repo),
        "session_id": "banner-resubmit-sess",
        "hook_event_name": "UserPromptSubmit",
        "kanban_task_id": "task-banner-resubmit",
    }
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(tmp_path / "locks")
        try:
            token, marker = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-resubmit-sess"
            )
            first = submit.inspect_user_prompt_submit({**event_base, "prompt": marker})
            assert "hookSpecificOutput" in first, first
            assert dts.read_decision_token(token).state == dts.STATE_INJECTED

            for label in ("injected", "acknowledged"):
                if label == "acknowledged":
                    dts.acknowledge_decision_token(token)
                state_before = dts.read_decision_token(token).state
                again = submit.inspect_user_prompt_submit(
                    {**event_base, "prompt": f"{marker}\n用户接着打的字"}
                )
                assert "hookSpecificOutput" not in again, (label, again)
                assert again.get("decision_point_banner_resubmitted_without_body_reinjection") is True, (
                    label,
                    again,
                )
                system_message = str(again.get("systemMessage") or "")
                assert "只在首次到达时注入" in system_message, (label, system_message)
                assert "未能推进" not in system_message, (label, system_message)
                assert dts.read_decision_token(token).state == state_before, label
                rendered = submit._render_hook_payload(again)
                assert "hookSpecificOutput" not in rendered, (label, rendered)
        finally:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)


def test_rvf_user_prompt_submit_decision_point_marker_without_body_text_advances_nothing(
    tmp_path: Path,
) -> None:
    """标记首次到达但 token 里没有正文 ⇒ 不推进、不注入、不清意图。

    ``injected`` 的语义是「agent 确实拿到了正文」。此刻 agent 只拿到一行无指令的标记，推进就等于
    谎报交付，TTL 对账会把一次「投出去了没人接」误判成「看到了没决定」而静默 expired。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(tmp_path / "locks")
        try:
            token, marker = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-bodyless-sess", persist_body_text_for_agent=False
            )
            import kanban_followup_lock as kfl  # noqa: PLC0415

            intent_path = kfl.write_decision_point_delivery_intent(
                task_id="task-banner-bodyless",
                session_id="banner-bodyless-sess",
                worktree=str(repo.resolve()),
                cwd=str(repo),
                decision_token=token,
                candidate_fingerprint="fp",
                kanban_project_path=str(repo),
                idempotency_key=f"rvf-decision-point-{token}",
                state=kfl.DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
            )
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": marker,
                    "cwd": str(repo),
                    "session_id": "banner-bodyless-sess",
                    "hook_event_name": "UserPromptSubmit",
                    "kanban_task_id": "task-banner-bodyless",
                }
            )
            assert "hookSpecificOutput" not in payload, payload
            assert payload.get("decision_token_advanced_to_injected") is False, payload
            assert payload.get("decision_point_banner_body_text_missing") is True, payload
            assert "推进为 injected" not in str(payload.get("systemMessage") or ""), payload
            assert dts.read_decision_token(token).state == dts.STATE_MINTED
            assert Path(intent_path).is_file(), "未推进时不得清掉投递意图"
        finally:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)



def test_rvf_user_prompt_submit_concurrent_decision_point_marker_arrivals_inject_body_exactly_once(
    tmp_path: Path,
) -> None:
    """同一枚 token 的两次 UPS **并发**到达 ⇒ 只有赢得锁内 ``minted → injected`` 迁移的那一次注入正文。

    旧实现在 token 锁外 ``read_decision_token`` 判定「首次到达」，随后才调幂等的 ``mark_injected``：
    两次到达都读到 minted、后到者的 ``mark_injected`` 幂等返回 ⇒ 两次都注入正文、都声称推进成功。

    用受控交错复现：第一次调用的锁外读刚返回（看到 minted）时，插入另一次**完整**的调用，再放第一次
    带着陈旧的 minted 快照继续往下走。判据只看生产函数的输出与磁盘，不手抄任何正文。
    """
    submit = load_rvf_user_prompt_submit_module()
    dts, _il, _summary = _load_manual_decision_modules()
    stop_hook = _load_stop_hook_module_for_ups_tests()
    repo = _manual_candidate_repo(tmp_path)
    event_base = {
        "cwd": str(repo),
        "session_id": "banner-concurrent-sess",
        "hook_event_name": "UserPromptSubmit",
        "kanban_task_id": "task-banner-concurrent",
    }
    with _manual_decision_env(tmp_path):
        os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(tmp_path / "locks")
        real_read_decision_token = dts.read_decision_token
        interleaved_arrival_payloads: list[dict] = []
        interleaving_already_started: list[bool] = []
        try:
            token, marker = _mint_out_of_band_decision_token(
                dts, stop_hook, repo, "banner-concurrent-sess"
            )

            def read_then_let_another_arrival_run_to_completion(*args, **kwargs):
                lookup = real_read_decision_token(*args, **kwargs)
                if not interleaving_already_started:
                    # 只交错一次：第一次到达的锁外读已返回 minted，此刻另一次到达完整跑完（先置标志，
                    # 否则插入的那次调用自己的读也会再触发交错）。
                    interleaving_already_started.append(True)
                    assert lookup.state == dts.STATE_MINTED, lookup
                    interleaved_arrival_payloads.append(
                        submit.inspect_user_prompt_submit({**event_base, "prompt": marker})
                    )
                return lookup

            dts.read_decision_token = read_then_let_another_arrival_run_to_completion
            try:
                stale_snapshot_arrival = submit.inspect_user_prompt_submit(
                    {**event_base, "prompt": marker}
                )
            finally:
                dts.read_decision_token = real_read_decision_token

            assert len(interleaved_arrival_payloads) == 1, interleaved_arrival_payloads
            winner = interleaved_arrival_payloads[0]
            assert winner["status"] == "decision_point_banner_delivered", winner
            assert winner.get("decision_token_advanced_to_injected") is True, winner
            assert "hookSpecificOutput" in winner, winner

            # 输家：持有陈旧的 minted 快照，但锁内输掉迁移权 ⇒ 走「重复到达」出口。
            assert "decision_token_observation_error" not in stale_snapshot_arrival, stale_snapshot_arrival
            assert "hookSpecificOutput" not in stale_snapshot_arrival, stale_snapshot_arrival
            assert stale_snapshot_arrival.get("decision_token_advanced_to_injected") is not True, (
                stale_snapshot_arrival
            )
            assert (
                stale_snapshot_arrival.get("decision_point_banner_resubmitted_without_body_reinjection")
                is True
            ), stale_snapshot_arrival
            assert (
                stale_snapshot_arrival.get(
                    "decision_point_banner_concurrent_arrival_lost_first_injection_claim"
                )
                is True
            ), stale_snapshot_arrival
            system_message = str(stale_snapshot_arrival.get("systemMessage") or "")
            assert "只在首次到达时注入" in system_message, system_message
            assert "推进为 injected" not in system_message, system_message
            assert "hookSpecificOutput" not in submit._render_hook_payload(stale_snapshot_arrival)

            # 磁盘上恰好一次 minted → injected。
            payload_on_disk = dts.read_decision_token(token).payload
            assert payload_on_disk["state"] == dts.STATE_INJECTED, payload_on_disk
            transitions_to_injected = [
                entry
                for entry in payload_on_disk.get("history") or []
                if entry.get("to") == dts.STATE_INJECTED
            ]
            assert len(transitions_to_injected) == 1, payload_on_disk.get("history")

            # store 层直接判据：已到达过的 token 再 claim 只能输，且不改任何东西。
            second_claim = dts.claim_first_minted_to_injected_transition_exclusively(token)
            assert second_claim.this_call_won_first_minted_to_injected_transition is False
            assert second_claim.state_observed_inside_lock_before_claim == dts.STATE_INJECTED
            assert dts.read_decision_token(token).payload == payload_on_disk
        finally:
            dts.read_decision_token = real_read_decision_token
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
