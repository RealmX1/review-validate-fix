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
import sys
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_rvf_user_prompt_submit_dispatches_shared_workflow',
    'test_rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist',
    'test_rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing',
    'test_rvf_user_prompt_submit_marker_without_token',
    'test_rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery',
    'test_rvf_user_prompt_submit_clears_pending_on_delivery',
    'test_rvf_user_prompt_submit_structured_manual_detection_catches_namespaced',
    'test_rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare',
    'test_rvf_user_prompt_submit_manual_scope_directive_passes_primary_files',
    'test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger',
    'test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking',
    'test_rvf_user_prompt_submit_backfills_child_session',
    'test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode',
    'test_rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage',
    'test_rvf_user_prompt_submit_render_hook_payload_merges_channels',
    'test_rvf_user_prompt_submit_captures_round_baseline',
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
        marker_path = lock_root / "task-task-fu.json"
        assert payload.get("kanban_followup_in_progress_marker_path") == str(marker_path)
        assert marker_path.exists()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["state"] == "in_progress"
        assert marker["kanban_task_id"] == "task-fu"
        assert marker["run_id"] == "rvf-fu-delivered"
        assert marker["expires_at"] > marker["armed_at"]

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
        marker_path = lock_root / "task-task-fu.json"
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


def test_rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    os.environ["CODEX_RVF_LOG_ROOT"] = str(tmp_path / "rvf-state")
    try:
        captured: list[dict[str, object]] = []

        def fake_prepare(record, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            captured.append(
                {
                    "token": record.token,
                    "target_flow": record.payload.get("target_flow"),
                    "dispatch_origin": record.payload.get("dispatch_origin"),
                    "excerpt": user_prompt_excerpt,
                }
            )
            state = {
                "started_at": "2026-05-07T00:00:00Z",
                "completed_at": "2026-05-07T00:00:01Z",
                "status": "completed",
                "target_flow": record.payload.get("target_flow"),
                "artifacts": {},
            }
            new_rvf_run = dict(record.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record, {"rvf_run": new_rvf_run})
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
                    "prompt": "/review-validate-fix please review my work",
                    "cwd": str(repo),
                    "session_id": "manual-session",
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
            )
            assert payload["status"] == "manual_prep_created"
            assert payload["dispatch_origin"] == "post_user_prompt_manual"
            assert payload["workflow_started"] is True
            assert payload["shared_workflow_state"]["status"] == "completed"
            assert len(captured) == 1
            assert captured[0]["target_flow"] == "flow-manual"
            assert captured[0]["dispatch_origin"] == "post_user_prompt_manual"
            # Prep file must exist on disk under the configured root.
            prep_path = root / f"{payload['token']}.json"
            assert prep_path.is_file()
            # Manual same-session path must inject the prep file path back into
            # the agent context via hookSpecificOutput.additionalContext.
            assert "hookSpecificOutput" in payload
            hook_specific = payload["hookSpecificOutput"]
            assert hook_specific["hookEventName"] == "UserPromptSubmit"
            additional_context = hook_specific["additionalContext"]
            assert "RVF dispatch prep" in additional_context
            assert str(prep_path) in additional_context
            assert "shared_workflow_state.status: completed" in additional_context
            # 成功触发须同时给用户一条可见 systemMessage（user-facing，不进模型
            # 上下文），与上面 model-facing 的 additionalContext **共存**。
            assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"]
            assert "RVF UPS" in payload["systemMessage"]
            assert "post_user_prompt_manual" in payload["systemMessage"]
            assert "status=completed" in payload["systemMessage"]
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)
        os.environ.pop("CODEX_RVF_LOG_ROOT", None)


def test_rvf_user_prompt_submit_manual_scope_directive_passes_primary_files(tmp_path: Path) -> None:
    """manual 触发内联 `scope:` → 解析出的 primary 文件作为 extra_primary_files 传入 prepare。"""
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    os.environ["CODEX_RVF_LOG_ROOT"] = str(tmp_path / "rvf-state")
    try:
        captured: list[dict[str, object]] = []

        def fake_prepare(
            record, *, timeout_seconds=60.0, user_prompt_excerpt=None, extra_primary_files=None, **_
        ):
            captured.append({"extra_primary_files": extra_primary_files})
            state = {
                "started_at": "2026-05-07T00:00:00Z",
                "completed_at": "2026-05-07T00:00:01Z",
                "status": "completed",
                "artifacts": {},
            }
            new_rvf_run = dict(record.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record, {"rvf_run": new_rvf_run})
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
                    "prompt": "/review-validate-fix please review scope: src/a.py, src/b.py",
                    "cwd": str(repo),
                    "session_id": "manual-scope-session",
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
            )
            assert payload["status"] == "manual_prep_created"
            assert payload["workflow_started"] is True
            assert payload["manual_scope_files"] == ["src/a.py", "src/b.py"]
            assert len(captured) == 1
            assert captured[0]["extra_primary_files"] == ["src/a.py", "src/b.py"]
            additional_context = payload["hookSpecificOutput"]["additionalContext"]
            assert "inline scope (primary): src/a.py, src/b.py" in additional_context
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)
        os.environ.pop("CODEX_RVF_LOG_ROOT", None)


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
    # 1) 两通道并存（manual 成功路径）：合并、不互相顶掉。
    both = render({"systemMessage": "RVF UPS：派发已就绪", "hookSpecificOutput": hook_block, "continue": True})
    assert both is not None
    assert both["systemMessage"] == "RVF UPS：派发已就绪"
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
    marker = rbm.read_round_baseline_marker(task_id=None, session_id="sess-capture", root=state_root)
    assert marker is not None, "expected a round-baseline marker to be written"
    assert marker["baseline_head"] == head, (marker.get("baseline_head"), head)

