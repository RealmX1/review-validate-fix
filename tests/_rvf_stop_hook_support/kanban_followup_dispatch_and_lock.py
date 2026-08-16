#!/usr/bin/env python3
"""kanban followup 派发与锁 测试簇。

从 tests/test_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from _rvf_test_support.kanban_followup_lock_marker_name import in_progress_lock_marker_name
from _rvf_test_support.stop_decision_agent_directive import (
    agent_directive_body,
    emitted_agent_directive_body,
)

# 由 aggregator（tests/test_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
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
    'test_kanban_followup_s2_redispatch_binds_idempotency_key_to_fresh_token_and_preserves_prompt_path',
    'test_kanban_followup_pending_reconciliation_never_waits_without_an_upper_bound',
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
    'test_kanban_followup_waiting_bootstrap_lock_directs_sync_instead_of_handoff',
    'test_kanban_followup_stale_waiting_bootstrap_lock_revives_instead_of_reopening',
    'test_kanban_followup_bootstrap_gate_resume_reuses_run_and_artifact_loss_clears',
    'test_kanban_followup_stale_bootstrap_gate_resume_reactivates_lock',
    'test_kanban_followup_bootstrap_replay_claim_is_atomic_across_concurrent_stops',
    'test_kanban_followup_lock_without_bootstrap_gate_keeps_legacy_stale_consumption',
    'test_kanban_followup_bootstrap_waiting_is_bounded_by_reengage_and_revive_budgets',
    'test_kanban_followup_bootstrap_replay_classifies_artifact_loss_and_retries_after_timeout',
    'test_kanban_followup_bootstrap_reengage_budget_exhausted_diagnostic_splits_replay_pending',
    'test_accepted_review_run_receipt_suppresses_repeated_decision_point_injection',
    'test_rewound_acceptance_reconciles_receipt_via_decision_token_needle',
    'test_kanban_followup_awaiting_agent_decision_lock_skips_without_nudging',
    'test_kanban_followup_awaiting_agent_decision_window_expiry_returns_to_normal_gate',
    'test_kanban_followup_awaiting_lock_promoted_when_trigger_on_ancestor_chain',
    'test_kanban_followup_lock_reconciled_when_trigger_rewound_off_ancestor_chain',
    'test_kanban_followup_awaiting_window_expiry_releases_tracker_lease',
    'test_kanban_followup_rewind_reconcile_releases_tracker_lease',
    'test_kanban_followup_awaiting_promotion_write_failure_fails_closed_without_nudging',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def _in_progress_marker_name(repo: Path | str, *, task_id: str | None = None, session_id: str | None = None) -> str:
    """in-progress 锁 marker 的文件名——**现算，不硬编码**（key 含仓身份，见共享助手模块头）。"""
    return in_progress_lock_marker_name(
        load_kanban_followup_lock_module(),  # noqa: F821 — inject() 注入
        repo=repo,
        task_id=task_id,
        session_id=session_id,
    )


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

    diff_tracker = sys.modules["core.session_scope_allocation.reviewable_unit_diff_tracker"]
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
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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
    # F2a：这个 fake 只回遗留镜像 ``status: queued``。契约明说那是 mutate **之前**读的快照推断、
    # 与实际投递结果无关，故 RVF 一律判「尚未确认」而不是「已入队/已送达」——历史实现在这里
    # 报 enqueued 且**当作已确认**（不写 pending），正是 2026-08-08 事故的那条乐观推断。
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "kanban-followup-dispatched-unconfirmed"
    assert latest["kanban_terminal_delivery_receipt_field_source"] == "legacy_status_mirror"
    assert latest["kanban_followup_delivery_confirmed"] is False
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
    assert latest["parent_source_session_ref"] == "Codex 2026-05-01T11-25-17 019de191"
    assert latest["parent_source_session_name_source"] == "session_ref_fallback"
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
    assert "RVF_PARENT_SOURCE_SESSION_REF: Codex 2026-05-01T11-25-17 019de191" in prompt_text
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
    assert "RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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

    def _followup_dispatch_scenario(name: str, message_branch: str) -> tuple[dict, list[dict], Path]:
        """跑一次干净的 kanban-followup 主派发，`message` 子命令行为由 message_branch 决定。"""
        scenario_repo = init_repo_with_head(tmp_path / f"repo-{name}")
        scenario_state = tmp_path / f"state-{name}"
        scenario_tx = tmp_path / f"rollout-{name}.jsonl"
        write_apply_patch_transcript(scenario_tx, scenario_repo, session_id=session_id)
        scenario_client = tmp_path / f"fake_client_{name}.py"
        scenario_calls = tmp_path / f"client-calls-{name}.jsonl"
        scenario_client.write_text(
            "import json, os, sys\n"
            "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
            "if sys.argv[1] == 'list':\n"
            "    plant = os.environ.get('FAKE_PLANT_PENDING_PATH')\n"
            "    if plant:\n"
            "        os.makedirs(os.path.dirname(plant), exist_ok=True)\n"
            "        with open(plant, 'w', encoding='utf-8') as handle:\n"
            "            handle.write(json.dumps({\n"
            "                'marker_version': 1,\n"
            "                'state': 'dispatched_unconfirmed',\n"
            "                'dispatched_at': '2026-05-21T15:57:55Z',\n"
            "                'expires_at': '2999-01-01T00:00:00Z',\n"
            "                'kanban_task_id': 'task-77',\n"
            "                'token': 'concurrenttoken0',\n"
            "                'delivery_channel': 'terminal',\n"
            "            }))\n"
            f"    print(json.dumps({{'ok': True, 'tasks': [{{'id': 'task-77', 'title': 'T', 'workspacePath': {str(scenario_repo)!r}}}]}}))\n"
            "elif sys.argv[1] == 'message':\n"
            f"{message_branch}\n"
            "else:\n"
            "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
            encoding="utf-8",
        )
        env = {
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(scenario_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(scenario_repo),
            "FAKE_CLIENT_CALLS": str(scenario_calls),
        }
        if name == "concurrent":
            env["FAKE_PLANT_PENDING_PATH"] = str(
                scenario_state
                / "kanban-followup-in-progress"
                / "kanban-followup-dispatched"
                / "task-task-77.json"
            )
        invoke(
            {
                "cwd": str(scenario_repo),
                "stop_hook_active": False,
                "session_id": session_id,
                "transcript_path": str(scenario_tx),
            },
            extra_env=env,
            state_dir=scenario_state,
        )
        calls = [
            json.loads(line)
            for line in scenario_calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return latest_summary(scenario_state), calls, scenario_state

    # --- RVF-011：delivery_confirmed 也必须写 pending marker ---------------------------
    # 不写就等于：prompt 提交后 UPS 尚未 arm 就被中断 / rewind 时，既没有 in-progress 锁、也没有
    # 带 TTL 的 pending ⇒「delivered 但从未成为 turn ⇒ 到期撤回」那道保护永远不会触发，
    # 那轮 review 静默搁浅且看上去一切正常。（``delivered_queued_behind_active_agent_turn``
    # 本来就只是排上了队，更是如此。）
    confirmed_latest, _confirmed_calls, _ = _followup_dispatch_scenario(
        "confirmed",
        "    print(json.dumps({'ok': True, 'task_id': 'task-77', 'message_id': "
        "'terminal:task-77:rvf-run', 'terminal_delivery_status': "
        "'delivered_and_submit_confirmed'}))",
    )
    assert confirmed_latest["status"] == "kanban-followup-started"
    assert confirmed_latest["kanban_followup_delivery_confirmed"] is True
    confirmed_pending = confirmed_latest.get("kanban_followup_pending_marker_path")
    assert isinstance(confirmed_pending, str) and Path(confirmed_pending).exists(), (
        "已确认送达也要留 pending：UPS arm 之前被中断 / rewind 时，它是唯一能到期收口的记录"
    )
    confirmed_marker = json.loads(Path(confirmed_pending).read_text(encoding="utf-8"))
    assert confirmed_marker["token"] == dispatch_prep_payload(confirmed_latest)["token"]

    # --- RVF-010：`kanban task message` 退出码 4（硬超时、空 stdout）= 「还没结论」 --------
    # 它**不是**投递失败：那条消息可能已被受理。走 kanban-followup-unavailable 且不写 pending
    # 会让它失去 status 对账与撤回能力，并可迟到执行。
    verdict_latest, verdict_calls, _ = _followup_dispatch_scenario(
        "verdict",
        "    print('cline-kanban verdict not yet available: hard timeout', file=sys.stderr)\n"
        "    raise SystemExit(4)",
    )
    assert [c["argv"][0] for c in verdict_calls][-1] == "message"
    assert verdict_latest["status"] == "kanban-followup-dispatched-unconfirmed"
    assert verdict_latest["kanban_terminal_delivery_verdict_not_yet_available"] is True
    verdict_pending_path = verdict_latest.get("kanban_followup_pending_marker_path")
    assert isinstance(verdict_pending_path, str) and Path(verdict_pending_path).exists(), (
        "「还没结论」必须留下 pending，否则一条可能已被受理的消息再也无法对账 / 撤回"
    )
    verdict_marker = json.loads(Path(verdict_pending_path).read_text(encoding="utf-8"))
    # 没有这枚 key 就查不到真实终态，对账只能退回 TTL 猜测。
    assert verdict_marker["idempotency_key"]
    assert verdict_marker["token"] == dispatch_prep_payload(verdict_latest)["token"]

    # --- RVF-012：delivery_failed 刻意不写 pending，但必须释放本轮的 tracker lease --------
    # 不放掉，scope unit 会在整个 lease TTL（默认约 1h）里保持 assigned，而 hook payload 却承诺
    # 「下一次该 task 的 Stop 会按当时的工作树重新决策」—— 那次 Stop 根本拿不到这批单元。
    failed_latest, _failed_calls, failed_state = _followup_dispatch_scenario(
        "failed",
        "    print(json.dumps({'ok': True, 'task_id': 'task-77', 'message_id': "
        "'terminal:task-77:rvf-run', 'terminal_delivery_status': 'delivery_failed', "
        "'terminal_delivery_failure_reason': 'terminal_prompt_readiness_timeout'}))",
    )
    assert failed_latest["status"] == "kanban-followup-delivery-failed"
    assert failed_latest.get("kanban_followup_pending_marker_path") is None
    released = [
        event
        for event in latest_events(failed_state)
        if event.get("event") == "kanban_followup_delivery_failed_lease_released"
    ]
    assert len(released) == 1
    assert released[0]["tracker_lease_id"]
    assert released[0]["tracker_lease_release"].get("released") is True

    # --- RVF-009：单飞闸必须裹到 pending 落盘，并在闸内复检决策是否已过时 -----------------
    # 两个 Stop 可以都在闸外读到「无 pending」，再一前一后进闸各投一条新 key ⇒ 先到的那条被判
    # superseded_by_later_delivery。这里让 fake client 在 `list`（闸前）阶段种下一条 active
    # pending，模拟「闸外判据已过时」，断言闸内复检把本轮拦下、`message` 根本没被调用。
    concurrent_latest, concurrent_calls, _ = _followup_dispatch_scenario(
        "concurrent",
        "    print(json.dumps({'ok': True, 'task_id': 'task-77', 'message_id': "
        "'terminal:task-77:should-not-dispatch'}))",
    )
    assert concurrent_latest["status"] == "skipped"
    assert (
        concurrent_latest["reason_code"]
        == "kanban_followup_dispatch_superseded_by_concurrent_pending"
    )
    assert "message" not in [c["argv"][0] for c in concurrent_calls]


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


def _load_stop_hook_module_in_process(alias: str):
    """把 Stop hook 脚本以独立别名 in-process 载入，供可 monkeypatch 的白盒断言使用。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(alias, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _exercise_kanban_followup_withdrawal_receipt_and_reconciliation(tmp_path: Path) -> None:
    """撤回本身的**可执行性 + 诚实性**，以及 pending 对账是否真的采纳撤回结论。

    ⚠️ 本段刻意挂在一个**已注册**的测试里跑：同簇的
    ``test_kanban_followup_pending_reconciliation_never_waits_without_an_upper_bound``
    虽然存在，却**不在** ``tests/test_stop_review_validate_fix.py`` 的注册表里（protected 文件，
    本包不得修改），挂上去等于静默不跑 = 假绿。

    覆盖四条：

    - **RVF-001**：撤回的「message-cancel 成功」路径（``try/except/**else**``）必须真的能跑完。
      该分支里两个 ``cancel_receipt_indicates_*`` 判据曾**没被 import**，而 ``else`` 里抛的
      NameError **不会**被前面的 except 捕获 ⇒ 释放 lease、清锁、清 pending 三步全部跳过、
      撤回半途而废；``py_compile`` 与既有测试都看不见它。
    - **RVF-002**：既没抢到 tombstone（机制此刻不可用）、``message-cancel`` 也失败/不存在时，
      回执**不得**声称 ``withdrawn=True`` —— 此刻既无物理取消也无语义中和。
    - **RVF-006**：三条 pending 对账分支都必须**读**撤回回执；撤回说「别放行」时不得 ``return None``
      让正常 launch 过去（原 review 已被 UPS 接手时那就是第二条 follow-up）。
    - **RVF-008①**：``delivery_failed`` 必须按契约的 ``redispatch_disposition`` 分流，
      ``wait_for_external_condition`` / ``requires_human_confirmation`` 不得自动重投。
    """
    cs = _load_stop_hook_module_in_process("cs_withdrawal_under_test")
    real_withdraw = cs._withdraw_unconfirmed_kanban_followup_dispatch

    events: list[dict] = []

    class _FakeLedger:
        run_id = "run-withdrawal"
        run_dir = str(tmp_path / "rd")

        def event(self, **kwargs):
            events.append(kwargs)

        def hook_payload(self, **kwargs):
            return {"continue": True, **kwargs}

    # --- RVF-001：真撤回，走「message-cancel 成功」那条 else 分支 ------------------------
    # 铁律：落盘根全部指向 tmp（tombstone 走 RVF_HOME，锁 / pending 走 LOCK_ROOT），绝不写真实 ~/.rvf。
    isolated_home = tmp_path / "withdraw-rvf-home"
    lock_root = tmp_path / "withdraw-kanban-followup-in-progress"
    (lock_root / "kanban-followup-dispatched").mkdir(parents=True, exist_ok=True)
    saved_env = {
        name: os.environ.get(name)
        for name in (
            "RVF_HOME",
            "RVF_WITHDRAWN_DISPATCH_TOMBSTONE_ROOT",
            "RVF_KANBAN_FOLLOWUP_LOCK_ROOT",
        )
    }
    os.environ["RVF_HOME"] = str(isolated_home)
    os.environ["RVF_WITHDRAWN_DISPATCH_TOMBSTONE_ROOT"] = str(
        isolated_home / "rvf-withdrawn-dispatch-tokens"
    )
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(lock_root)
    try:
        cancel_calls: list[dict] = []

        def _fake_cancel_success(**kw):
            cancel_calls.append(kw)
            # 契约：判「是不是被取消的」读 terminal_delivery_failure_reason，不读 cancel_result。
            return {
                "terminal_delivery_status": "delivery_failed",
                "terminal_delivery_failure_reason": "cancelled_before_delivery",
                "cancel_result": "cancelled_before_delivery",
            }

        cs.cancel_task_message_delivery = _fake_cancel_success
        withdraw_marker = {
            "kanban_task_id": "taskCancelOk",
            "kanban_project_path": str(tmp_path),
            "idempotency_key": "idem-cancel-ok",
            "token": "abcdef0123456789",
            "run_dir": str(tmp_path / "origin-run"),
        }
        result = real_withdraw(withdraw_marker, reason="prior_dispatch_never_converged")
        assert cancel_calls, "cancel 成功路径必须真的调用过 message-cancel"
        assert result["physical_cancel"]["outcome"] == "completed"
        # 两个判据必须都被求值（漏 import 时它们根本走不到，NameError 会把整段撤回炸掉）。
        assert result["physical_cancel"]["message_already_reached_agent"] is False
        assert result["physical_cancel"]["cancelled_rather_than_failed_for_another_reason"] is True
        # 撤回的后两步（放 lease、清锁、清 pending）必须真的跑到，而不是被异常跳过。
        assert "tracker_lease_release" in result
        assert "removed_in_progress_marker_paths" in result
        assert "removed_pending_marker_paths" in result
        assert result["withdrawn"] is True
        assert result.get("cleaned_up_without_neutralization") is not True

        # --- RVF-002：tombstone 不可用 + 取消也用不了 ⇒ 回执不得谎报 withdrawn -----------
        def _fake_cancel_unavailable(**kw):
            raise cs.KanbanTerminalDeliveryInterfaceUnavailableError("no such subcommand")

        cs.cancel_task_message_delivery = _fake_cancel_unavailable
        # token 形状非法 ⇒ tombstone_path 返回 None ⇒ claim.applicable=False（机制此刻用不了）。
        unusable_marker = dict(withdraw_marker)
        unusable_marker["kanban_task_id"] = "taskNoNeutralize"
        unusable_marker["token"] = "not-a-valid-hex-token!"
        degraded = real_withdraw(unusable_marker, reason="prior_dispatch_unconfirmed_ttl_elapsed")
        assert degraded["tombstone"]["applicable"] is False
        assert degraded["physical_cancel"]["outcome"] == "interface_unavailable"
        assert degraded["neutralization"] == cs.KANBAN_FOLLOWUP_DISPATCH_NOT_NEUTRALIZED
        assert degraded["withdrawn"] is False, "既无物理取消也无语义中和时不得声称已撤回"
        assert degraded["cleaned_up_without_neutralization"] is True
        # 清理仍然照做（不清理 = 这一轮永久 park，比多一轮延迟坏得多）。
        assert "tracker_lease_release" in degraded
        assert "removed_pending_marker_paths" in degraded

        # --- token 终态已是 consumed：不动 lease / 锁，但必须清掉那条作废的 pending -------
        # 留着它，此后每一次 Stop 都会重走这条路径并被拦下，直到 tombstone TTL（6h）过期才自解。
        # （「已确认送达也写 pending」之后这条路径才真的会被走到：UPS 可能在 dispatch 侧落盘
        # pending **之前**就已 arm 并清过一次，那条 pending 从诞生起就是孤儿。）
        consumed_token = "c0ffee0123456789"
        cs.rvf_withdrawn_dispatch_tombstone.claim_dispatch_token_terminal_state(
            consumed_token,
            state=cs.rvf_withdrawn_dispatch_tombstone.STATE_CONSUMED,
            reason="test_ups_delivery_landed",
        )
        pending_dir = lock_root / "kanban-followup-dispatched"
        orphan_pending = pending_dir / "task-taskConsumed.json"
        orphan_pending.write_text(
            json.dumps(
                {
                    "marker_version": 1,
                    "state": "dispatched_unconfirmed",
                    "kanban_task_id": "taskConsumed",
                    "token": consumed_token,
                    "expires_at": "2000-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        in_progress_lock = lock_root / "task-taskConsumed.json"
        in_progress_lock.write_text(json.dumps({"state": "in_progress"}), encoding="utf-8")
        consumed_marker = {
            "kanban_task_id": "taskConsumed",
            "kanban_project_path": str(tmp_path),
            "idempotency_key": "idem-consumed",
            "token": consumed_token,
            "run_dir": str(tmp_path / "origin-run"),
        }
        consumed_result = real_withdraw(consumed_marker, reason="prior_dispatch_never_converged")
        assert consumed_result["withdrawn"] is False
        assert consumed_result["skipped_because"] == "dispatch_token_already_consumed"
        assert not orphan_pending.exists(), "consumed 之后那条 pending 已作废，必须清掉"
        # 正在跑的那轮 review 的锁与 lease 绝不能被动。
        assert in_progress_lock.exists()
        assert "tracker_lease_release" not in consumed_result
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # --- RVF-006：撤回说「别放行」时，三条对账分支都不得 return None ---------------------
    marker = {
        "kanban_task_id": "taskB",
        "kanban_project_path": str(tmp_path),
        "idempotency_key": "idem-1",
        "token": "tok0000000000000",
        "run_dir": str(tmp_path / "origin-run"),
        "_marker_path": str(tmp_path / "pending.json"),
    }
    cs.read_kanban_followup_pending = lambda **kw: dict(marker)
    cs.kanban_followup_pending_status = lambda *a, **kw: "stale"
    cs._withdraw_unconfirmed_kanban_followup_dispatch = lambda m, *, reason: {
        "withdrawn": False,
        "neutralization": cs.KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED,
        "skipped_because": "dispatch_token_already_consumed",
    }
    delivered_receipt = cs.classify_cline_kanban_terminal_delivery_receipt(
        {"terminal_delivery_status": "delivered_and_submit_confirmed"}
    )
    failed_receipt = cs.classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": "delivery_failed",
            "terminal_delivery_failure_reason": "terminal_prompt_readiness_timeout",
        }
    )
    for label, query in (
        ("delivery_failed", lambda m: (failed_receipt, None)),
        ("never_converged", lambda m: (delivered_receipt, None)),
        ("ttl_degraded", lambda m: (None, "interface_unavailable")),
    ):
        cs._query_kanban_terminal_delivery_receipt = query
        decision = cs._kanban_followup_pending_decision(
            {}, _FakeLedger(), task_id="taskB", session_id="s", cwd=str(tmp_path)
        )
        assert decision is not None, f"{label} 分支必须采纳撤回回执，不能放行第二条 follow-up"
        assert decision.reason_code == "kanban_followup_prior_dispatch_already_consumed"

    # --- RVF-008①：delivery_failed 按契约处置分流 --------------------------------------
    cs._withdraw_unconfirmed_kanban_followup_dispatch = lambda m, *, reason: {
        "withdrawn": True,
        "neutralization": cs.KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED,
    }
    withheld_reasons = (
        # wait_for_external_condition：会话没起来 / 人正在输入框打字 / agent 等用户答完。
        "no_active_terminal_session",
        "human_terminal_contention_timeout",
        "agent_awaiting_user_decision_timeout",
        # requires_human_confirmation：文本可能仍残留在输入框里。
        "submit_confirmation_budget_exhausted",
    )
    for failure_reason in withheld_reasons:
        receipt = cs.classify_cline_kanban_terminal_delivery_receipt(
            {
                "terminal_delivery_status": "delivery_failed",
                "terminal_delivery_failure_reason": failure_reason,
            }
        )
        cs._query_kanban_terminal_delivery_receipt = lambda m, _r=receipt: (_r, None)
        decision = cs._kanban_followup_pending_decision(
            {}, _FakeLedger(), task_id="taskB", session_id="s", cwd=str(tmp_path)
        )
        assert decision is not None, f"{failure_reason} 不得被自动重投"
        assert decision.reason_code == "kanban_followup_redispatch_disposition_withheld"
    # 「换新 key 就能干净重投」这一种仍然放行（主派发本就每轮换新 key）。
    cs._query_kanban_terminal_delivery_receipt = lambda m: (failed_receipt, None)
    assert (
        cs._kanban_followup_pending_decision(
            {}, _FakeLedger(), task_id="taskB", session_id="s", cwd=str(tmp_path)
        )
        is None
    )


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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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

    # 同一条对账链路的白盒面：撤回是否真的能跑完（RVF-001）、回执是否诚实（RVF-002）、
    # 以及三条对账分支是否真的采纳撤回结论 / 按契约处置分流（RVF-006 / RVF-008①）。
    _exercise_kanban_followup_withdrawal_receipt_and_reconciliation(tmp_path / "withdrawal")


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
        "RVF_PROVIDER_HEALTH_CHECK": "0",
        "RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
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
        "RVF_PROVIDER_HEALTH_CHECK": "0",
        "RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
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


def _exercise_stranded_redispatch_neutralization_and_disposition(tmp_path: Path) -> None:
    """S2 重投的可达路径：旧 token 必须先被中和，且中和 / 投递 / 落盘全在 per-task 派发闸内。

    ⚠️ 本段刻意挂在一个**已注册**的测试里跑：同簇那个 in-process 的 S2 测试
    （``test_kanban_followup_s2_redispatch_binds_idempotency_key_to_fresh_token_and_preserves_prompt_path``）
    在 protected 注册表里仍写着一个**早已改名、如今解析不到**的旧名字，本包不得修改该文件，
    挂上去等于静默不跑 = 假绿。

    覆盖四条：

    - **RVF-007**：重投前从不为旧 token 写 withdrawn tombstone、也不调 ``message-cancel``
      ⇒ 旧消息仍可迟到落地并与新一轮并发。同 task 对账路径明明会走撤回，这条路径把整套止血绕过。
      旧 token 已被 UPS 接手（终态 ``consumed``）时更必须收手，否则就是第二条 follow-up。
    - **RVF-009（重投侧）**：闸只裹住投递、放开后才写 pending ⇒ 另一个 Stop 能就地进闸再投一条
      新 key，先到的那条被判 ``superseded_by_later_delivery``。
    - **RVF-010（重投侧）**：``kanban task message`` 退出码 4 = 「还没结论」而非失败；当普通失败
      退出会把 pending 留在**旧** token 上（prompt 里却已是新 token）⇒ UPS 落地时防误清 guard
      直接拒绝 ⇒ pending 永久锁死。
    - **RVF-008②**：``redispatch_not_needed`` 与「换新 key 可重投」并列是反语义的 ——
      ``superseded_by_later_delivery`` / ``cancelled_before_delivery`` 会被再投，
      还可能把后一条合法投递撞成 superseded。
    """
    import contextlib as _contextlib

    tmp_path.mkdir(parents=True, exist_ok=True)
    cs = _load_stop_hook_module_in_process("cs_s2_neutralize_under_test")

    old_prompt = tmp_path / "old-prompt.md"
    old_prompt.write_text(
        "$review-validate-fix\n\nRVF dispatch prep file:\nRVF_DISPATCH=token=oldoldoldoldoldo\n",
        encoding="utf-8",
    )

    class _FakePrep:
        token = "freshtoken000000"
        path = tmp_path / "fresh-prep.json"

    class _FakeLedger:
        run_id = "run-redispatch-neutralize"
        run_dir = str(tmp_path / "rd")

    recorded: dict[str, dict] = {}
    neutralized: list[dict] = []
    inside_gate = {"open": False}
    saw_gate: dict[str, object] = {"neutralize": None, "start": None, "pending": None}

    cs.select_existing_app_server_socket_for_metadata = lambda: (tmp_path / "sock", "explicit", {})
    cs.can_connect_app_server_socket = lambda p: True
    cs.write_dispatch_prep_file = lambda **kw: _FakePrep()
    cs.clear_kanban_followup_pending = lambda **kw: []

    @_contextlib.contextmanager
    def _tracking_single_flight(task_id_arg):
        inside_gate["open"] = True
        try:
            yield True
        finally:
            inside_gate["open"] = False

    cs.kanban_followup_dispatch_single_flight_for_task = _tracking_single_flight

    def _fake_neutralize(marker_arg, *, reason):
        saw_gate["neutralize"] = inside_gate["open"]
        neutralized.append({"token": marker_arg.get("token"), "reason": reason})
        return {"neutralization": cs.KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED}

    cs._neutralize_kanban_followup_dispatch_token = _fake_neutralize

    def _fake_start(**kw):
        saw_gate["start"] = inside_gate["open"]
        recorded["start"] = kw
        return {"message_id": "terminal:taskX:redispatch", "prompt_path": "/new/p.md"}

    cs.start_cline_kanban_followup_message = _fake_start

    def _fake_write_pending(**kw):
        saw_gate["pending"] = inside_gate["open"]
        recorded["pending"] = kw
        return tmp_path / "pending.json"

    cs.write_kanban_followup_pending = _fake_write_pending

    marker = {
        "kanban_task_id": "taskX",
        "kanban_project_path": "/repo",
        "prompt_path": str(old_prompt),
        "session_id": "sX",
        "repo": "/repo",
        "cwd": "/repo",
        "run_dir": str(tmp_path / "origin-run"),
        "kanban_attempt_id": "att",
        "token": "oldoldoldoldoldo",
    }

    def _redispatch():
        saved = os.environ.get("RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH")
        os.environ["RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = "1"
        try:
            return cs._maybe_redispatch_stranded_kanban_followup(
                marker, _FakeLedger(), token="oldoldoldoldoldo"
            )
        finally:
            if saved is None:
                os.environ.pop("RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH", None)
            else:
                os.environ["RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = saved

    # --- RVF-007 + RVF-009（重投侧） --------------------------------------------------
    result = _redispatch()
    assert result.get("redispatched") is True
    assert [n["token"] for n in neutralized] == ["oldoldoldoldoldo"], "旧 token 必须被中和一次"
    assert saw_gate == {"neutralize": True, "start": True, "pending": True}

    # 旧 token 已被 UPS 接手（tombstone 终态 consumed）⇒ 重投等于投出第二条，必须收手。
    cs._neutralize_kanban_followup_dispatch_token = lambda m, *, reason: {
        "neutralization": cs.KANBAN_FOLLOWUP_DISPATCH_ALREADY_CONSUMED
    }
    started_before = recorded.get("start")
    consumed_result = _redispatch()
    assert consumed_result.get("redispatched") is False
    assert consumed_result.get("reason") == "prior-dispatch-already-consumed"
    assert recorded.get("start") is started_before, "已被接手时不得再投"

    # --- RVF-010（重投侧）：退出码 4 = 「还没结论」，pending 必须改写到新 token ----------
    cs._neutralize_kanban_followup_dispatch_token = lambda m, *, reason: {
        "neutralization": cs.KANBAN_FOLLOWUP_DISPATCH_NEUTRALIZED
    }

    def _fake_start_verdict_pending(**kw):
        raise cs.KanbanFollowupDeliveryVerdictNotYetAvailableError(
            "hard timeout",
            task_id="taskX",
            idempotency_key=kw["idempotency_key"],
            attempt_id=kw.get("attempt_id"),
            prompt_path="/new/p3.md",
            command_artifact_path=None,
            project_path="/repo",
            task_cmd="fake task",
        )

    cs.start_cline_kanban_followup_message = _fake_start_verdict_pending
    recorded.pop("pending", None)
    verdict_result = _redispatch()
    assert verdict_result.get("redispatched") is True
    assert verdict_result.get("terminal_delivery_verdict_not_yet_available") is True
    verdict_pending = recorded["pending"]
    assert verdict_pending["token"] == "freshtoken000000", "pending 必须改写到新 token"
    assert verdict_pending["idempotency_key"] == verdict_result["idempotency_key"]
    # 没拿到结论 ⇒ 回执判读必须落 awaiting，绝不伪造送达。
    assert verdict_pending["terminal_delivery_status"] is None

    # --- RVF-008② ---------------------------------------------------------------------
    for failure_reason, expected_reason in (
        ("superseded_by_later_delivery", "redispatch-not-needed"),
        ("cancelled_before_delivery", "redispatch-not-needed"),
        ("no_active_terminal_session", "redispatch-disposition-withheld"),
    ):
        receipt = cs.classify_cline_kanban_terminal_delivery_receipt(
            {
                "terminal_delivery_status": "delivery_failed",
                "terminal_delivery_failure_reason": failure_reason,
            }
        )
        cs._query_kanban_terminal_delivery_receipt = lambda m, _r=receipt: (_r, None)
        withheld = _redispatch()
        assert withheld.get("redispatched") is False, f"{failure_reason} 不得自动重投"
        assert withheld.get("reason") == expected_reason


def test_kanban_followup_stranded_sweep_redispatch_enabled_but_unreachable(tmp_path: Path) -> None:
    """S2（RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH=1）：app-server 不可达 → 诚实放弃重投，
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
        "RVF_PROVIDER_HEALTH_CHECK": "0",
        "RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
        "NOTIFY_LOG": str(notify_log),
        "RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH": "1",
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

    # --- RVF-014：条数上界（默认 8）管不住时间，sweep 必须另有总预算 --------------------
    # 开启自动重投后每条 pending 都要多做一次终态查询（默认 timeout 5s）⇒ 仅这一项最坏就是
    # 8×5=40s，还没算 task-list（10s）、通知（10s）与重投子进程（20s），已越过 Stop 链路
    # 约 30s 的总预算。预算用尽必须**停止扫荡**（marker 全保留，下一次 Stop 接着扫），
    # 而不是继续往下跑。
    budget_state = tmp_path / "state-budget"
    budget_pending_dir = (
        budget_state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
    )
    budget_pending_dir.mkdir(parents=True)
    budget_a = _write_stranded_pending(
        budget_pending_dir, task_id="taskBudgetA", token="1111111111111111",
        project_path=str(repo), prompt_path=str(old_prompt),
    )
    budget_b = _write_stranded_pending(
        budget_pending_dir, task_id="taskBudgetB", token="2222222222222222",
        project_path=str(repo), prompt_path=str(old_prompt),
    )
    invoke(
        {"cwd": str(repo), "stop_hook_active": False},
        extra_env={**extra, "RVF_KANBAN_FOLLOWUP_STRANDED_SWEEP_BUDGET_SECONDS": "1e-9"},
        state_dir=budget_state,
    )
    budget_events = latest_events(budget_state)
    assert any(
        event.get("event") == "kanban_followup_stranded_sweep_budget_exhausted"
        for event in budget_events
    ), "sweep 必须有一道总挂钟预算闸，条数上界不构成时间上界"
    assert not any(
        event.get("event") == "kanban_followup_pending_stranded_escalated"
        for event in budget_events
    )
    # 停止扫荡不丢事：marker 全部保留。
    assert budget_a.exists() and budget_b.exists()

    # 同一条重投链路的白盒面：旧 token 的中和、闸内落盘、退出码 4 的处置与契约处置分流
    # （RVF-007 / RVF-009 重投侧 / RVF-010 重投侧 / RVF-008②）。
    _exercise_stranded_redispatch_neutralization_and_disposition(tmp_path / "s2-neutralize")


def test_kanban_followup_s2_redispatch_binds_idempotency_key_to_fresh_token_and_preserves_prompt_path(tmp_path: Path) -> None:
    """S2 重投可达路径回归：in-process 调用 _maybe_redispatch_*，monkeypatch app-server 可达 +
    假派发，断言——

      **（F2a 改写）idempotency_key 必须绑定本次新铸的 fresh prep token。** 本测试原先断言的是
      反面（绑定稳定的 stranded token），那条判据在 cline-kanban 的终端投递契约下已被证伪：
        - 同 key + 同 prompt hash ⇒ 契约「直接返回当前记录、**不重投**」⇒ 稳定键根本投不出去；
        - 每次重投都会铸新 token 并写进 prompt ⇒ prompt hash 变了 ⇒ 契约判**幂等冲突、退出码 1**
          ⇒ 这条 stranded marker 从此**永久**重投失败。
      稳定键当初承担的「别并发投同一个 task」责任，现由 per-task 派发单飞闸承担（契约第 5 条）。

      RVF-002（仍有效）：terminal 未确认重投改写 pending 必须带上新派发的 prompt_path/turn_id
      （否则下次重投 missing-prompt-path）。

    e2e 路径需 websocket-协议假 socket（过重），故此处走 in-process 单测直接锁住这两条。"""
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
    saved = os.environ.get("RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH")
    os.environ["RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = "1"
    try:
        result = cs._maybe_redispatch_stranded_kanban_followup(
            marker, _FakeLedger(), token="stabletoken00000"
        )
    finally:
        if saved is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_AUTO_REDISPATCH"] = saved

    assert result.get("redispatched") is True
    # F2a：idem 绑定**本次新铸**的 token，与 prompt 正文里的 token 一一对应 ⇒ 同 key 永远配同
    # 一份 prompt hash ⇒ 永不触发契约的幂等冲突。绑旧的 stranded token 才是那条死路。
    idem = recorded["start"]["idempotency_key"]
    assert idem == f"rvf-redispatch-{cs.safe_token('taskX')}-freshtoken000000"
    assert "stabletoken00000" not in idem
    # 重投子进程带 timeout（有界）。
    assert recorded["start"].get("timeout")
    # RVF-002：terminal 未确认重投改写 pending 带新 prompt_path/turn_id + 新 token。
    pending = recorded["pending"]
    assert pending["prompt_path"] == "/new/redispatch-prompt.md"
    assert pending["turn_id"] == "7"
    assert pending["token"] == "freshtoken000000"
    # F2a：重投次数必须显式记账——换新 key 之后，「重投了几次」再也无法从 key 反推。
    assert pending["redispatch_attempt_count"] == 1
    assert pending["idempotency_key"] == idem


def test_kanban_followup_pending_reconciliation_never_waits_without_an_upper_bound(
    tmp_path: Path,
) -> None:
    """pending 对账的每一条「继续等」分支都必须以 pending 自己的 TTL 为上界。

    没有这条上界，一条「cline-kanban 说已送达、但 UPS 永远不会 arm」的 pending
    （prompt 落地后 agent 被中断，或用户 rewind 掉了那一回合）会让**每一次** Stop 都查到
    ``delivered``、每一次都 skip —— 那轮 review 就此永久静默搁浅，而且看上去一切正常。
    这是「先查真实终态」这条改进自带的新风险，故单独钉死。

    走 in-process 单测：桩掉终态查询与撤回执行，直接驱动 ``_kanban_followup_pending_decision``。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    import importlib.util

    spec = importlib.util.spec_from_file_location("cs_pending_bound_under_test", SCRIPT)
    cs = importlib.util.module_from_spec(spec)
    sys.modules["cs_pending_bound_under_test"] = cs
    spec.loader.exec_module(cs)

    class _FakeLedger:
        run_id = "run-pending-bound"
        run_dir = str(tmp_path / "rd")

        def event(self, **kwargs):
            events.append(kwargs)

        def hook_payload(self, **kwargs):
            return {"continue": True, **kwargs}

    events: list[dict] = []
    withdrawals: list[dict] = []

    delivered_receipt = cs.classify_cline_kanban_terminal_delivery_receipt(
        {"terminal_delivery_status": "delivered_and_submit_confirmed"}
    )
    cs._query_kanban_terminal_delivery_receipt = lambda marker: (delivered_receipt, None)
    cs._withdraw_unconfirmed_kanban_followup_dispatch = lambda marker, *, reason: (
        withdrawals.append({"reason": reason}) or {"withdrawn": True, "reason": reason}
    )

    marker = {
        "kanban_task_id": "taskB",
        "kanban_project_path": str(tmp_path),
        "idempotency_key": "idem-1",
        "token": "tok0000000000000",
        "run_dir": str(tmp_path / "origin-run"),
        "_marker_path": str(tmp_path / "pending.json"),
    }

    # ① pending 仍在自己的 TTL 内 → 允许等（不重投、不撤回）。
    cs.read_kanban_followup_pending = lambda **kw: dict(marker)
    cs.kanban_followup_pending_status = lambda *a, **kw: cs.KANBAN_FOLLOWUP_LOCK_ACTIVE
    decision = cs._kanban_followup_pending_decision(
        {}, _FakeLedger(), task_id="taskB", session_id="s", cwd=str(tmp_path)
    )
    assert decision is not None, "TTL 内应当继续等待"
    assert withdrawals == []

    # ② pending 自己的 TTL 已过、UPS 仍未 arm → 必须收口：撤回并放行重新决策，
    #    绝不因为「回执说 delivered」而无限期等下去。
    cs.kanban_followup_pending_status = lambda *a, **kw: "stale"
    decision = cs._kanban_followup_pending_decision(
        {}, _FakeLedger(), task_id="taskB", session_id="s", cwd=str(tmp_path)
    )
    assert decision is None, "TTL 到期后必须放行重新决策，而不是继续 skip"
    assert [w["reason"] for w in withdrawals] == [
        "prior_dispatch_delivered_but_never_became_a_turn"
    ]


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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
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
            "RVF_PROVIDER_HEALTH_CHECK": "0",
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
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
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
                "RVF_PROVIDER_HEALTH_CHECK": "0",
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
    assert "reason=kanban_followup_dispatched_unconfirmed" in payload["systemMessage"]
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
    marker_path = marker_dir / _in_progress_marker_name(dirty, task_id="task-active")
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
    marker_path = marker_dir / _in_progress_marker_name(dirty, task_id="task-active")
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
    # force-continue 契约：受众 agent_must_act_now，在**未标明 host** 的事件上兜底 Codex 通道
    # ⇒ 恰好 {"decision":"block","reason":…} + 一条 developer-only 的 systemMessage——既非 skip 的
    # {"continue":…}，也非 launch 的 fork payload（证明没经常规 gate 重派新 run）。
    # systemMessage 是**必需项**而非装饰：没有它，开发者终端里这条刻意注入与「hook 真崩了」
    # 完全无法区分（历史上 90/92 条阻塞注入正是这样）。
    assert set(payload.keys()) == {"decision", "reason", "systemMessage"}, payload
    assert payload["decision"] == "block"
    assert "刻意注入" in payload["systemMessage"], payload["systemMessage"]
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
    marker_path = marker_dir / _in_progress_marker_name(dirty, task_id="task-active")
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
    # 锁 key 含仓身份，故 arm/read 都要给一个**真实** git 仓做锚点（否则 marker_paths 空 → 不落锁）。
    repo = init_repo(tmp_path / "repo", dirty=True)
    arm_kwargs = dict(
        task_id="task-rearm",
        session_id="session-rearm",
        repo=str(repo),
        cwd=None,
        root=root,
    )
    read_kwargs = dict(task_id="task-rearm", session_id="session-rearm", repo=str(repo), root=root)

    # 首次 arm：无既有 marker → 计数缺省 0（不应被 carry 逻辑误置）。
    first = module.write_marker(run_id="rvf-1", run_dir=str(tmp_path / "run-1"), **arm_kwargs)
    assert first is not None
    marker_after_first = module.read_marker(**read_kwargs)
    assert module.reengage_nudge_count(marker_after_first) == 0

    # 模拟 Stop 读侧的 nudge 记账：bump 两次 → 计数 2，写在实际命中文件上。
    assert module.bump_reengage_nudge_count(marker_after_first) == 1
    assert (
        module.bump_reengage_nudge_count(module.read_marker(**read_kwargs))
        == 2
    )

    # re-arm（新 run，整份覆盖）：run_id 必须换成 rvf-2（证明是真覆盖而非 no-op），
    # 且 reengage_nudge_count 必须保留为 2（Option A 的核心：计数跨 re-arm 存活）。
    second = module.write_marker(run_id="rvf-2", run_dir=str(tmp_path / "run-2"), **arm_kwargs)
    assert second is not None
    marker_after_rearm = module.read_marker(**read_kwargs)
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
    marker_path = marker_dir / _in_progress_marker_name(dirty, task_id="task-active")
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
    # 锁 key 含仓身份 → 锚点必须是真实 git 仓；后面 acquire_marker 也用同一个仓，两侧同 key。
    repo = init_repo(tmp_path / "repo", dirty=True)
    marker_path = module.marker_paths(
        task_id="task-race", session_id=None, repo=str(repo), root=root
    )[0]
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

    def racing_read_marker(
        *, task_id: str | None, session_id: str | None, repo=None, root: Path | None = None
    ):
        nonlocal swapped_to_active
        marker = original_read_marker(
            task_id=task_id, session_id=session_id, repo=repo, root=root
        )
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
            repo=str(repo),
            cwd=str(repo),
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
    # RVF_KANBAN_FOLLOWUP_LOCK_ROOT 下，文件名按 task_id + 仓身份）。
    marker_path = shared_lock_root / _in_progress_marker_name(repo, task_id="task-shared")
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
            "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(shared_lock_root),
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
    # 将经 RVF_KANBAN_FOLLOWUP_LOCK_ROOT 读到的同一基目录）。
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
            "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(awaiting_root),
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



# ---------------------------------------------------------------------------
# pre-review local-base bootstrap 等待态的 Stop 读侧（入口 B）
#
# 这一族守的是「锁上挂着 bootstrap 等待 gate」时的三条分支：ACTIVE 时不得发通用「去收尾
# handoff」提示、STALE 时不得清锁重开（32cad 家族）、receipt 有效时按**原 run** 恢复且不
# 产生第二把锁 / 第二轮 RVF。
# ---------------------------------------------------------------------------

def load_bootstrap_gate_module():
    """加载 Stop hook 已 top-level import 的 rvf_bootstrap_gate（与锁模块同构的取法）。"""
    load_hook_module()
    module = sys.modules.get("rvf_bootstrap_gate")
    assert module is not None
    return module


def _git_stdout(args: list[str], cwd: Path) -> str:
    """取 git stdout。本簇注入的 ``run`` 只跑不取输出（返回 None），故 bootstrap fixture 自带一个。"""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _bootstrap_opt_in_task_repo(path: Path) -> dict[str, object]:
    """建一个「local base 声明了 pre-review 同步 marker、task 尚未吸收」的真实仓库。"""
    repo = init_repo(path, dirty=False)
    run(["git", "config", "user.email", "rvf@example.test"], repo)
    run(["git", "config", "user.name", "RVF Test"], repo)
    (repo / "changed.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "changed.txt"], repo)
    run(["git", "commit", "-q", "-m", "base"], repo)
    task_start = _git_stdout(["rev-parse", "HEAD"], repo)
    run(["git", "branch", "-M", "main"], repo)
    (repo / "AGENTS.md").write_text(
        "RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n",
        encoding="utf-8",
    )
    run(["git", "add", "AGENTS.md"], repo)
    run(["git", "commit", "-q", "-m", "base enables pre-review sync"], repo)
    local_base_tip = _git_stdout(["rev-parse", "HEAD"], repo)
    run(["git", "checkout", "-q", "--detach", task_start], repo)
    (repo / "changed.txt").write_text("task change\n", encoding="utf-8")
    return {
        "repo": repo,
        "task_start": task_start,
        "local_base_ref": "main",
        "local_base_tip": local_base_tip,
    }


def _waiting_bootstrap_marker(
    *,
    state: Path,
    fixture: dict[str, object],
    run_dir: Path,
    expires_at: str,
    task_id: str = "task-bootstrap",
    session_id: str = "session-bootstrap",
    run_id: str = "rvf-bootstrap",
) -> Path:
    """在锁根下种一把 ``state=waiting_bootstrap`` 的 in-progress 锁（gate 经真模块构造）。"""
    gate_module = load_bootstrap_gate_module()
    repo = fixture["repo"]
    run_dir.mkdir(parents=True, exist_ok=True)
    gate = gate_module.build_bootstrap_gate(
        repo,
        {
            "required_skill": "sync-local-base-into-task-worktree-only",
            "local_base_ref": fixture["local_base_ref"],
            "local_base_tip": fixture["local_base_tip"],
            "task_head": _git_stdout(["rev-parse", "HEAD"], repo),
        },
        origin_kind=gate_module.BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP,
        origin_prep_token="0123456789abcdef",
        origin_kanban_task_id=task_id,
        origin_session_id=session_id,
        rvf_run_id=run_id,
        rvf_run_dir=str(run_dir),
    )
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / _in_progress_marker_name(repo, task_id=task_id)
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "waiting_bootstrap",
                "bootstrap_gate": gate,
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": expires_at,
                "kanban_task_id": task_id,
                "session_id": session_id,
                "run_id": run_id,
                "run_dir": str(run_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker_path


def test_kanban_followup_waiting_bootstrap_lock_directs_sync_instead_of_handoff(
    tmp_path: Path,
) -> None:
    """锁 active 且挂着 bootstrap 等待 gate：给**定向**同步指令，不发通用「去收尾 handoff」提示。

    这一轮 review 根本还没开始（run/scope 未建、reviewer 未派），旧的 nudge/skip 两条分支都以
    「review 正在跑、只差 handoff」为前提，对它是错误指令。断言：force-continue 的 reason 指向
    同步 skill 与 receipt，而**不含** RVF_HANDOFF_FILE；且不消耗 re-engage nudge 预算。
    """
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )

    stdout, _ = invoke(
        {
            "cwd": str(fixture["repo"]),
            "session_id": "session-bootstrap",
            "stop_hook_active": False,
            "last_user_message": "继续推进这轮任务。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-bootstrap",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    # 受众 agent_must_act_now + 未标明 host ⇒ 兜底阻塞通道；systemMessage 是必需的
    # developer-only 旁注（把刻意注入与 hook 崩溃区分开）。
    assert set(payload.keys()) == {"decision", "reason", "systemMessage"}, payload
    assert payload["decision"] == "block"
    assert "刻意注入" in payload["systemMessage"], payload["systemMessage"]
    reason = payload["reason"]
    assert "sync-local-base-into-task-worktree-only" in reason
    assert "BASE_BRANCH_SYNC_POST_ABSORPTION_REVIEW_OWNER=RVF" in reason
    assert "尚未开始 review" in reason
    assert "RVF_HANDOFF_FILE" not in reason  # 不是「去收尾 handoff」那条通用提示
    latest = latest_summary(state)
    assert latest["status"] == "reengaged"
    assert latest["reason_code"] == "kanban_followup_bootstrap_bootstrap_still_required"
    events = latest_events(state)
    assert [e for e in events if e.get("event") == "kanban_followup_bootstrap_gate_waiting"]
    assert not [e for e in events if e.get("event") == "kanban_followup_in_progress_nudged"]
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "waiting_bootstrap"
    assert "revived_count" not in persisted  # 锁还没过期，不该被复活


def test_kanban_followup_stale_waiting_bootstrap_lock_revives_instead_of_reopening(
    tmp_path: Path,
) -> None:
    """32cad 重放：等待期跨过 1h 锁 TTL —— 锁必须被**复活**，绝不清锁 + 回落常规 gate 重开一轮。

    旧行为：stale 分支无条件 clear_marker + return None → 常规 gate 判「没人在跑」→ 重铸 token /
    新开 run，于是同一轮 bootstrap 等待被反复重开。断言：锁仍在、expires_at 被推到未来、
    revived_count=1，且**没有** ..._marker_consumed_incomplete 事件、没有派发新 RVF。
    """
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2000-01-01T00:00:00Z",  # 已过 TTL → STALE
    )

    stdout, _ = invoke(
        {
            "cwd": str(fixture["repo"]),
            "session_id": "session-bootstrap",
            "stop_hook_active": False,
            "last_user_message": "继续推进这轮任务。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-bootstrap",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert set(payload.keys()) == {"decision", "reason", "systemMessage"}, payload
    assert payload["decision"] == "block"
    assert "刻意注入" in payload["systemMessage"], payload["systemMessage"]
    assert "sync-local-base-into-task-worktree-only" in payload["reason"]

    assert marker_path.exists(), "stale 的 bootstrap 等待锁被清掉了（32cad 回归）"
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted["revived_count"] == 1
    assert persisted["state"] == "waiting_bootstrap"
    # 复活的判据是「锁重新变回 active」，由真锁模块判定，而不是拿字符串比字面量。
    assert persisted["expires_at"] != "2000-01-01T00:00:00Z"
    lock_module = load_kanban_followup_lock_module()
    assert lock_module.marker_status(persisted) == lock_module.STATUS_ACTIVE
    assert persisted["bootstrap_gate"]["rvf_run_id"] == "rvf-bootstrap"
    events = latest_events(state)
    assert not [
        e
        for e in events
        if e.get("event") == "kanban_followup_in_progress_marker_consumed_incomplete"
    ], events
    waiting = [e for e in events if e.get("event") == "kanban_followup_bootstrap_gate_waiting"]
    assert len(waiting) == 1
    assert waiting[0].get("lock_revived") is True
    assert waiting[0].get("bootstrap_phase") == "bootstrap_interrupted_retryable"
    # 锁根下只该有这**一把**锁：恢复不得产生第二把 / 第二轮。
    assert sorted(p.name for p in (state / "kanban-followup-in-progress").glob("*.json")) == [
        _in_progress_marker_name(fixture["repo"], task_id="task-bootstrap")
    ]


def test_kanban_followup_bootstrap_gate_resume_reuses_run_and_artifact_loss_clears(
    tmp_path: Path,
) -> None:
    """receipt 有效 → 按**原 run** 重放并摘掉 gate（锁不清）；工件丢失 → 清锁安全回 fresh。

    重放本身（prepare 全套）在别处已被覆盖，这里把 replay 注入成可控 stub，专测 Stop 读侧的
    三条收尾动作：resolved 不清锁 / 不新开、重放未完成时维持等待、artifact_lost 才清锁。
    """
    module = load_hook_module()
    gate_module = load_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["_marker_path"] = str(marker_path)
    gate = marker["bootstrap_gate"]

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    original_reconcile = gate_module.reconcile_bootstrap_gate
    original_replay = gate_module.replay_prepare_for_gate
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    try:
        ledger = _make_test_ledger(module, state / "log")

        # ① receipt 有效 + 重放成功 → 摘 gate、锁回 in_progress（**不清锁**）、force-continue 续审。
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_RECEIPT_VALID,
                phase=gate_module.PHASE_REVIEWING,
                reason="bootstrap_receipt_valid",
                diagnostic="stub: receipt 有效",
                resume_run_id=g.get("rvf_run_id"),
                resume_run_dir=g.get("rvf_run_dir"),
            )
        )
        module.rvf_bootstrap_gate.replay_prepare_for_gate = lambda g, **kw: {
            "status": "completed",
            "run_id": g.get("rvf_run_id"),
            "run_dir": g.get("rvf_run_dir"),
        }
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=dict(marker),
            task_id="task-bootstrap",
            session_id="session-bootstrap",
            cwd=str(fixture["repo"]),
            store_expired=False,
        )
        assert handled is True
        body = agent_directive_body(decision)
        assert "rvf-bootstrap" in body
        assert "不要**新开 review" in body
        assert marker_path.exists(), "bootstrap 收尾时不得清锁（否则常规 gate 会重开一轮）"
        resolved = json.loads(marker_path.read_text(encoding="utf-8"))
        assert resolved["bootstrap_gate"] is None
        assert resolved["state"] == "in_progress"

        # ② receipt 有效但重放没跑成 → 维持等待、锁仍在、gate 不摘。
        marker_path.write_text(
            json.dumps({**marker, "bootstrap_gate": gate}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        module.rvf_bootstrap_gate.replay_prepare_for_gate = lambda g, **kw: {
            "status": "timeout",
            "error": "stub: 重放超时",
        }
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=dict(marker),
            task_id="task-bootstrap",
            session_id="session-bootstrap",
            cwd=str(fixture["repo"]),
            store_expired=False,
        )
        assert handled is True
        agent_directive_body(decision)
        assert marker_path.exists()
        assert json.loads(marker_path.read_text(encoding="utf-8"))["bootstrap_gate"] is not None

        # ③ 等待期工件丢失 → 清锁安全回 fresh（无在途工作可撞），并显式落账原因。
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_ARTIFACT_LOST,
                phase=gate_module.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
                reason="rvf_run_artifacts_missing",
                diagnostic="stub: 工件丢失",
            )
        )
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=dict(marker),
            task_id="task-bootstrap",
            session_id="session-bootstrap",
            cwd=str(fixture["repo"]),
            store_expired=True,
        )
        assert handled is True
        assert decision is None  # 回落常规 gate
        assert not marker_path.exists()
        events = latest_events(state / "log")
        assert [
            e for e in events if e.get("event") == "kanban_followup_bootstrap_gate_artifacts_lost"
        ]
    finally:
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = original_reconcile
        module.rvf_bootstrap_gate.replay_prepare_for_gate = original_replay
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


def test_kanban_followup_bootstrap_replay_classifies_artifact_loss_and_retries_after_timeout(
    tmp_path: Path,
) -> None:
    """重放结果必须**分类**处理，不得一律折叠成 still_waiting（RVF-001 回归）。

    ① ``artifact_lost``：prep 文件 TTL 只有 300s（远短于一次 bootstrap 等待，且 sweep_stale 会删
       它），此时本轮没有任何在途工作可撞——必须立刻走「显式诊断 + 清锁安全重来」，而不是被硬等
       到 re-engage / revive 预算耗尽。
    ② ``timeout``：重放跑在独立子进程里（内层预算到点自己写结果 `os._exit`，外层硬上限按进程组
       SIGKILL），收尾后**不留**在途 prepare 写者。故超时必须释放起 replay 前抢到的占位，让下一次
       Stop **立刻**重试同一个 run 的 prepare（RVF-009 回归）——扣着占位只会把一轮本可立刻恢复的
       等待白拖满替换窗口，还给 agent 一条「仍在后台进行」的事实错误 reason。
    ③ 并发去重仍在，且是**有界**的：占位被别的持有者握着时本轮不起第二次重放；持有者崩在半路
       （占位过接手窗口）则就地接手。
    顺带守 RVF-002 在入口 B 的写侧：校验通过的 receipt digest 必须被盖回锁上的 gate。
    """
    module = load_hook_module()
    gate_module = load_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )
    pristine = marker_path.read_text(encoding="utf-8")

    def _marker() -> dict:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["_marker_path"] = str(marker_path)
        return payload

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    original_reconcile = gate_module.reconcile_bootstrap_gate
    original_replay = gate_module.replay_prepare_for_gate
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    replay_calls: list[str] = []
    try:
        ledger = _make_test_ledger(module, state / "log")
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_RECEIPT_VALID,
                phase=gate_module.PHASE_REVIEWING,
                reason="bootstrap_receipt_valid",
                diagnostic="stub: receipt 有效",
                receipt_path=str(tmp_path / "receipt.json"),
                receipt_digest="a" * 64,
                resume_run_id=g.get("rvf_run_id"),
                resume_run_dir=g.get("rvf_run_dir"),
            )
        )

        def _replay_timeout(g, **kw):
            replay_calls.append("timeout")
            return {"status": "timeout", "error": "stub: 重放超时，后台 worker 仍在跑"}

        def _replay_artifact_lost(g, **kw):
            replay_calls.append("artifact_lost")
            return {"status": "artifact_lost", "error": "stub: prep 文件已被 sweep"}

        # ① 重放超时 → 维持等待，但**释放**占位；gate 上还必须带上已校验的 receipt digest。
        module.rvf_bootstrap_gate.replay_prepare_for_gate = _replay_timeout
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        # 已经吸收完、receipt 也验过了：定向指令必须**反过来**要求别再跑同步 skill、别动 receipt。
        assert "**不要**再跑一次同步 skill" in agent_directive_body(decision)
        persisted = json.loads(marker_path.read_text(encoding="utf-8"))
        assert persisted["bootstrap_gate"] is not None
        assert persisted["bootstrap_gate"]["bootstrap_receipt_digest"] == "a" * 64
        lock_module = load_kanban_followup_lock_module()
        # 在途占位落在 marker **旁边**的 claim sidecar 上（不在 payload 里：payload 的整份
        # 读-改-写会被并发 Stop 互相抹掉，占位就不再原子）。超时后子进程已收尾、没有在途写者，
        # 占位必须被释放，否则下一轮什么都不能做，只能干等满替换窗口。
        assert (
            lock_module.bootstrap_replay_in_flight_age_seconds(
                {"_marker_path": str(marker_path)}
            )
            is None
        ), "重放超时后占位没被释放（本可立刻重试的一轮被白拖满替换窗口）"
        assert replay_calls == ["timeout"]

        # ② 紧接着的下一次 Stop（远在替换窗口之内）：必须**立刻**重新抢占并重试同一个 run 的
        # prepare，而不是被上一轮超时留下的占位挡掉。
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        agent_directive_body(decision)
        assert replay_calls == ["timeout", "timeout"], "超时后的下一次 Stop 没能立刻重试重放"
        pending = [
            e
            for e in latest_events(state / "log")
            if e.get("event") == "kanban_followup_bootstrap_gate_replay_pending"
        ]
        assert len(pending) == 2  # ① 与 ② 各一条，且与普通 waiting 事件区分
        # 超时 = 「这次重放已收尾但没成功、可以立刻再试」，不是「还有人在跑」。
        assert pending[-1]["reason_code"] == "kanban_followup_bootstrap_bootstrap_replay_incomplete"
        assert not [
            e
            for e in latest_events(state / "log")
            if e.get("event") == "kanban_followup_bootstrap_gate_waiting"
        ]

        # ③ 并发去重仍在：占位被**另一个仍在跑重放的持有者**握着时，本轮不得起第二次重放。
        assert lock_module.claim_bootstrap_replay_in_flight(
            {"_marker_path": str(marker_path)}, window_seconds=3600
        )[0] is True
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        assert replay_calls == ["timeout", "timeout"], "占位在途时又起了第二次 prepare 重放"
        in_flight = [
            e
            for e in latest_events(state / "log")
            if e.get("event") == "kanban_followup_bootstrap_gate_replay_pending"
        ]
        assert in_flight[-1]["reason_code"] == "kanban_followup_bootstrap_bootstrap_replay_in_flight"

        # ④ 接手窗口设为 0（持有者崩在半路）→ 允许接手再试，去重是**有界**的而非永久禁止重放。
        os.environ[module.KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_ENV] = "0"
        try:
            handled, decision = module._kanban_followup_bootstrap_gate_decision(
                ledger, marker=_marker(), task_id="task-bootstrap",
                session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
            )
        finally:
            os.environ.pop(module.KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_ENV, None)
        assert handled is True
        assert replay_calls == ["timeout", "timeout", "timeout"]

        # ⑤ 重放报 artifact_lost → 清锁安全重来（不是继续空等），并落一条**可区分**的事件。
        marker_path.write_text(pristine, encoding="utf-8")
        module.rvf_bootstrap_gate.replay_prepare_for_gate = _replay_artifact_lost
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        assert decision is None  # 回落常规 gate，而不是硬挡着继续等
        assert not marker_path.exists()
        assert replay_calls == ["timeout", "timeout", "timeout", "artifact_lost"]
        events = latest_events(state / "log")
        lost = [
            e
            for e in events
            if e.get("event") == "kanban_followup_bootstrap_gate_replay_artifact_lost"
        ]
        assert len(lost) == 1
        assert lost[0]["reason_code"] == "kanban_followup_bootstrap_bootstrap_replay_artifact_lost"
        # 与「reconcile 级工件丢失」是两条可区分的路径，不共用同一个事件名。
        assert not [
            e for e in events if e.get("event") == "kanban_followup_bootstrap_gate_artifacts_lost"
        ]
    finally:
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = original_reconcile
        module.rvf_bootstrap_gate.replay_prepare_for_gate = original_replay
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


def test_kanban_followup_stale_bootstrap_gate_resume_reactivates_lock(
    tmp_path: Path,
) -> None:
    """从 STALE 进入 + receipt 有效 + 重放成功：摘 gate 的**同时**必须把锁续回 active。

    ``stamp_bootstrap_gate`` 是读-改-写，会原样保留已经过期的 ``expires_at``：只把 ``state``
    写回 ``in_progress`` 的话，锁的 marker_status 仍是 stale。下一次 Stop：gate 已摘 →
    bootstrap 分支 handled=False → 落到既有 stale 分支 clear_kanban_followup_lock + return
    None → 常规 gate 判「没人在跑」→ 为同一个仍在继续的 run 再开一轮 review。正是这套改动
    本来要根治的 32cad 失败模式在「恢复成功」这条路径上原样复活。
    """
    module = load_hook_module()
    gate_module = load_bootstrap_gate_module()
    lock_module = load_kanban_followup_lock_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2000-01-01T00:00:00Z",
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["_marker_path"] = str(marker_path)

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    original_reconcile = gate_module.reconcile_bootstrap_gate
    original_replay = gate_module.replay_prepare_for_gate
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    try:
        ledger = _make_test_ledger(module, state / "log")
        # 前提：这把锁**确实**已过 TTL（本轮就是从 stale 分支进来的）。
        assert lock_module.marker_status(marker) == lock_module.STATUS_STALE

        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_RECEIPT_VALID,
                phase=gate_module.PHASE_REVIEWING,
                reason="bootstrap_receipt_valid",
                diagnostic="stub: receipt 有效",
                resume_run_id=g.get("rvf_run_id"),
                resume_run_dir=g.get("rvf_run_dir"),
            )
        )
        module.rvf_bootstrap_gate.replay_prepare_for_gate = lambda g, **kw: {
            "status": "completed",
            "run_id": g.get("rvf_run_id"),
            "run_dir": g.get("rvf_run_dir"),
        }
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=dict(marker),
            task_id="task-bootstrap",
            session_id="session-bootstrap",
            cwd=str(fixture["repo"]),
            store_expired=True,
        )
        assert handled is True
        agent_directive_body(decision)
        assert marker_path.exists(), "bootstrap 收尾时不得清锁（否则常规 gate 会重开一轮）"
        persisted = json.loads(marker_path.read_text(encoding="utf-8"))
        assert persisted["bootstrap_gate"] is None
        assert persisted["state"] == "in_progress"
        # 核心回归判据：锁重新变回 **active 的 in_progress 锁**（由真锁模块判定，不比字面量）。
        assert persisted["expires_at"] != "2000-01-01T00:00:00Z"
        assert lock_module.marker_status(persisted) == lock_module.STATUS_ACTIVE

        # 复演下一次 Stop：读到的是 active 锁 → 走常规 active 分支（handled=False 交回上层的
        # nudge/skip），而**不是** stale 清锁分支；锁仍在，且没有第二把锁 / 第二轮。
        next_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        next_marker["_marker_path"] = str(marker_path)
        assert lock_module.marker_status(next_marker) == lock_module.STATUS_ACTIVE
        handled_next, decision_next = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=next_marker,
            task_id="task-bootstrap",
            session_id="session-bootstrap",
            cwd=str(fixture["repo"]),
            store_expired=False,
        )
        assert handled_next is False and decision_next is None
        assert marker_path.exists()
        assert sorted(p.name for p in (state / "kanban-followup-in-progress").glob("*.json")) == [
            _in_progress_marker_name(fixture["repo"], task_id="task-bootstrap")
        ]
        resolved = [
            e
            for e in latest_events(state / "log")
            if e.get("event") == "kanban_followup_bootstrap_gate_resolved"
        ]
        assert len(resolved) == 1
    finally:
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = original_reconcile
        module.rvf_bootstrap_gate.replay_prepare_for_gate = original_replay
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


def test_kanban_followup_bootstrap_replay_claim_is_atomic_across_concurrent_stops(
    tmp_path: Path,
) -> None:
    """两个并发 Stop 同时判到「receipt 有效」时，只有**一个**能真正起 prepare 重放。

    在途占位若是**事后**才写（只在 replay 返回 timeout 时盖），两个并发进入者会同时读到
    「无在途标记」并各自起一次 replay —— 同一 prep / 同一 run_id 被并发 prepare、同一个 run
    目录被重复写。故占位必须在起 replay **之前**原子抢占；且必须落在 marker payload 之外，
    否则另一个 Stop 那次整份读-改-写（把 receipt digest 盖回 gate）会顺手把刚落的占位抹掉。

    本用例刻意让两个进入者**各持一份在对方写盘之前读到的陈旧 marker 副本**（真实竞态形状）。
    """
    module = load_hook_module()
    gate_module = load_bootstrap_gate_module()
    lock_module = load_kanban_followup_lock_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )

    def _stale_copy() -> dict:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["_marker_path"] = str(marker_path)
        return payload

    first_marker = _stale_copy()
    second_marker = _stale_copy()

    entered_replay = threading.Event()
    second_finished = threading.Event()
    calls_lock = threading.Lock()
    replay_calls: list[str] = []
    results: dict[str, tuple] = {}
    errors: list[BaseException] = []

    def _replay(gate, **_kwargs):
        name = threading.current_thread().name
        with calls_lock:
            replay_calls.append(name)
        if name == "stop-first":
            # 把「第一个 Stop 正卡在 replay 里」这段窗口撑开，让第二个 Stop 恰好在此期间进入。
            entered_replay.set()
            second_finished.wait(timeout=30)
        return {"status": "timeout", "error": "stub: 重放超时，后台 worker 仍在跑"}

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    original_reconcile = gate_module.reconcile_bootstrap_gate
    original_replay = gate_module.replay_prepare_for_gate
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    try:
        # 两个 Stop 各写各的 ledger：本用例验的是重放去重，不该被两个线程往同一份
        # events.jsonl 交错追加而变脆。
        first_ledger = _make_test_ledger(module, state / "log-first")
        second_ledger = _make_test_ledger(module, state / "log-second")
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_RECEIPT_VALID,
                phase=gate_module.PHASE_REVIEWING,
                reason="bootstrap_receipt_valid",
                diagnostic="stub: receipt 有效",
                receipt_path=str(tmp_path / "receipt.json"),
                receipt_digest="b" * 64,
                resume_run_id=g.get("rvf_run_id"),
                resume_run_dir=g.get("rvf_run_dir"),
            )
        )
        module.rvf_bootstrap_gate.replay_prepare_for_gate = _replay

        def _first() -> None:
            try:
                results["first"] = module._kanban_followup_bootstrap_gate_decision(
                    first_ledger,
                    marker=first_marker,
                    task_id="task-bootstrap",
                    session_id="session-bootstrap",
                    cwd=str(fixture["repo"]),
                    store_expired=False,
                )
            except BaseException as exc:  # noqa: BLE001 — 线程异常要带回主线程断言
                errors.append(exc)
            finally:
                entered_replay.set()

        def _second() -> None:
            try:
                assert entered_replay.wait(timeout=30), "第一个 Stop 始终没进入 replay"
                results["second"] = module._kanban_followup_bootstrap_gate_decision(
                    second_ledger,
                    marker=second_marker,
                    task_id="task-bootstrap",
                    session_id="session-bootstrap",
                    cwd=str(fixture["repo"]),
                    store_expired=False,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                second_finished.set()

        threads = [
            threading.Thread(target=_first, name="stop-first"),
            threading.Thread(target=_second, name="stop-second"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not [t for t in threads if t.is_alive()], "并发 Stop 线程没能收尾"
        assert not errors, errors

        assert replay_calls == ["stop-first"], (
            f"两个并发 Stop 各起了一次 prepare 重放（同一 run 目录并发重复写）：{replay_calls}"
        )
        handled_second, decision_second = results["second"]
        assert handled_second is True
        assert "正在跑本轮 prepare 重放" in agent_directive_body(decision_second)
        handled_first, decision_first = results["first"]
        assert handled_first is True
        assert decision_first is not None
        # 占位的寿命 == 一次重放调用：持有者收尾（这里是超时）后必须释放，下一次 Stop 才能立刻
        # 重试；去重只发生在「真的有人正在跑」的那段窗口里。
        assert (
            lock_module.bootstrap_replay_in_flight_age_seconds(
                {"_marker_path": str(marker_path)}
            )
            is None
        )
        # 恢复不得产生第二把锁 / 第二轮。
        assert sorted(p.name for p in (state / "kanban-followup-in-progress").glob("*.json")) == [
            _in_progress_marker_name(fixture["repo"], task_id="task-bootstrap")
        ]
    finally:
        second_finished.set()
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = original_reconcile
        module.rvf_bootstrap_gate.replay_prepare_for_gate = original_replay
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


def test_kanban_followup_lock_without_bootstrap_gate_keeps_legacy_stale_consumption(
    tmp_path: Path,
) -> None:
    """非 opt-in / 无 gate 的旧锁：stale 语义**逐字不变**（仍是 consume + 回落常规 gate）。"""
    module = load_hook_module()
    state = tmp_path / "state"
    marker_dir = state / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True)
    # 这把锁**不经 key 解析**：本用例直接把 marker dict 喂给 _kanban_followup_bootstrap_gate_decision
    # 并断言 handled is False，marker_paths 根本没被调用，故文件名保持字面量即可（tmp_path 也不是
    # git 仓，现算文件名反而会失败）。
    marker_path = marker_dir / "task-legacy.json"
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2000-01-01T00:00:00Z",
                "kanban_task_id": "task-legacy",
                "session_id": "session-legacy",
                "run_id": "rvf-legacy",
                "run_dir": str(tmp_path / "legacy-run"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["_marker_path"] = str(marker_path)

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    try:
        ledger = _make_test_ledger(module, state / "log")
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger,
            marker=marker,
            task_id="task-legacy",
            session_id="session-legacy",
            cwd=str(tmp_path),
            store_expired=True,
        )
        # 无 gate → 本分支完全不接手，旧的 active/stale 逻辑原样生效。
        assert handled is False
        assert decision is None
        assert marker_path.exists()
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log


def test_kanban_followup_bootstrap_waiting_is_bounded_by_reengage_and_revive_budgets(
    tmp_path: Path,
) -> None:
    """等待态必须**终止**：护住一轮是对的，无界护住只是把「反复重开」换成「永久驻留」。

    两条界各测一次：re-engage 预算用尽 → 退回静默 skip（不再硬挡 agent 干停，等待态仍保留）；
    复活次数用尽 → 本分支不再接手（handled=False），交还既有 stale 语义清锁、明确放弃本轮。
    """
    module = load_hook_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )

    def _marker(**overrides: object) -> dict:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload.update(overrides)
        payload["_marker_path"] = str(marker_path)
        return payload

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    try:
        ledger = _make_test_ledger(module, state / "log")

        # ① 预算内：force-continue 定向指令，并把 bootstrap 专属计数（与 review nudge 预算隔离）+1。
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        agent_directive_body(decision)
        persisted = json.loads(marker_path.read_text(encoding="utf-8"))
        assert persisted["bootstrap_reengage_count"] == 1
        assert "reengage_nudge_count" not in persisted  # 不吃 review 收尾那把预算

        # ② re-engage 预算用尽 → 静默 skip（{"continue":...}），等待态**不清**。
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(bootstrap_reengage_count=3), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        assert decision.payload.get("continue") is True
        assert "decision" not in decision.payload
        assert marker_path.exists()
        assert json.loads(marker_path.read_text(encoding="utf-8"))["bootstrap_gate"] is not None

        # ③ 复活预算用尽 → 不再复活、不接手，交回既有 stale 清锁语义（明确放弃本轮）。
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(revived_count=3), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=True,
        )
        assert handled is False
        assert decision is None
        events = latest_events(state / "log")
        assert [
            e
            for e in events
            if e.get("event") == "kanban_followup_bootstrap_gate_revive_budget_exhausted"
        ]
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


def test_kanban_followup_bootstrap_reengage_budget_exhausted_diagnostic_splits_replay_pending(
    tmp_path: Path,
) -> None:
    """re-engage 预算耗尽这条出口的诊断文案必须按「在等什么」分流（RVF-009 回归）。

    预算检查排在 replay_pending 判定**之前**，所以这条出口同样会在「base 早已吸收、receipt 已
    校验通过、只差 prepare 收尾」时到达。此时若照写「需要先跑同步 skill 吸收 local base」，就是
    一条方向完全相反的记录——同一函数下方的 `bootstrap_replay_pending_reengage_reason` 明确写着
    再跑一次同步 skill 会产出另一份 receipt、下一轮按 digest 判 tampered 把这一轮锁死。
    """
    module = load_hook_module()
    gate_module = load_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_task_repo(tmp_path / "task-repo")
    state = tmp_path / "state"
    marker_path = _waiting_bootstrap_marker(
        state=state,
        fixture=fixture,
        run_dir=tmp_path / "existing-run",
        expires_at="2999-01-01T00:00:00Z",
    )

    def _marker(**overrides: object) -> dict:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload.update(overrides)
        payload["_marker_path"] = str(marker_path)
        return payload

    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    original_reconcile = gate_module.reconcile_bootstrap_gate
    original_replay = gate_module.replay_prepare_for_gate
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state / "kanban-followup-in-progress")
    try:
        ledger = _make_test_ledger(module, state / "log")

        # ① 「还没吸收 base」这条（reconcile 直接判 still_waiting）：文案照旧指向同步 skill。
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_STILL_WAITING,
                phase=gate_module.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP,
                reason="bootstrap_receipt_missing",
                diagnostic="stub: 还没吸收",
            )
        )
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(bootstrap_reengage_count=3), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        assert decision.payload.get("continue") is True
        waiting_message = decision.message
        assert "需要先跑" in waiting_message
        assert "sync-local-base-into-task-worktree-only" in waiting_message

        # ② 「只差 prepare 收尾」这条（receipt 已验 + 重放未完成）：**不得**再让人去跑同步 skill。
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = lambda g, **kw: (
            gate_module.BootstrapGateReconciliation(
                outcome=gate_module.OUTCOME_RECEIPT_VALID,
                phase=gate_module.PHASE_REVIEWING,
                reason="bootstrap_receipt_valid",
                diagnostic="stub: receipt 有效",
                receipt_path=str(tmp_path / "receipt.json"),
                receipt_digest="c" * 64,
                resume_run_id=g.get("rvf_run_id"),
                resume_run_dir=g.get("rvf_run_dir"),
            )
        )
        module.rvf_bootstrap_gate.replay_prepare_for_gate = lambda g, **kw: {
            "status": "timeout",
            "error": "stub: 重放子进程被强杀",
        }
        handled, decision = module._kanban_followup_bootstrap_gate_decision(
            ledger, marker=_marker(bootstrap_reengage_count=3), task_id="task-bootstrap",
            session_id="session-bootstrap", cwd=str(fixture["repo"]), store_expired=False,
        )
        assert handled is True
        assert decision.payload.get("continue") is True
        pending_message = decision.message
        assert "需要先跑" not in pending_message, pending_message
        assert "已完成" in pending_message and "只差本轮 prepare 收尾" in pending_message
        assert "**不要**再跑一次同步 skill" in pending_message
        # 记录同样要落进 ledger 的 skipped 事件（事后排障读的就是它）。
        skipped = [
            e
            for e in latest_events(state / "log")
            if e.get("event") == "skipped"
            and e.get("reason_code") == "kanban_followup_waiting_local_base_bootstrap"
        ]
        assert len(skipped) == 2
        assert "需要先跑" not in str(skipped[-1].get("message"))
        assert marker_path.exists()
        assert json.loads(marker_path.read_text(encoding="utf-8"))["bootstrap_gate"] is not None
    finally:
        module.rvf_bootstrap_gate.reconcile_bootstrap_gate = original_reconcile
        module.rvf_bootstrap_gate.replay_prepare_for_gate = original_replay
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root


# ---------------------------------------------------------------------------
# delivered_awaiting_agent_decision 三态 + rewind 对账（治「被强制续跑一次从未接受的 RVF」）
# ---------------------------------------------------------------------------

def _write_kanban_followup_lock_marker(
    marker_dir: Path,
    *,
    task_id: str,
    state: str,
    expires_at: str,
    repository_identity_anchor: Path | str,
    dispatch_token: str | None = None,
    delivered_transcript_path: str | None = None,
    delivered_session_id: str | None = None,
    session_id: str = "session-active",
    run_id: str = "rvf-existing",
    run_dir: str = "/tmp/rvf-existing",
    repo: str | None = None,
) -> Path:
    # ``repository_identity_anchor`` 只决定**文件名**（key 含仓身份），与 payload 的 ``repo``
    # 字段刻意分开：后者是「读侧据它定位 tracker」的语义字段，部分用例故意留 None 来验
    # 「无 repo 时不误放 lease」，不能被文件名需求顺手改掉。
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / _in_progress_marker_name(
        repository_identity_anchor, task_id=task_id
    )
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": state,
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": expires_at,
                "kanban_task_id": task_id,
                "session_id": session_id,
                "run_id": run_id,
                "run_dir": run_dir,
                # UPS arm 时从 prep 的 ``origin_repo`` 抄来；清锁释放 lease 时读侧按它定位 tracker。
                "repo": repo,
                "dispatch_token": dispatch_token,
                "delivered_session_id": delivered_session_id,
                "delivered_transcript_path": delivered_transcript_path,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker_path


def _write_transcript_entries(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path


def test_accepted_review_run_receipt_suppresses_repeated_decision_point_injection(
    tmp_path: Path,
) -> None:
    """INV-5：agent 已在发起决策点上接受并起了一轮 run，后续 Stop 不得把同一批改动重新征询。

    就地接受没有 UPS 事件，凭证由 ``rvf-agent start`` 自己 arm（session-keyed、带
    ``decision_token``）。这里直接把那枚凭证摆上，验证下一次 Stop 走在途 skip 而不是再注入一次
    横幅——不然接受之后每一次 Stop 都会把同一批改动再问一遍。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    lock_module = load_kanban_followup_lock_module()
    marker_path = lock_module.write_marker(
        task_id=None,
        session_id="session-accepted",
        run_id="rvf-accepted-in-flight",
        run_dir=str(tmp_path / "accepted-run"),
        repo=str(dirty),
        cwd=str(dirty),
        decision_token="a1b2c3d4e5f60718",
        delivered_session_id="session-accepted",
        state=lock_module.STATE_IN_PROGRESS,
        # ``_root(root)`` 会自己拼上 SUBDIR_NAME，故这里给 state 本身；子进程那边由
        # ``invoke`` 的 RVF_KANBAN_FOLLOWUP_LOCK_ROOT 指到同一层。
        root=state,
    )
    assert marker_path is not None and marker_path.exists()

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-accepted",
            "stop_hook_active": False,
            "last_user_message": "继续推进这一轮 RVF。",
        },
        # 默认 fork mode（auto）：没有这枚凭证的话本轮**必然**注入一次决策点横幅。
        extra_env={"CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "0"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "RVF_DECISION_TOKEN=" not in str(payload.get("reason") or ""), payload
    latest = latest_summary(state)
    assert latest["reason_code"] == "kanban_followup_in_progress", latest
    assert latest["active_rvf_run_id"] == "rvf-accepted-in-flight", latest
    assert marker_path.exists(), "在途凭证不该被这一次 Stop 清掉"


def test_rewound_acceptance_reconciles_receipt_via_decision_token_needle(
    tmp_path: Path,
) -> None:
    """接受被 rewind 掉：凭证按**决策 token 的 needle**（``RVF_DECISION_TOKEN=``）对账清除。

    Fix B 的祖先链对账历史上只认 ``RVF_DISPATCH=token=``。就地接受的凭证根本没有 dispatch
    token，若不从 marker 现推 needle，这类凭证会永远判成「无 token → unknown」，rewind 之后
    一路挡到 1h TTL——正是「锁 squat」那一族缺陷。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    decision_token = "0f1e2d3c4b5a6978"
    transcript = _write_transcript_entries(
        tmp_path / "transcript.jsonl",
        [
            {"uuid": "u0", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "起点"}},
            # 横幅落地成一个回合（正文含 needle）……
            {"uuid": "u1", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": f"RVF_DECISION_TOKEN={decision_token} 决策点"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "已 start"}},
            # ……随后用户 rewind：新 prompt 的 parentUuid 挂回 u0，那一段离开祖先链。
            {"uuid": "u2", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": "改做别的"}},
        ],
    )
    lock_module = load_kanban_followup_lock_module()
    marker_path = lock_module.write_marker(
        task_id=None,
        session_id="session-rewound",
        run_id="rvf-rewound",
        run_dir=str(tmp_path / "rewound-run"),
        repo=str(dirty),
        cwd=str(dirty),
        decision_token=decision_token,
        delivered_session_id="session-rewound",
        delivered_transcript_path=str(transcript),
        state=lock_module.STATE_IN_PROGRESS,
        # ``_root(root)`` 会自己拼上 SUBDIR_NAME，故这里给 state 本身；子进程那边由
        # ``invoke`` 的 RVF_KANBAN_FOLLOWUP_LOCK_ROOT 指到同一层。
        root=state,
    )
    assert marker_path is not None and marker_path.exists()

    invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-rewound",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "改做别的",
        },
        state_dir=state,
    )

    assert not marker_path.exists(), "rewind 掉的接受，其在途凭证必须被对账清除"
    events = latest_events(state)
    reconciled = [
        e for e in events if e.get("event") == "kanban_followup_lock_reconciled_for_rewind"
    ]
    assert reconciled, [e.get("event") for e in events]
    assert reconciled[-1].get("kanban_followup_trigger_dispatch_token") == decision_token


def test_kanban_followup_awaiting_agent_decision_lock_skips_without_nudging(
    tmp_path: Path,
) -> None:
    """投递已落地但 agent 未被确认接手（trigger 从未成为 transcript 条目）：只互斥去重、**不发 nudge**。

    这是事故的核心格：注入的 trigger 被同回合的中断冲掉（enqueue→dequeue），agent 对这轮 RVF
    零 token 产出，历史实现却按 in_progress 锁催它「继续收尾这一轮、输出 handoff」。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = _write_transcript_entries(
        tmp_path / "transcript.jsonl",
        [
            {"uuid": "u1", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "做点别的"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "好"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="delivered_awaiting_agent_decision",
        expires_at="2999-01-01T00:00:00Z",
        repository_identity_anchor=dirty,
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "继续做用户要求的别的工作。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-active",
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    # skip 契约：{"continue": ...}，绝不是 force-continue 的 {"decision":"block"}。
    assert "decision" not in payload, payload
    latest = latest_summary(state)
    assert latest["reason_code"] == "kanban_followup_delivered_awaiting_agent_decision"
    events = latest_events(state)
    assert not [e for e in events if e.get("event") == "kanban_followup_in_progress_nudged"], events
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted.get("reengage_nudge_count") is None  # 预算一次都没被消耗
    assert persisted["state"] == "delivered_awaiting_agent_decision"  # 未被误升格


def test_kanban_followup_awaiting_agent_decision_window_expiry_returns_to_normal_gate(
    tmp_path: Path,
) -> None:
    """awaiting 短窗到期仍未确认接手：清锁、交回常规 gate 按**当时**工作树重新决策，绝不复活旧派发。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = _write_transcript_entries(
        tmp_path / "transcript.jsonl",
        [
            {"uuid": "u1", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "做点别的"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="delivered_awaiting_agent_decision",
        expires_at="2020-01-01T00:00:00Z",  # 已过期
        repository_identity_anchor=dirty,
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
    )

    invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "继续做用户要求的别的工作。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "off",  # 交回常规 gate 后不真派发，只验锁被回收
            "KANBAN_TASK_ID": "task-active",
        },
        state_dir=state,
    )

    events = latest_events(state)
    expired = [
        e for e in events
        if e.get("event") == "kanban_followup_awaiting_agent_decision_window_expired"
    ]
    assert len(expired) == 1, events
    assert expired[0].get("reason_code") == "kanban_followup_awaiting_agent_decision_expired"
    assert not marker_path.exists()  # 锁已回收，不再挡后续 Stop


def test_kanban_followup_awaiting_lock_promoted_when_trigger_on_ancestor_chain(
    tmp_path: Path,
) -> None:
    """trigger 确实成为回合、且仍在当前 leaf 的祖先链上 → 升格 in_progress，nudge 预算等一切照旧。"""
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = _write_transcript_entries(
        tmp_path / "transcript.jsonl",
        [
            {"uuid": "u1", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "RVF_DISPATCH=token=abcdef0123456789 请审"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "开始 review"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="delivered_awaiting_agent_decision",
        expires_at="2999-01-01T00:00:00Z",
        repository_identity_anchor=dirty,
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "transcript_path": str(transcript),
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

    events = latest_events(state)
    promoted = [
        e for e in events
        if e.get("event") == "kanban_followup_lock_promoted_after_agent_received_trigger"
    ]
    assert len(promoted) == 1, events
    assert promoted[0].get("kanban_followup_lock_state") == "in_progress"
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "in_progress"
    # 升格后落回常规 in_progress 分支：nudge 照发。
    payload = parse_json(stdout)
    # 本 fixture 写的是 Claude 形状 transcript ⇒ host 探测判 Claude ⇒ 走非错误的
    # additionalContext 通道；断言只关心「确实驱动了 agent + 正文对」，不钉线材。
    emitted_agent_directive_body(payload)
    assert latest_summary(state)["reason_code"] == "kanban_followup_in_progress_reengage"


def test_kanban_followup_lock_reconciled_when_trigger_rewound_off_ancestor_chain(
    tmp_path: Path,
) -> None:
    """trigger 落地过、但已被用户 rewind 出主链：清锁交回常规 gate，绝不再催收尾这一轮。

    与「从未落地」区分开：token 在 transcript 里**出现过**却不在祖先链上，是 rewind 的确凿证据
    （/compact 抹历史会让 token 整个消失 → 走 absent 分支，故本判据不因压缩误伤）。
    """
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = _write_transcript_entries(
        tmp_path / "transcript.jsonl",
        [
            {"uuid": "u0", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "起点"}},
            # 被 rewind 掉的那一段：trigger 及其后续仍留在文件里，但不再挂在主链上。
            {"uuid": "u1", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": "RVF_DISPATCH=token=abcdef0123456789 请审"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "开始 review"}},
            # 用户重发自己的 prompt，parentUuid 指回 u0 → 主链绕开了 trigger。
            {"uuid": "u2", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": "改做别的"}},
            {"uuid": "a2", "parentUuid": "u2", "type": "assistant",
             "message": {"role": "assistant", "content": "好"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="in_progress",  # 已升格过，仍应被 rewind 对账清掉
        expires_at="2999-01-01T00:00:00Z",
        repository_identity_anchor=dirty,
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
    )

    invoke(
        {
            "cwd": str(dirty),
            "session_id": "session-active",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "继续做用户要求的别的工作。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "off",
            "KANBAN_TASK_ID": "task-active",
        },
        state_dir=state,
    )

    events = latest_events(state)
    reconciled = [
        e for e in events if e.get("event") == "kanban_followup_lock_reconciled_for_rewind"
    ]
    assert len(reconciled) == 1, events
    assert reconciled[0].get("kanban_followup_trigger_presence") == "off_ancestor_chain_rewound"
    assert not [e for e in events if e.get("event") == "kanban_followup_in_progress_nudged"], events
    assert not marker_path.exists()


# ---------------------------------------------------------------------------
# 清锁必须连带释放 tracker lease；升格写盘失败必须 fail-closed
# ---------------------------------------------------------------------------

def _tracker_lease_state(db_path: Path, lease_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT state FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _tracker_unit_count_by_review_state(db_path: Path, review_state: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM units WHERE review_state=?", (review_state,)
        ).fetchone()[0]
    finally:
        conn.close()


def _kanban_followup_run_with_real_tracker_lease(tmp_path: Path, state: Path) -> dict:
    """真跑一次 tracker refresh + allocate，产出「一把 active lease + 记着它的 RVF run dir」。

    刻意**不**手搓 sqlite 假 lease：手写行一旦与真实 DDL / lease 状态机漂移，缺陷仍在时测试会
    假绿。``run_dir`` 也直接用这次 allocate 的 ledger run dir——``artifacts/tracker-scope.json``
    是真产物，于是「读侧按哪个 artifact 名回捞 lease id」这条契约一并被钉死。

    tracker 根：in-process 侧经 ``CODEX_RVF_LOG_ROOT``、子进程侧经 ``invoke`` 的
    ``CODEX_RVF_STATE_DIR``，两者都解析到同一个 ``state``（``log_root()`` 的取值顺序），
    故子进程 Stop 释放的正是这里分配的这把 lease。
    """
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    transcript = tmp_path / "allocating-session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-lease-fixture")
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    try:
        os.environ["CODEX_RVF_FORK_MODE"] = "kanban-followup"
        ledger = _make_test_ledger(module, state)
        event = {
            "cwd": str(repo),
            "transcript_path": str(transcript),
            "task_id": "task-active",
        }
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        assert module.allocate_auto_review_scope(context, ledger, dry_run=False) is None
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_mode is None:
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
        else:
            os.environ["CODEX_RVF_FORK_MODE"] = old_mode

    meta = getattr(ledger, "tracker_scope_meta", None)
    assert isinstance(meta, dict), "fixture 没拿到 lease；后面的释放断言会变成假绿"
    db_path = Path(meta["tracker_dir"]) / "tracker.sqlite3"
    lease_id = meta["tracker_lease_id"]
    # 前置钉死起点：lease 真是 active、unit 真被 assigned 走了。否则「释放后变 available」
    # 这条断言在缺陷仍在时也会通过。
    assert _tracker_lease_state(db_path, lease_id) == "active"
    assert _tracker_unit_count_by_review_state(db_path, "assigned") > 0
    return {
        "repo": repo,
        "lease_id": lease_id,
        "db_path": db_path,
        "run_dir": Path(meta["tracker_scope_path"]).parent.parent,
    }


def test_kanban_followup_awaiting_window_expiry_releases_tracker_lease(tmp_path: Path) -> None:
    """awaiting 窗口过期清锁时必须**连带释放本轮 lease**，否则「交回常规 gate」只是一句空话。

    lease 默认 1h 而 awaiting 短窗只有 10min：只清锁不放 lease 的话，那批 unit 还要在
    ``assigned`` 上再挂 ~50min，而常规 gate 的候选查询只收 ``review_state='available'``
    → 整批被排除，「按当时工作树重新决策」根本没有可决策的东西；等 lease 自己过期时，
    会话早已走远、当时的口径也可能丢了。
    """
    state = tmp_path / "state"
    fixture = _kanban_followup_run_with_real_tracker_lease(tmp_path, state)
    transcript = _write_transcript_entries(
        tmp_path / "delivered.jsonl",
        [
            {"uuid": "u1", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "做点别的"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="delivered_awaiting_agent_decision",
        expires_at="2020-01-01T00:00:00Z",  # 短窗已过
        repository_identity_anchor=fixture["repo"],
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
        repo=str(fixture["repo"]),
        run_dir=str(fixture["run_dir"]),
    )

    invoke(
        {
            "cwd": str(fixture["repo"]),
            "session_id": "session-active",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "继续做用户要求的别的工作。",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "off",  # 交回常规 gate 后不真派发，只验锁与 lease 都被回收
            "KANBAN_TASK_ID": "task-active",
        },
        state_dir=state,
    )

    events = latest_events(state)
    expired = [
        e for e in events
        if e.get("event") == "kanban_followup_awaiting_agent_decision_window_expired"
    ]
    assert len(expired) == 1, events
    assert expired[0].get("tracker_lease_id") == fixture["lease_id"], expired
    assert (expired[0].get("lease_release_result") or {}).get("status") == "released", expired
    assert not marker_path.exists()
    # 真相在 tracker 里：lease 已释放、unit 退回可审池，下一次 Stop 才可能重新提议这批改动。
    assert _tracker_lease_state(fixture["db_path"], fixture["lease_id"]) == "failed-released"
    assert _tracker_unit_count_by_review_state(fixture["db_path"], "assigned") == 0
    assert _tracker_unit_count_by_review_state(fixture["db_path"], "available") > 0


def test_kanban_followup_rewind_reconcile_releases_tracker_lease(tmp_path: Path) -> None:
    """rewind 对账清锁同样必须释放 lease——被撤销的那一轮不该继续占着 scope 到 1h TTL。"""
    state = tmp_path / "state"
    fixture = _kanban_followup_run_with_real_tracker_lease(tmp_path, state)
    transcript = _write_transcript_entries(
        tmp_path / "rewound.jsonl",
        [
            {"uuid": "u0", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "起点"}},
            {"uuid": "u1", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": "RVF_DISPATCH=token=abcdef0123456789 请审"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "开始 review"}},
            {"uuid": "u2", "parentUuid": "u0", "type": "user",
             "message": {"role": "user", "content": "改做别的"}},
        ],
    )
    marker_path = _write_kanban_followup_lock_marker(
        state / "kanban-followup-in-progress",
        task_id="task-active",
        state="in_progress",  # 已升格过，仍应被 rewind 对账连锁带 lease 一起回收
        expires_at="2999-01-01T00:00:00Z",
        repository_identity_anchor=fixture["repo"],
        dispatch_token="abcdef0123456789",
        delivered_transcript_path=str(transcript),
        delivered_session_id="session-active",
        repo=str(fixture["repo"]),
        run_dir=str(fixture["run_dir"]),
    )

    invoke(
        {
            "cwd": str(fixture["repo"]),
            "session_id": "session-active",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_user_message": "继续做用户要求的别的工作。",
        },
        extra_env={"CODEX_RVF_FORK_MODE": "off", "KANBAN_TASK_ID": "task-active"},
        state_dir=state,
    )

    events = latest_events(state)
    reconciled = [
        e for e in events if e.get("event") == "kanban_followup_lock_reconciled_for_rewind"
    ]
    assert len(reconciled) == 1, events
    assert reconciled[0].get("tracker_lease_id") == fixture["lease_id"], reconciled
    assert (reconciled[0].get("lease_release_result") or {}).get("status") == "released", reconciled
    assert not marker_path.exists()
    assert _tracker_lease_state(fixture["db_path"], fixture["lease_id"]) == "failed-released"
    assert _tracker_unit_count_by_review_state(fixture["db_path"], "assigned") == 0


def test_kanban_followup_awaiting_promotion_write_failure_fails_closed_without_nudging(
    tmp_path: Path,
) -> None:
    """升格写盘失败必须 fail-closed：磁盘上锁仍是 awaiting，就绝不能落进 nudge 路径。

    ``promote_marker_out_of_awaiting_agent_decision`` 写盘失败时只返回 None（best-effort 不抛）。
    历史实现照样 ``handled=False`` 把锁交回常规 in_progress 分支 → 对一把「磁盘态仍是未确认接手」
    的锁发 re-engage nudge，正是本轮要根治的那个行为；ledger 里记了 warning，但控制流没被约束。

    本测试带**对照臂**：同一套 fixture 在真升格下确实会 nudge，故失败臂的「不 nudge」是真差异，
    不是因为这条路径本来就到不了 nudge（那样会假绿）。
    """
    module = load_hook_module()
    repo = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = _write_transcript_entries(
        tmp_path / "delivered.jsonl",
        [
            {"uuid": "u1", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "RVF_DISPATCH=token=abcdef0123456789 请审"}},
            {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
             "message": {"role": "assistant", "content": "开始 review"}},
        ],
    )
    marker_dir = state / "kanban-followup-in-progress"

    def _rearm_awaiting_marker() -> Path:
        return _write_kanban_followup_lock_marker(
            marker_dir,
            task_id="task-active",
            state="delivered_awaiting_agent_decision",
            expires_at="2999-01-01T00:00:00Z",
            repository_identity_anchor=repo,
            dispatch_token="abcdef0123456789",
            delivered_transcript_path=str(transcript),
            delivered_session_id="session-active",
            repo=str(repo),
        )

    event = {
        "cwd": str(repo),
        "session_id": "session-active",
        "transcript_path": str(transcript),
        "task_id": "task-active",
        "last_user_message": "继续做用户要求的别的工作。",
    }
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_lock_root = os.environ.get("RVF_KANBAN_FOLLOWUP_LOCK_ROOT")
    # in-process 调用没有 ``invoke`` 的子进程 env 清洗：``current_kanban_task_id`` **先读 env**，
    # 真机上跑测试时那里坐着当前 Kanban task 的 id（实测 7ec31），锁按它的 task-path 去找就会
    # 落空 → marker 读不到 → 整条 awaiting 判定被绕开，测试无声退化成空跑。故显式钉死三个别名。
    task_env_names = ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID")
    old_task_env = {name: os.environ.get(name) for name in task_env_names}
    original_promote = module.promote_kanban_followup_lock_out_of_awaiting
    os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(marker_dir)
    os.environ["KANBAN_TASK_ID"] = "task-active"
    for name in ("CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"):
        os.environ.pop(name, None)
    try:
        ledger = _make_test_ledger(module, state / "log")

        # ① 对照臂：升格成功 → 落回常规 in_progress 分支，nudge 照发（证明本路径可达 nudge）。
        marker_path = _rearm_awaiting_marker()
        control = module.kanban_followup_in_progress_decision(event, ledger, cwd=str(repo))
        assert control is not None and control.reason_code == "kanban_followup_in_progress_reengage"
        assert json.loads(marker_path.read_text(encoding="utf-8"))["state"] == "in_progress"

        # ② 失败臂：升格写盘失败（返回 None）→ 必须自己接管，按 awaiting 语义只互斥去重。
        marker_path = _rearm_awaiting_marker()
        module.promote_kanban_followup_lock_out_of_awaiting = lambda marker, **kwargs: None
        decision = module.kanban_followup_in_progress_decision(event, ledger, cwd=str(repo))
    finally:
        module.promote_kanban_followup_lock_out_of_awaiting = original_promote
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_lock_root is None:
            os.environ.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
        else:
            os.environ["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = old_lock_root
        for name, value in old_task_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert decision is not None, "fail-closed 要求这里自己接管，不能 handled=False 交回常规分支"
    assert decision.reason_code == "kanban_followup_delivered_awaiting_agent_decision"
    # skip 契约：{"continue": ...}，绝不是 force-continue 的 {"decision":"block"}。
    assert "decision" not in (decision.payload or {}), decision.payload
    persisted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "delivered_awaiting_agent_decision"  # 磁盘态未变，下次 Stop 重试升格
    assert persisted.get("reengage_nudge_count") is None  # 预算一次都没被消耗
    events = latest_events(state / "log")
    warned = [
        e for e in events
        if e.get("event") == "kanban_followup_lock_promoted_after_agent_received_trigger"
        and e.get("kanban_followup_lock_promotion_failed_kept_awaiting") is True
    ]
    assert len(warned) == 1, events
    assert warned[0].get("status") == "warning"
