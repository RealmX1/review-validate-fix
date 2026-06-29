#!/usr/bin/env python3
"""kanban followup 派发与锁 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_kanban_followup_auto_review_scope_uses_one_hour_lease_ttl',
    'test_kanban_followup_without_task_id_does_not_allocate_review_scope',
    'test_kanban_followup_mode_injects_current_task_message',
    'test_kanban_followup_terminal_fallback_reports_unconfirmed_and_writes_pending',
    'test_kanban_followup_active_pending_skips_redispatch',
    'test_kanban_followup_stale_pending_redispatches_and_reports',
    'test_kanban_followup_stranded_sweep_escalates_other_skips_current',
    'test_kanban_followup_stranded_sweep_consumed_refinement_clears_marker',
    'test_kanban_followup_stranded_sweep_redispatch_enabled_but_unreachable',
    'test_kanban_followup_s2_redispatch_stable_idem_and_preserves_prompt_path',
    'test_kanban_followup_title_falls_back_to_local_board_state',
    'test_kanban_followup_title_ignores_unrelated_board_with_same_task_id',
    'test_kanban_followup_title_uses_session_matched_board_state',
    'test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd',
    'test_kanban_followup_blocks_expired_codex_login_before_message',
    'test_kanban_followup_mode_without_task_id_reports_without_fallback',
    'test_kanban_followup_trigger_marker_skips_one_turn',
    'test_kanban_followup_in_progress_marker_skips_new_followup',
    'test_kanban_followup_in_progress_lock_reengage_nudges_within_budget',
    'test_kanban_followup_in_progress_lock_skips_after_reengage_budget_exhausted',
    'test_kanban_followup_lock_write_marker_preserves_reengage_nudge_count_on_rearm',
    'test_kanban_followup_in_progress_lock_does_not_consume_nudge_budget_on_trigger_turn',
    'test_kanban_followup_stale_takeover_rechecks_marker_before_unlink',
    'test_kanban_followup_shared_lock_blocks_second_dispatch_with_different_state_roots',
    'test_awaiting_dispatched_agent_marker_skips_rvf_when_main_agent_parks_on_dispatch',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def _write_fake_kanban_empty_list_client(path: Path) -> str:
    """Hermetic fake：让 sweep 的 liveness `task list` 子进程拿到空 tasks → 判 unknown → 退回 TTL。

    本测试簇验的是「stale marker → TTL 兜底升级」，不应依赖真机 kanban server；session.state 真相
    路径（stopped→即升级 / alive→不升级）由 test_kanban_*_liveness / *_skips_alive_session 专测。
    """
    path.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': []}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )
    return str(path)


def test_kanban_followup_auto_review_scope_uses_one_hour_lease_ttl(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-followup-ttl")
    state = tmp / "state"
    original_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    try:
        os.environ["CODEX_RVF_FORK_MODE"] = "kanban-followup"
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript), "task_id": "task-ttl"}
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        result = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if original_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log
        if original_mode is None:
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
        else:
            os.environ["CODEX_RVF_FORK_MODE"] = original_mode

    assert result is None
    meta = getattr(ledger, "tracker_scope_meta", None)
    assert isinstance(meta, dict)
    db_path = Path(meta["tracker_dir"]) / "tracker.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT ttl_seconds FROM leases WHERE lease_id=?",
            (meta["tracker_lease_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 3600


def test_kanban_followup_without_task_id_does_not_allocate_review_scope(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-followup-no-task")
    state = tmp / "state"
    original_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    task_env_names = ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID")
    original_task_env = {name: os.environ.get(name) for name in task_env_names}
    try:
        os.environ["CODEX_RVF_FORK_MODE"] = "kanban-followup"
        for name in task_env_names:
            os.environ.pop(name, None)
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        result = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if original_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log
        if original_mode is None:
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
        else:
            os.environ["CODEX_RVF_FORK_MODE"] = original_mode
        for name, value in original_task_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert result is not None
    assert "reason=kanban_followup_missing_task_id" in result["systemMessage"]
    meta = getattr(ledger, "tracker_scope_meta", None)
    assert meta is None

    diff_tracker = sys.modules["diff_tracker"]
    _, _, tracker_dir, db_path, _, _ = diff_tracker._lease_repo_paths(
        repo,
        log_root_override=state / "global-diff-tracker",
    )
    assert Path(tracker_dir).exists()
    conn = sqlite3.connect(str(db_path))
    try:
        has_leases_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='leases'"
        ).fetchone()[0]
        count = (
            conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            if has_leases_table
            else 0
        )
    finally:
        conn.close()
    assert count == 0


def test_kanban_followup_mode_injects_current_task_message(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        f"    print(json.dumps({{'ok': True, 'tasks': [{{'id': 'task-77', 'title': 'Fix RVF follow-up source metadata', 'workspacePath': {str(repo)!r}}}]}}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'msg-77', 'status': 'queued', 'checkpoint_id': 'checkpoint-1'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "kanban-followup-enqueued"
    assert latest["backend"] == "kanban-followup"
    assert latest["rvf_backend"] == "kanban-followup"
    assert latest["rvf_state_phase"] == "prepare"
    assert latest["cline_kanban_task_id"] == "task-77"
    assert latest["cline_kanban_attempt_id"] == "attempt-9"
    assert latest["cline_kanban_message_id"] == "msg-77"
    assert latest["cline_kanban_checkpoint_id"] == "checkpoint-1"
    # 新契约：Stop dispatch 不再 arm in-progress 锁；arm 已移交目标 session 的
    # UserPromptSubmit hook，仅在注入的 follow-up trigger 真正投递落地时才上锁。
    # 因此 dispatch 这一刻没有 marker 被写出（治本 squat：投递静默失败不留空转锁）。
    assert latest.get("kanban_followup_in_progress_marker_path") is None
    assert latest["cline_kanban_task_title"] == "Fix RVF follow-up source metadata"
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_task_lookup"
    assert latest["parent_thread_id"] == session_id
    assert latest["parent_thread_path"] == str(transcript.resolve())
    assert latest["parent_source_kind"] == "cline-kanban-task"
    assert latest["parent_conversation_ref"] == "Fix RVF follow-up source metadata"
    assert latest["parent_conversation_name"] == latest["parent_conversation_ref"]
    assert latest["parent_conversation_name_source"] == "cline_kanban_task_lookup"
    assert latest["parent_codex_session_ref"] == "Codex 2026-05-01T11-25-17 019de191"
    assert latest["parent_codex_session_name_source"] == "session_ref_fallback"
    assert latest["parent_codex_url"] == f"codex://local/{session_id}"
    assert Path(str(latest["parent_origin_path"])).exists()
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "list", "message"]
    message_argv = calls[2]["argv"]
    assert "--task-id" in message_argv
    assert message_argv[message_argv.index("--task-id") + 1] == "task-77"
    assert "--attempt-id" in message_argv
    assert message_argv[message_argv.index("--attempt-id") + 1] == "attempt-9"
    assert "--prompt-file" in message_argv
    prompt_path = Path(message_argv[message_argv.index("--prompt-file") + 1])
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert prompt_text.startswith("$review-validate-fix\n")
    assert "RVF_KANBAN_FOLLOWUP_TRIGGER" in prompt_text
    assert "RVF_CURRENT_TASK_ID: task-77" in prompt_text
    assert "RVF_CURRENT_ATTEMPT_ID: attempt-9" in prompt_text
    assert "RVF_PARENT_CONVERSATION_REF: Fix RVF follow-up source metadata" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME: Fix RVF follow-up source metadata" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: cline_kanban_task_lookup" in prompt_text
    assert "RVF_PARENT_SOURCE_KIND: cline-kanban-task" in prompt_text
    assert "RVF_PARENT_KANBAN_TASK_ID: task-77" in prompt_text
    assert "RVF_PARENT_KANBAN_ATTEMPT_ID: attempt-9" in prompt_text
    assert "RVF_PARENT_KANBAN_TASK_TITLE: Fix RVF follow-up source metadata" in prompt_text
    assert "`source Kanban task id`" in prompt_text
    assert "`source Kanban attempt id`" in prompt_text
    assert "`source Kanban task title at trigger`" in prompt_text
    assert "RVF_PARENT_CODEX_SESSION_REF: Codex 2026-05-01T11-25-17 019de191" in prompt_text
    assert f"RVF_PARENT_CODEX_URL: codex://local/{session_id}" in prompt_text
    assert f"RVF_PARENT_TRANSCRIPT_PATH: {transcript.resolve()}" in prompt_text
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert f"RVF_DISPATCH=token={prep_token}" in prompt_text
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt_text
    assert prep["origin_session_id"] == session_id
    assert Path(str(prep["origin_repo"])).resolve() == repo.resolve()
    assert prep["target_flow"] == "flow-1-self-rising"
    assert prep["target_kanban_task_id"] == "task-77"
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert "如果当前会话位于 Cline Kanban task 内，它们应优先使用 Kanban task" in prompt_text
    assert "RVF_CLINE_KANBAN_TASK" not in prompt_text
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
    assert calls[0]["suppress"] is None
    assert calls[1]["suppress"] is None


def test_kanban_followup_terminal_fallback_reports_unconfirmed_and_writes_pending(
    tmp_path: Path,
) -> None:
    """terminal fallback 投递（message_id 以 ``terminal:`` 开头）→ 诚实上报 dispatched-unconfirmed + 写 pending。

    无 app-server socket 时外部 CLI 走 terminal fallback、返回乐观 ``status:started``，但消息未必
    成为真实 turn。RVF 不再谎报 injected/started，而是 dispatched-unconfirmed，并写 pending 供对账。
    """
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        f"    print(json.dumps({{'ok': True, 'tasks': [{{'id': 'task-77', 'title': 'Fix RVF follow-up source metadata', 'workspacePath': {str(repo)!r}}}]}}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'ok': True, 'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'terminal:task-77:rvf-run', 'turn_id': '3', 'status': 'started', 'checkpoint_id': 'checkpoint-1'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "kanban-followup-dispatched-unconfirmed"
    assert latest["kanban_followup_delivery_channel"] == "terminal"
    assert latest["kanban_followup_delivery_confirmed"] is False
    # dispatch 这一刻仍不 arm in-progress 锁（arm 仍归 UPS）。
    assert latest.get("kanban_followup_in_progress_marker_path") is None
    # 写了 pending marker，内容自洽（state / token / channel）。
    pending_path = latest.get("kanban_followup_pending_marker_path")
    assert isinstance(pending_path, str) and Path(pending_path).exists()
    pending = json.loads(Path(pending_path).read_text(encoding="utf-8"))
    assert pending["state"] == "dispatched_unconfirmed"
    assert pending["delivery_channel"] == "terminal"
    prep_token = dispatch_prep_payload(latest)["token"]
    assert pending["token"] == prep_token
    assert pending["kanban_task_id"] == "task-77"


def test_kanban_followup_active_pending_skips_redispatch(tmp_path: Path) -> None:
    """active pending（dispatch→delivery 在途窗口内）→ 本次 Stop 跳过重复 dispatch，避免双注入。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    pending_dir.mkdir(parents=True)
    pending_path = pending_dir / "task-task-active.json"
    pending_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "dispatched_unconfirmed",
                "dispatched_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-active",
                "session_id": "session-active",
                "run_id": "rvf-inflight",
                "token": "inflighttoken000",
                "delivery_channel": "terminal",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-active', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "stop_hook_active": False,
            "last_user_message": "测试在途窗口去重。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "KANBAN_TASK_ID": "task-active",
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_dispatch_in_flight" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_dispatch_in_flight"
    # 跳过发生在任何 kanban client 调用之前。
    assert not client_calls.exists()
    # pending 仍在（未被清，等其落地或超时）。
    assert pending_path.exists()


def test_kanban_followup_stale_pending_redispatches_and_reports(tmp_path: Path) -> None:
    """stale pending（在途窗口已过仍未确认）→ 判定上次静默丢投：上报 + 清旧 pending + 放行重投。"""
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    pending_dir.mkdir(parents=True)
    pending_path = pending_dir / "task-task-77.json"
    pending_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "dispatched_unconfirmed",
                "dispatched_at": "2026-05-01T00:00:00Z",
                "expires_at": "2026-05-01T00:00:00Z",
                "kanban_task_id": "task-77",
                "session_id": session_id,
                "run_id": "rvf-dropped",
                "token": "oldtokenold00000",
                "delivery_channel": "terminal",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        f"    print(json.dumps({{'ok': True, 'tasks': [{{'id': 'task-77', 'title': 'Fix RVF follow-up source metadata', 'workspacePath': {str(repo)!r}}}]}}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'ok': True, 'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'terminal:task-77:rvf-redispatch', 'turn_id': '4', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    parse_json(stdout)
    latest = latest_summary(state)
    # 没有被 pending 挡住——重投真的发生（terminal 再投 → 又是 unconfirmed）。
    assert latest["status"] == "kanban-followup-dispatched-unconfirmed"
    assert client_calls.exists()
    actions = [json.loads(line)["argv"][0] for line in client_calls.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "message" in actions
    # 上报了「上次静默丢投」事件。
    assert any(
        event.get("event") == "kanban_followup_prior_dispatch_unconfirmed"
        for event in latest_events(state)
    )
    # 旧 pending 被新 dispatch 覆盖为新 token。
    assert pending_path.exists()
    refreshed = json.loads(pending_path.read_text(encoding="utf-8"))
    assert refreshed["token"] != "oldtokenold00000"
    assert refreshed["token"] == dispatch_prep_payload(latest)["token"]


def test_kanban_followup_stranded_sweep_escalates_other_skips_current(tmp_path: Path) -> None:
    """S1b：任意会话的 Stop 在入口扫荡——升级**别的** task 的 stale pending（OS 通知 + 盖戳 + 保留
    marker），但**跳过当前 Stop 的 task**（交既有同 task 对账）；二次立即 Stop 被 RENOTIFY 抑制。"""
    repo = init_repo(tmp_path / "clean", dirty=False)  # 干净 repo → 主流程 skip，但 sweep 仍在入口跑
    state = tmp_path / "state"
    pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    pending_dir.mkdir(parents=True)
    other = _write_stranded_pending(
        pending_dir, task_id="taskOTHER", token="aaaaaaaaaaaaaaaa",
        project_path=str(repo), title="修复登录",
    )
    current = _write_stranded_pending(
        pending_dir, task_id="taskCURRENT", token="cccccccccccccccc",
        project_path=str(repo), title="当前任务",
    )
    notify_log = tmp_path / "notify.log"
    notifier = _logging_notifier(tmp_path / "fake_notifier.py", notify_log)
    extra = {
        "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
        "CODEX_RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
        "NOTIFY_LOG": str(notify_log),
        # 让 sweep 把 taskCURRENT 视为当前 task（current_kanban_task_id 读 env）。
        "KANBAN_TASK_ID": "taskCURRENT",
        # liveness `list` 走 hermetic fake（空 tasks → unknown → TTL 兜底），不碰真机 server。
        "CODEX_RVF_CLINE_KANBAN_CLIENT": _write_fake_kanban_empty_list_client(
            tmp_path / "fake_list_client.py"
        ),
        "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
    }
    invoke({"cwd": str(repo), "stop_hook_active": False}, extra_env=extra, state_dir=state)

    events = latest_events(state)
    escalated = [
        e for e in events if e.get("event") == "kanban_followup_pending_stranded_escalated"
    ]
    # 只升级 taskOTHER，绝不升级当前 task。
    assert [e.get("cline_kanban_task_id") for e in escalated] == ["taskOTHER"]
    # S2 默认关：升级事件里 redispatch 为 disabled（已接线、但不开则纯通知，不重投）。
    assert escalated[0].get("kanban_followup_stranded_redispatch", {}).get("reason") == "disabled"
    # OS 通知发了一次、带 -open。
    calls = [
        json.loads(line)
        for line in notify_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1 and "-open" in calls[0]
    # taskOTHER marker 保留 + 盖 last_notified_at + token 原样保留。
    assert other.exists()
    other_after = json.loads(other.read_text(encoding="utf-8"))
    assert other_after.get("last_notified_at")
    assert other_after.get("token") == "aaaaaaaaaaaaaaaa"
    # 当前 task 未被 sweep 升级/通知（escalated 列表里没有它；通知只发了 taskOTHER 一次）。
    # 注：当前 task 的 stale marker 交既有同 task 对账（_kanban_followup_pending_decision）
    # 处理，可能被它清除——这正是 sweep 跳过当前 task 的目的，故此处不对该 marker 存留做断言。
    assert "taskCURRENT" not in [e.get("cline_kanban_task_id") for e in escalated]
    assert all("taskCURRENT" not in str(arg) for call in calls for arg in call)

    # 二次立即 Stop → RENOTIFY 抑制，不再新发通知。
    invoke({"cwd": str(repo), "stop_hook_active": False}, extra_env=extra, state_dir=state)
    calls2 = [
        json.loads(line)
        for line in notify_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls2) == 1


def test_kanban_followup_stranded_sweep_consumed_refinement_clears_marker(tmp_path: Path) -> None:
    """S1b 可选精修：marker 的 origin_transcript_path 命中 RVF_DISPATCH=token=<token> → 判迟到消费，
    清 marker、不通知；transcript 无该 token → 不据此判 consumed，仍按 stranded 升级。"""
    repo = init_repo(tmp_path / "clean", dirty=False)
    state = tmp_path / "state"
    pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    pending_dir.mkdir(parents=True)
    # consumed：transcript 含该 token。
    consumed_tx = tmp_path / "consumed.jsonl"
    consumed_tx.write_text(
        '{"role":"user","content":"... RVF_DISPATCH=token=dddddddddddddddd ..."}\n',
        encoding="utf-8",
    )
    consumed = _write_stranded_pending(
        pending_dir, task_id="taskConsumed", token="dddddddddddddddd",
        project_path=str(repo), origin_transcript_path=str(consumed_tx),
    )
    # not-consumed：transcript 不含该 token。
    miss_tx = tmp_path / "miss.jsonl"
    miss_tx.write_text('{"role":"user","content":"no dispatch token here"}\n', encoding="utf-8")
    stranded = _write_stranded_pending(
        pending_dir, task_id="taskStranded", token="eeeeeeeeeeeeeeee",
        project_path=str(repo), origin_transcript_path=str(miss_tx),
    )
    notify_log = tmp_path / "notify.log"
    notifier = _logging_notifier(tmp_path / "fake_notifier.py", notify_log)
    extra = {
        "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
        "CODEX_RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
        "NOTIFY_LOG": str(notify_log),
        # liveness `list` 走 hermetic fake（空 tasks → unknown → TTL 兜底），不碰真机 server。
        "CODEX_RVF_CLINE_KANBAN_CLIENT": _write_fake_kanban_empty_list_client(
            tmp_path / "fake_list_client.py"
        ),
        "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
    }
    invoke({"cwd": str(repo), "stop_hook_active": False}, extra_env=extra, state_dir=state)

    events = latest_events(state)
    # consumed marker 被清、记 reconciled_consumed、不通知。
    assert not consumed.exists()
    assert any(
        e.get("event") == "kanban_followup_pending_reconciled_consumed"
        and e.get("cline_kanban_task_id") == "taskConsumed"
        for e in events
    )
    # 未命中 token 的 marker → 仍按 stranded 升级、保留。
    assert stranded.exists()
    assert any(
        e.get("event") == "kanban_followup_pending_stranded_escalated"
        and e.get("cline_kanban_task_id") == "taskStranded"
        for e in events
    )
    # 只为 stranded（taskStranded）发了一次通知，consumed 不通知。
    calls = [
        json.loads(line)
        for line in notify_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1


def test_kanban_followup_stranded_sweep_redispatch_enabled_but_unreachable(tmp_path: Path) -> None:
    """S2（CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH=1）：app-server 不可达 → 诚实放弃重投，
    仍发通知 + 保留 marker，绝不谎报已自动跑。"""
    repo = init_repo(tmp_path / "clean", dirty=False)
    state = tmp_path / "state"
    pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    pending_dir.mkdir(parents=True)
    # 真实 stranded marker 总带已渲染的 prompt（含旧 dispatch 注入块），让 S2 走到 app-server 闸门。
    old_prompt = tmp_path / "old-prompt.md"
    old_prompt.write_text(
        "$review-validate-fix\n\nRVF dispatch prep file:\nRVF_DISPATCH=token=abcabcabcabcabca\n",
        encoding="utf-8",
    )
    marker = _write_stranded_pending(
        pending_dir, task_id="taskS2", token="abcabcabcabcabca",
        project_path=str(repo), title="S2 重投", prompt_path=str(old_prompt),
    )
    notify_log = tmp_path / "notify.log"
    notifier = _logging_notifier(tmp_path / "fake_notifier.py", notify_log)
    extra = {
        "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
        "CODEX_RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
        "NOTIFY_LOG": str(notify_log),
        "CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH": "1",
        # 显式指向不存在的 app-server socket，确定性不可达（避免误连真机 app-server）。
        "CODEX_RVF_APP_SERVER_SOCKET": str(tmp_path / "no-such.sock"),
        # liveness `list` 走 hermetic fake（空 tasks → unknown → TTL 兜底），不碰真机 server。
        "CODEX_RVF_CLINE_KANBAN_CLIENT": _write_fake_kanban_empty_list_client(
            tmp_path / "fake_list_client.py"
        ),
        "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
    }
    invoke({"cwd": str(repo), "stop_hook_active": False}, extra_env=extra, state_dir=state)

    events = latest_events(state)
    escalated = [
        e for e in events if e.get("event") == "kanban_followup_pending_stranded_escalated"
    ]
    assert len(escalated) == 1
    redispatch = escalated[0].get("kanban_followup_stranded_redispatch", {})
    assert redispatch.get("redispatched") is False
    assert redispatch.get("reason") == "app-server-unreachable"
    # marker 保留（未重投、未确认）；通知仍发了一次。
    assert marker.exists()
    calls = [
        line for line in notify_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(calls) == 1


def test_kanban_followup_s2_redispatch_stable_idem_and_preserves_prompt_path(tmp_path: Path) -> None:
    """S2 重投可达路径回归（RVF-001 / RVF-002）：in-process 调用 _maybe_redispatch_*，monkeypatch
    app-server 可达 + 假派发，断言——
      RVF-001：idempotency_key 必须绑定**稳定** stranded token（传入的 token），不含每次新铸的
        fresh prep token；
      RVF-002：terminal 未确认重投改写 pending 必须带上新派发的 prompt_path/turn_id（否则下次
        重投 missing-prompt-path）。
    e2e 路径需 websocket-协议假 socket（过重），故此处走 in-process 单测直接锁住两个修复。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    import importlib.util

    spec = importlib.util.spec_from_file_location("cs_s2_under_test", SCRIPT)
    cs = importlib.util.module_from_spec(spec)
    sys.modules["cs_s2_under_test"] = cs
    spec.loader.exec_module(cs)

    old_prompt = tmp_path / "old-prompt.md"
    old_prompt.write_text(
        "$review-validate-fix\n\nRVF dispatch prep file:\nRVF_DISPATCH=token=oldoldoldoldoldo\n",
        encoding="utf-8",
    )

    class _FakePrep:
        token = "freshtoken000000"
        path = tmp_path / "fresh-prep.json"

    class _FakeLedger:
        run_id = "run-redispatch"
        run_dir = str(tmp_path / "rd")

    recorded: dict[str, dict] = {}
    cs.select_existing_app_server_socket_for_metadata = lambda: (tmp_path / "sock", "explicit", {})
    cs.can_connect_app_server_socket = lambda p: True
    cs.write_dispatch_prep_file = lambda **kw: _FakePrep()

    def _fake_start(**kw):
        recorded["start"] = kw
        return {
            "message_id": "terminal:taskX:redispatch",
            "prompt_path": "/new/redispatch-prompt.md",
            "turn_id": "7",
        }

    cs.start_cline_kanban_followup_message = _fake_start

    def _fake_write_pending(**kw):
        recorded["pending"] = kw
        return tmp_path / "pending.json"

    cs.write_kanban_followup_pending = _fake_write_pending
    cs.clear_kanban_followup_pending = lambda **kw: []

    marker = {
        "kanban_task_id": "taskX",
        "kanban_project_path": "/repo",
        "prompt_path": str(old_prompt),
        "session_id": "sX",
        "repo": "/repo",
        "cwd": "/repo",
        "run_dir": str(tmp_path / "origin-run"),
        "kanban_attempt_id": "att",
        "kanban_task_title": "标题",
        "kanban_task_title_source": "src",
        "origin_transcript_path": str(tmp_path / "tx.jsonl"),
    }
    saved = os.environ.get("CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH")
    os.environ["CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = "1"
    try:
        result = cs._maybe_redispatch_stranded_kanban_followup(
            marker, _FakeLedger(), token="stabletoken00000"
        )
    finally:
        if saved is None:
            os.environ.pop("CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH", None)
        else:
            os.environ["CODEX_RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = saved

    assert result.get("redispatched") is True
    # RVF-001：idem 绑定稳定 token，绝不含 fresh prep token。
    idem = recorded["start"]["idempotency_key"]
    assert idem == f"rvf-redispatch-{cs.safe_token('taskX')}-stabletoken00000"
    assert "freshtoken000000" not in idem
    # 重投子进程带 timeout（有界）。
    assert recorded["start"].get("timeout")
    # RVF-002：terminal 未确认重投改写 pending 带新 prompt_path/turn_id + 新 token。
    pending = recorded["pending"]
    assert pending["prompt_path"] == "/new/redispatch-prompt.md"
    assert pending["turn_id"] == "7"
    assert pending["token"] == "freshtoken000000"


def test_kanban_followup_title_falls_back_to_local_board_state(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    kanban_state = tmp_path / "kanban"
    workspace = kanban_state / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    (workspace / "board.json").write_text(
        json.dumps(
            {
                "columns": [
                    {
                        "id": "in_progress",
                        "cards": [
                            {
                                "id": "task-77",
                                "title": 'The kanban "follow up" rvf handoff source chat title',
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    expected_title = 'The kanban "follow up" rvf handoff source chat title'
    assert latest["cline_kanban_task_title"] == expected_title
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_board_lookup"
    assert latest["parent_conversation_ref"] == expected_title
    assert latest["parent_conversation_name_source"] == "cline_kanban_board_lookup"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_board_lookup"
    assert task_lookup["task_list_lookup"]["source"] == "cline_kanban_task_lookup_missing_title"
    assert Path(task_lookup["artifact"]).exists()

    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "list", "message"]
    prompt_path = Path(calls[2]["argv"][calls[2]["argv"].index("--prompt-file") + 1])
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert f"RVF_PARENT_CONVERSATION_REF: {expected_title}" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: cline_kanban_board_lookup" in prompt_text
    assert f"RVF_PARENT_KANBAN_TASK_TITLE: {expected_title}" in prompt_text
    assert "`source Kanban task id`" in prompt_text


def test_kanban_followup_title_ignores_unrelated_board_with_same_task_id(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    kanban_state = tmp_path / "kanban"
    stale_workspace = kanban_state / "workspaces" / "stale-project"
    stale_workspace.mkdir(parents=True)
    (stale_workspace / "board.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-77",
                        "title": "Wrong stale workspace title",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cline_kanban_task_title"] is None
    assert latest["cline_kanban_task_title_source"] is None
    assert latest["parent_conversation_ref"] == "Cline Kanban task task-77"
    assert latest["parent_conversation_name_source"] == "cline_kanban_task_id_fallback"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_task_lookup_missing_title"


def test_kanban_followup_title_uses_session_matched_board_state(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    kanban_state = tmp_path / "kanban"
    matched_workspace = kanban_state / "workspaces" / "task-workspace"
    matched_workspace.mkdir(parents=True)
    (matched_workspace / "sessions.json").write_text(
        json.dumps(
            {
                "session-1": {
                    "taskId": "task-77",
                    "workspacePath": str(repo),
                }
            }
        ),
        encoding="utf-8",
    )
    (matched_workspace / "board.json").write_text(
        json.dumps(
            {
                "columns": [
                    {
                        "cards": [
                            {
                                "id": "task-77",
                                "title": "Session matched workspace title",
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stale_workspace = kanban_state / "workspaces" / "aaa-stale-project"
    stale_workspace.mkdir(parents=True)
    (stale_workspace / "board.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-77",
                        "title": "Wrong stale workspace title",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cline_kanban_task_title"] == "Session matched workspace title"
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_board_lookup"
    assert latest["parent_conversation_ref"] == "Session matched workspace title"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_board_lookup"
    board_lookup = json.loads(Path(task_lookup["artifact"]).read_text(encoding="utf-8"))
    assert board_lookup["matched_board"] == str(matched_workspace / "board.json")
    assert str(stale_workspace / "board.json") not in board_lookup["checked"]


def test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    subdir = repo / "nested"
    subdir.mkdir()
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        f"    print(json.dumps({{'ok': True, 'tasks': [{{'id': 'task-77', 'title': 'Subdir follow-up', 'workspacePath': {str(repo)!r}}}]}}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.pop(key, None)
        for key in ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH")
    }
    try:
        stdout, _ = invoke(
            {
                "cwd": str(subdir),
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_FORK_MODE": "kanban-followup",
                "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "KANBAN_TASK_ID": "task-77",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )
    finally:
        for key, value in original_env.items():
            if value is not None:
                os.environ[key] = value

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cwd"] == str(subdir.resolve())
    assert latest["cline_kanban_project_path"] == str(repo.resolve())
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "list", "message"]
    list_argv = calls[0]["argv"]
    assert list_argv[list_argv.index("--repo") + 1] == str(repo.resolve())
    message_argv = calls[2]["argv"]
    assert message_argv[message_argv.index("--repo") + 1] == str(repo.resolve())


def test_kanban_followup_blocks_expired_codex_login_before_message(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print('session expired; please login again', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(f'unexpected codex argv: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-77', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CODEX_BIN": str(fake_codex),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=provider_health_failed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "provider_health_failed"
    assert latest["backend"] == "kanban-followup"
    assert "codex login" in str(latest["message"])
    assert not client_calls.exists()
    health = read_json_artifact(latest, "provider_health_path")
    results = health["results"]
    assert results[0]["provider"] == "codex"
    assert results[0]["status"] == "failed"
    assert "session expired" in results[0]["stderr"]


def test_kanban_followup_mode_without_task_id_reports_without_fallback(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_missing_task_id" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_missing_task_id"
    assert latest["backend"] == "kanban-followup"
    assert not client_calls.exists()


def test_kanban_followup_trigger_marker_skips_one_turn(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "last_user_message": "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER",
        },
        extra_env={"CODEX_RVF_FORK_MODE": "kanban-followup"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_trigger_turn" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_trigger_turn"


def test_kanban_followup_in_progress_marker_skips_new_followup(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / "task-task-active.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-active",
                "session_id": "session-active",
                "run_id": "rvf-existing",
                "run_dir": str(tmp_path / "existing-run"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-active', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "stop_hook_active": False,
            "last_user_message": "测试再次后台跑。等完成后 finalize handoff.md。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "KANBAN_TASK_ID": "task-active",
            "FAKE_CLIENT_CALLS": str(client_calls),
            # nudge 预算=0 → 关掉 re-engage，锁 active 即静默 skip（历史行为 / loop-break 守卫）。
            # 默认预算>0 时的「先 re-engage 再 skip」由
            # test_kanban_followup_in_progress_lock_reengage_nudges_within_budget 与
            # test_kanban_followup_in_progress_lock_skips_after_reengage_budget_exhausted 覆盖。
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "0",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_in_progress" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_in_progress"
    assert latest["active_rvf_run_id"] == "rvf-existing"
    assert latest["kanban_followup_in_progress_marker_path"] == str(marker_path)
    assert marker_path.exists()
    assert not client_calls.exists()


def test_kanban_followup_in_progress_lock_reengage_nudges_within_budget(
    tmp_path: Path,
) -> None:
    """锁 active + nudge 预算未尽 + 仍有未审 dirty：不静默 skip、也**不再让回常规 gate 重派新 run**，
    而是 force-continue 现有 run（{"decision":"block","reason":...}）唤醒 agent 收尾该轮。

    刻意用 **dirty** repo（复现事故：reset --mixed 物化的未审 dirty 让常规 gate 每次都能找到可审
    scope）证明根因已修——旧实现 return None → 常规 gate 会 mint 重复 RVF run；新实现短路在常规
    gate 之前，绝不派发新 review。断言：输出是 force-continue block payload（不是 skip 的
    {"continue":...}、也不是 launch fork payload）、reason 带阻塞 run_id、summary 记
    kanban_followup_in_progress_reengage、nudge 计数自增到 1。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / "task-task-active.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-active",
                "session_id": "session-active",
                "run_id": "rvf-existing",
                "run_dir": str(tmp_path / "existing-run"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "stop_hook_active": False,
            "last_user_message": "测试再次后台跑。等完成后 finalize handoff.md。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-active",
            # 默认预算=2，这里显式钉死以免环境带入覆盖。
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    # force-continue 契约：恰好 {"decision":"block","reason":...}——既非 skip 的 {"continue":...}，
    # 也非 launch 的 fork payload（证明没经常规 gate 重派新 run）。
    assert set(payload.keys()) == {"decision", "reason"}, payload
    assert payload["decision"] == "block"
    assert "rvf-existing" in payload["reason"]  # reason 带阻塞 run_id，指明续跑哪一轮
    assert "RVF_HANDOFF_FILE" in payload["reason"]  # 指引 agent 用 handoff 收尾、而非新开 review
    latest = latest_summary(state)
    assert latest["reason_code"] == "kanban_followup_in_progress_reengage"
    assert latest["status"] == "reengaged"
    events = latest_events(state)
    nudged = [
        event
        for event in events
        if event.get("event") == "kanban_followup_in_progress_nudged"
    ]
    assert len(nudged) == 1, events
    assert nudged[0].get("reason_code") == "kanban_followup_in_progress_reengage_nudge"
    assert nudged[0].get("kanban_followup_in_progress_nudge_count") == 1
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted.get("reengage_nudge_count") == 1
    assert marker_path.exists()


def test_kanban_followup_in_progress_lock_skips_after_reengage_budget_exhausted(
    tmp_path: Path,
) -> None:
    """nudge 预算用尽（reengage_nudge_count >= 预算）：退回静默 skip，保住 review↔fix loop-break。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / "task-task-active.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-active",
                "session_id": "session-active",
                "run_id": "rvf-existing",
                "run_dir": str(tmp_path / "existing-run"),
                "reengage_nudge_count": 2,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "stop_hook_active": False,
            "last_user_message": "测试再次后台跑。等完成后 finalize handoff.md。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-active",
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_in_progress" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_in_progress"
    # 预算用尽不再自增（marker 计数维持 2，不被 bump 到 3）。
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted.get("reengage_nudge_count") == 2


def test_kanban_followup_lock_write_marker_preserves_reengage_nudge_count_on_rearm(
    tmp_path: Path,
) -> None:
    """Option A：重派发的 delivery re-arm（write_marker 整份覆盖 marker）必须保留既有
    reengage_nudge_count——否则 nudge 预算每轮被清回 0，『预算用尽再退回静默 skip』的 loop-break
    在 kanban『dirty 停→re-engage→重派发→re-arm』循环里永不触发。本测试钉死：首次 arm（无既有
    marker）从 0 起；bump 累积后的 re-arm 既是真·整份覆盖（run_id 换新），又把计数带回（计数跨
    投递存活）。
    """
    module = load_kanban_followup_lock_module()
    root = tmp_path / "locks"
    arm_kwargs = dict(
        task_id="task-rearm",
        session_id="session-rearm",
        repo=None,
        cwd=None,
        root=root,
    )

    # 首次 arm：无既有 marker → 计数缺省 0（不应被 carry 逻辑误置）。
    first = module.write_marker(run_id="rvf-1", run_dir=str(tmp_path / "run-1"), **arm_kwargs)
    assert first is not None
    marker_after_first = module.read_marker(
        task_id="task-rearm", session_id="session-rearm", root=root
    )
    assert module.reengage_nudge_count(marker_after_first) == 0

    # 模拟 Stop 读侧的 nudge 记账：bump 两次 → 计数 2，写在实际命中文件上。
    assert module.bump_reengage_nudge_count(marker_after_first) == 1
    assert (
        module.bump_reengage_nudge_count(
            module.read_marker(task_id="task-rearm", session_id="session-rearm", root=root)
        )
        == 2
    )

    # re-arm（新 run，整份覆盖）：run_id 必须换成 rvf-2（证明是真覆盖而非 no-op），
    # 且 reengage_nudge_count 必须保留为 2（Option A 的核心：计数跨 re-arm 存活）。
    second = module.write_marker(run_id="rvf-2", run_dir=str(tmp_path / "run-2"), **arm_kwargs)
    assert second is not None
    marker_after_rearm = module.read_marker(
        task_id="task-rearm", session_id="session-rearm", root=root
    )
    assert marker_after_rearm.get("run_id") == "rvf-2"
    assert module.reengage_nudge_count(marker_after_rearm) == 2


def test_kanban_followup_in_progress_lock_does_not_consume_nudge_budget_on_trigger_turn(
    tmp_path: Path,
) -> None:
    """trigger turn（latest_user 是注入的 RVF_KANBAN_FOLLOWUP_TRIGGER）+ 锁 active：nudge 分支
    不得消耗预算——本回合必被随后的 KANBAN_FOLLOWUP trigger-skip 无条件挡停，若先 bump 则预算白花、
    削掉真正能 re-engage 的 non-trigger turn 预算（committed-round RVF review elevated 修复的回归守卫）。

    断言：落到 kanban_followup_trigger_turn skip（用精确 reason_code，避免 tmp_path 含测试名
    子串假阳性）、没有 kanban_followup_in_progress_nudged 事件、marker 的 reengage_nudge_count 仍为 0。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / "task-task-active.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-active",
                "session_id": "session-active",
                "run_id": "rvf-existing",
                "run_dir": str(tmp_path / "existing-run"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "stop_hook_active": False,
            "last_user_message": "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-active",
            # 默认预算=2，显式钉死以免环境带入覆盖。
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
        },
        state_dir=state,
    )

    parse_json(stdout)  # 仍是合法 hook 输出
    # trigger turn 应落到 kanban_followup_trigger_turn skip（in_progress_decision 的新 guard 在
    # trigger marker 命中时不 bump、return None，让 trigger-skip 接管）。
    latest = latest_summary(state)
    assert latest["reason_code"] == "kanban_followup_trigger_turn"
    # 关键：本回合没有消耗 nudge 预算。
    events = latest_events(state)
    nudged = [
        event
        for event in events
        if event.get("event") == "kanban_followup_in_progress_nudged"
    ]
    assert nudged == [], events
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted.get("reengage_nudge_count", 0) == 0
    assert marker_path.exists()


def test_kanban_followup_stale_takeover_rechecks_marker_before_unlink(tmp_path: Path) -> None:
    module = load_kanban_followup_lock_module()
    root = tmp_path / "locks"
    marker_path = module.marker_paths(task_id="task-race", session_id=None, root=root)[0]
    marker_path.parent.mkdir(parents=True)
    stale_marker = {
        "marker_version": 1,
        "state": "in_progress",
        "armed_at": "2026-05-21T15:57:55Z",
        "expires_at": "2000-01-01T00:00:00Z",
        "kanban_task_id": "task-race",
        "session_id": "session-race",
        "run_id": "rvf-stale",
        "run_dir": str(tmp_path / "stale-run"),
    }
    active_marker = {
        "marker_version": 1,
        "state": "in_progress",
        "armed_at": "2026-05-21T15:57:56Z",
        "expires_at": "2999-01-01T00:00:00Z",
        "kanban_task_id": "task-race",
        "session_id": "session-race",
        "run_id": "rvf-active",
        "run_dir": str(tmp_path / "active-run"),
    }
    marker_path.write_text(json.dumps(stale_marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    original_read_marker = module.read_marker
    swapped_to_active = False

    def racing_read_marker(*, task_id: str | None, session_id: str | None, root: Path | None = None):
        nonlocal swapped_to_active
        marker = original_read_marker(task_id=task_id, session_id=session_id, root=root)
        if not swapped_to_active and isinstance(marker, dict) and marker.get("run_id") == "rvf-stale":
            swapped_to_active = True
            marker_path.write_text(
                json.dumps(active_marker, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return marker

    module.read_marker = racing_read_marker
    try:
        result = module.acquire_marker(
            task_id="task-race",
            session_id=None,
            run_id="rvf-new",
            run_dir=str(tmp_path / "new-run"),
            repo=str(tmp_path / "repo"),
            cwd=str(tmp_path / "repo"),
            root=root,
        )
    finally:
        module.read_marker = original_read_marker

    assert swapped_to_active
    assert not result.acquired
    assert result.status == module.STATUS_ACTIVE
    assert isinstance(result.marker, dict)
    assert result.marker["run_id"] == "rvf-active"
    final_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert final_marker["run_id"] == "rvf-active"


def test_kanban_followup_shared_lock_blocks_second_dispatch_with_different_state_roots(
    tmp_path: Path,
) -> None:
    # 新契约：Stop dispatch 不再 arm 锁（arm 移交 UserPromptSubmit 投递确认）。本测试
    # 模拟「上一轮 follow-up trigger 已投递落地、UPS 已在共享 lock root arm」之后，
    # 另一个 state dir 的 Stop 仍被该共享锁（按 task_id、跨 state root）挡住、不重复
    # dispatch——即读侧（kanban_followup_in_progress_decision）的跨 state-root 共享语义
    # 在新 arm 模型下保持不变。
    repo = init_repo(tmp_path / "repo", dirty=True)
    session_id = "session-shared"
    shared_lock_root = tmp_path / "shared-followup-lock"
    shared_lock_root.mkdir(parents=True)
    # 直接写一份 marker，等价于 UPS 在投递确认时 arm 的结果（env 模式：marker 直接落在
    # CODEX_RVF_KANBAN_FOLLOWUP_LOCK_ROOT 下，文件名按 task_id）。
    marker_path = shared_lock_root / "task-task-shared.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-06-04T02:29:26Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": "task-shared",
                "session_id": session_id,
                "run_id": "rvf-delivered",
                "run_dir": str(tmp_path / "delivered-run"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-shared', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )
    state_b = tmp_path / "state-b"

    stdout_b, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "last_user_message": "继续推进实现。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(shared_lock_root),
            "KANBAN_TASK_ID": "task-shared",
            "FAKE_CLIENT_CALLS": str(client_calls),
            # 本测试只验「跨 state-root 共享锁挡住第二次 dispatch」的 skip 短路语义，与
            # re-engage nudge 正交：预算=0 关掉 nudge，钉死走静默 skip 分支。
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "0",
        },
        state_dir=state_b,
    )

    payload_b = parse_json(stdout_b)
    assert "reason=kanban_followup_in_progress" in payload_b["systemMessage"]
    latest_b = latest_summary(state_b)
    assert latest_b["status"] == "skipped"
    assert latest_b["reason_code"] == "kanban_followup_in_progress"
    assert latest_b["active_rvf_run_id"] == "rvf-delivered"
    assert latest_b["kanban_followup_in_progress_marker_path"] == str(marker_path)
    # 读侧短路发生在任何 kanban client 调用之前：不会有 list / message。
    assert not client_calls.exists()


def test_awaiting_dispatched_agent_marker_skips_rvf_when_main_agent_parks_on_dispatch(
    tmp_path: Path,
) -> None:
    """RVF 内外统一 shield 的集成验证：主 agent 本轮 Stop 只是 park 等一个已派发的后台/外部
    agent（典型 = 实现期 delegate-to-cursor WRITE 留脏树）→ 即使工作树 DIRTY，``evaluate_stop_event``
    也走 ``awaiting_dispatched_agent`` 静默 skip，**不**落到 dirty gate / 不 mint 新 RVF。

    没有本闸时，dirty 工作树会被 route_reviewable_scope 当成「有未审改动」开审、打断 agent 的等待。
    marker 由 dispatch 层（writer）经 arm_awaiting_dispatched_agent 写入；Stop hook 只是 reader。
    """
    repo = init_repo(tmp_path / "repo", dirty=True)
    session_id = "session-awaiting"
    awaiting_root = tmp_path / "awaiting-root"
    awaiting_root.mkdir(parents=True)

    # writer = dispatch 层：加载 marker 模块并 arm 一条 wait-on marker（root 指向 subprocess
    # 将经 CODEX_RVF_KANBAN_FOLLOWUP_LOCK_ROOT 读到的同一基目录）。
    load_kanban_followup_lock_module()  # 副作用：加载 stop hook，连带 import 同目录 marker 模块
    awaiting = sys.modules["rvf_awaiting_dispatched_agent_marker"]
    marker_path = awaiting.arm_awaiting_dispatched_agent(
        main_session_id=session_id,
        dispatched_agent_id="bg-impl-1",
        dispatcher="delegate-to-cursor",
        description="后台实现 agent，留脏树等收编",
        root=awaiting_root,
    )
    assert marker_path is not None and Path(marker_path).exists()

    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-await', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "last_user_message": "派了个后台 agent 去实现，等它完成后我再收编。",
        },
        extra_env={
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(awaiting_root),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert payload.get("continue") is True  # 静默 skip，不打断 agent 续等（非 decision:block）
    assert "reason=awaiting_dispatched_agent" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "awaiting_dispatched_agent"
    assert latest["awaiting_dispatched_agent_ids"] == ["bg-impl-1"]
    assert latest["awaiting_dispatched_agent_dispatchers"] == ["delegate-to-cursor"]
    # 闸是 read-only：active marker 不被清；且短路发生在 dirty gate / 任何 dispatch 之前。
    assert Path(marker_path).exists()
    assert not client_calls.exists()

