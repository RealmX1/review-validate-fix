#!/usr/bin/env python3
"""``rvf`` 快捷入口（RVF 降本 T1：执行段减停）测试簇。

覆盖：当前 run / 决策 token 的推断优先级、``wait`` / ``results`` 的 canonical 合并摘要、
``fix prepare`` → 子代理 ``fix stop``（cwd 反推）→ ``apply --all-stopped``（遇冲突停下）的端到端、
redispatch 自动带当前 run 目录、shim 安装与命令前缀解析、session id 环境变量名、start 回执的
有序下一步。共享 helper 经 aggregator 的 ``inject()`` 推入。

隔离：每个用例自建 tmp 下的 RVF_HOME / HOME / CODEX_RVF_LOG_ROOT / 锁根，并清掉宿主会话的
``RVF_*`` / ``CLAUDE_CODE_SESSION_ID`` / ``CODEX_THREAD_ID`` 等变量——绝不读写真实 ``~/.rvf``，
也不会被正在跑测试的这个 agent 会话的身份带偏。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

__all__ = [
    "test_rvf_shortcut_run_context_resolution_priority",
    "test_rvf_shortcut_infers_decision_token_for_start_and_dispatch",
    "test_rvf_shortcut_results_summary_reads_canonical_legs_only",
    "test_rvf_shortcut_wait_mode_and_default_duration_by_host",
    "test_rvf_shortcut_fix_prepare_stop_apply_all_stopped_end_to_end",
    "test_rvf_shortcut_redispatch_reviewers_argv_carries_current_run_dir",
    "test_rvf_command_line_shortcut_shim_install_and_invocation_resolution",
    "test_current_agent_session_id_reads_claude_code_session_id",
    "test_agent_cli_start_receipt_renders_intent_status_and_ordered_next_steps",
    "test_rvf_wait_awaiting_marker_keeps_stop_silent_while_waiting_and_reengage_returns_after_exit_kill_or_ttl",
    "test_rvf_wait_awaiting_marker_armed_only_on_hosts_that_wake_main_agent_after_background_work",
    "test_rvf_fix_prepare_arms_awaiting_marker_per_attempt_and_full_stop_command_works_from_any_cwd",
    "test_rvf_validate_inside_fix_attempt_worktree_or_suppressed_subprocess_stays_off_main_run",
    "test_rvf_land_passthrough_commit_defaults_to_most_recent_prepare_run_of_this_repository",
    "test_rvf_apply_orders_attempts_by_created_at_without_baseline_patch_mtime_fallback",
    "test_rvf_receipts_carry_explicit_run_dir_only_when_current_session_is_unresolvable",
    "test_rvf_execution_segment_docs_teach_rvf_validate_background_timeout_and_no_polling_protocol",
]

HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CURSOR_AGENT",
    "AI_AGENT",
)


def inject(**deps: object) -> None:
    globals().update(deps)


def _entrypoint_script() -> Path:
    return SCRIPT_DIR / "rvf_command_line_shortcut_entrypoint.py"  # noqa: F821 - injected


def _isolated_environment(root: Path, **overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_"))
        and key not in HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES
    }
    environment.update(
        {
            "HOME": str(root / "home"),
            "RVF_HOME": str(root / "rvf-home"),
            "CODEX_RVF_LOG_ROOT": str(root / "run-logs"),
            "RVF_USER_LOCAL_DATA_ROOT": str(root / "user-local-data"),
            "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(root / "in-flight-lock"),
        }
    )
    environment.update(overrides)
    (root / "home").mkdir(parents=True, exist_ok=True)
    return environment


@contextlib.contextmanager
def _process_environment_and_cwd(environment: dict[str, str], cwd: Path) -> Iterator[None]:
    saved_environment = dict(os.environ)
    saved_cwd = Path.cwd()
    os.environ.clear()
    os.environ.update(environment)
    os.chdir(cwd)
    try:
        yield
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_environment)


@contextlib.contextmanager
def _only_these_host_session_environment_variables(**values: str) -> Iterator[None]:
    """进程内测试：临时抹掉宿主会话身份变量，只留下 ``values``（回执是否带 --run-dir 取决于它）。"""
    names = (*HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES, "RVF_SESSION_ID")
    saved = {name: os.environ.get(name) for name in (*names, *values)}
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _wait_until(predicate: Callable[[], bool], *, timeout_seconds: float, what: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout_seconds}s 内未等到：{what}")
        time.sleep(0.05)


def _load_entrypoint_module():
    import importlib  # noqa: PLC0415

    if str(SCRIPT_DIR) not in sys.path:  # noqa: F821
        sys.path.insert(0, str(SCRIPT_DIR))  # noqa: F821
    return importlib.import_module("rvf_command_line_shortcut_entrypoint")


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "rvf@example.test"],
        ["git", "config", "user.name", "RVF Test"],
    ):
        subprocess.run(argv, cwd=path, check=True, capture_output=True)
    return path


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, capture_output=True)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_decision_token(rvf_home: Path, *, token: str, state: str, worktree: Path, session_id: str,
                          minted_at: str) -> None:
    _write_json(
        rvf_home / "agentic-initiation-decision-tokens" / f"{token}.json",
        {"schema_version": 1, "token": token, "state": state, "worktree": str(worktree),
         "session_id": session_id, "minted_at": minted_at, "created_at": minted_at},
    )


def _write_scope_overlay(rvf_home: Path, *, token: str, worktree: Path, session_id: str, run_dir: Path,
                         updated_at: str, extra: dict | None = None) -> None:
    _write_json(
        rvf_home / "agentic-initiation-scope-adjustments" / f"{token}.json",
        {"schema_version": 1, "token": token, "worktree": str(worktree), "repo": str(worktree),
         "session_id": session_id, "run_id": run_dir.name, "run_dir": str(run_dir),
         "updated_at": updated_at, "created_at": updated_at, **(extra or {})},
    )


def test_rvf_shortcut_run_context_resolution_priority(root: Path) -> None:
    """显式 > RVF_RUN_DIR > attempt worktree cwd > in-flight marker > worktree 最近已接受 token。"""
    module = _load_entrypoint_module()
    worktree = _git_repo(root / "worktree")
    environment = _isolated_environment(root, RVF_SESSION_ID="sess-mine")
    rvf_home = Path(environment["RVF_HOME"])
    old_run, new_run, other_session_run = (root / "runs" / name for name in ("run-old", "run-new", "run-other"))
    for run in (old_run, new_run, other_session_run):
        (run / "artifacts").mkdir(parents=True, exist_ok=True)
    _write_decision_token(rvf_home, token="aaaaaaaaaaaaaaaa", state="dispatched", worktree=worktree,
                          session_id="sess-mine", minted_at="2020-01-01T01:00:00Z")
    _write_scope_overlay(rvf_home, token="aaaaaaaaaaaaaaaa", worktree=worktree, session_id="sess-mine",
                         run_dir=old_run, updated_at="2020-01-01T01:00:00Z")
    _write_decision_token(rvf_home, token="bbbbbbbbbbbbbbbb", state="acknowledged", worktree=worktree,
                          session_id="sess-mine", minted_at="2020-01-01T02:00:00Z")
    _write_scope_overlay(rvf_home, token="bbbbbbbbbbbbbbbb", worktree=worktree, session_id="sess-mine",
                         run_dir=new_run, updated_at="2020-01-01T02:00:00Z")
    # 别的会话、更新：不得被本会话捡走。
    _write_decision_token(rvf_home, token="cccccccccccccccc", state="acknowledged", worktree=worktree,
                          session_id="sess-other", minted_at="2020-01-01T03:00:00Z")
    _write_scope_overlay(rvf_home, token="cccccccccccccccc", worktree=worktree, session_id="sess-other",
                         run_dir=other_session_run, updated_at="2020-01-01T03:00:00Z")

    with _process_environment_and_cwd(environment, worktree):
        latest = module.resolve_current_rvf_run_context()
        assert latest.resolution_source == module.RUN_CONTEXT_SOURCE_WORKTREE_LATEST_DECISION_TOKEN, latest
        assert latest.run_dir == new_run and latest.decision_token == "bbbbbbbbbbbbbbbb", latest

        explicit = module.resolve_current_rvf_run_context(explicit_run_dir=str(old_run))
        assert explicit.resolution_source == module.RUN_CONTEXT_SOURCE_EXPLICIT_RUN_DIR
        assert explicit.decision_token == "aaaaaaaaaaaaaaaa", explicit

        # in-flight marker（start 时 arm 的那枚）压过「worktree 最近」。
        import kanban_followup_lock  # noqa: PLC0415

        kanban_followup_lock.write_marker(
            task_id=None, session_id="sess-mine", run_id=old_run.name, run_dir=str(old_run),
            repo=str(worktree), cwd=str(worktree), decision_token="aaaaaaaaaaaaaaaa",
            state=kanban_followup_lock.STATE_IN_PROGRESS,
        )
        marker = module.resolve_current_rvf_run_context()
        assert marker.resolution_source == module.RUN_CONTEXT_SOURCE_IN_FLIGHT_MARKER, marker
        assert marker.run_dir == old_run and marker.decision_token == "aaaaaaaaaaaaaaaa", marker

        os.environ["RVF_RUN_DIR"] = str(new_run)
        from_env = module.resolve_current_rvf_run_context()
        assert from_env.resolution_source == module.RUN_CONTEXT_SOURCE_RVF_RUN_DIR_ENV, from_env
        assert from_env.run_dir == new_run.resolve()
        del os.environ["RVF_RUN_DIR"]

    # attempt worktree cwd：路径本身编码了 run 与 attempt。
    attempt_worktree = new_run / "artifacts" / "fix-attempts" / "rvf-attempt-x" / "worktree" / "nested"
    attempt_worktree.mkdir(parents=True)
    _write_json(new_run / "artifacts" / "fix-attempts" / "rvf-attempt-x" / "attempt.json", {"attempt_id": "rvf-attempt-x"})
    with _process_environment_and_cwd(environment, attempt_worktree):
        from_attempt = module.resolve_current_rvf_run_context()
        assert from_attempt.resolution_source == module.RUN_CONTEXT_SOURCE_ATTEMPT_WORKTREE_CWD, from_attempt
        assert from_attempt.run_dir.resolve() == new_run.resolve()

    # 什么都没有：诚实报「找不到」，不猜。
    bare = _git_repo(root / "bare-worktree")
    with _process_environment_and_cwd(_isolated_environment(root / "bare", RVF_SESSION_ID="s"), bare):
        missing = module.resolve_current_rvf_run_context()
        assert missing.run_dir is None and missing.notes, missing

    # RVF-001 回归：in-flight marker 只认「未过期 + 已接受态（in_progress / waiting_bootstrap）」，
    # 且本会话 session 键（``rvf start`` 只写这一键）优先于 kanban task 键；不合格就落到下一层。
    with _process_environment_and_cwd(environment, worktree):
        import kanban_followup_lock  # noqa: PLC0415

        def arm_in_flight_marker(*, task_id: str | None, session_id: str | None, run_dir: Path,
                                 decision_token: str, state: str) -> Path:
            marker_path = kanban_followup_lock.write_marker(
                task_id=task_id, session_id=session_id, run_id=run_dir.name, run_dir=str(run_dir),
                repo=str(worktree), cwd=str(worktree), decision_token=decision_token, state=state,
            )
            assert marker_path is not None
            return marker_path

        session_marker_path = arm_in_flight_marker(
            task_id=None, session_id="sess-mine", run_dir=old_run, decision_token="aaaaaaaaaaaaaaaa",
            state=kanban_followup_lock.STATE_IN_PROGRESS,
        )
        expired_marker_payload = json.loads(session_marker_path.read_text(encoding="utf-8"))
        expired_marker_payload["expires_at"] = "2020-01-01T00:00:00Z"
        session_marker_path.write_text(json.dumps(expired_marker_payload), encoding="utf-8")
        expired = module.resolve_current_rvf_run_context()
        assert expired.resolution_source == module.RUN_CONTEXT_SOURCE_WORKTREE_LATEST_DECISION_TOKEN, expired
        assert expired.run_dir == new_run and expired.decision_token == "bbbbbbbbbbbbbbbb", expired

        arm_in_flight_marker(
            task_id=None, session_id="sess-mine", run_dir=old_run, decision_token="aaaaaaaaaaaaaaaa",
            state=kanban_followup_lock.STATE_DELIVERED_AWAITING_AGENT_DECISION,
        )
        unaccepted = module.resolve_current_rvf_run_context()
        assert unaccepted.resolution_source == module.RUN_CONTEXT_SOURCE_WORKTREE_LATEST_DECISION_TOKEN, unaccepted
        assert unaccepted.run_dir == new_run, unaccepted

        os.environ["KANBAN_TASK_ID"] = "task-rvf-001"
        arm_in_flight_marker(
            task_id="task-rvf-001", session_id=None, run_dir=other_session_run,
            decision_token="cccccccccccccccc", state=kanban_followup_lock.STATE_IN_PROGRESS,
        )
        arm_in_flight_marker(
            task_id=None, session_id="sess-mine", run_dir=old_run, decision_token="aaaaaaaaaaaaaaaa",
            state=kanban_followup_lock.STATE_IN_PROGRESS,
        )
        session_preferred = module.resolve_current_rvf_run_context()
        assert session_preferred.resolution_source == module.RUN_CONTEXT_SOURCE_IN_FLIGHT_MARKER, session_preferred
        assert session_preferred.run_dir == old_run, session_preferred
        assert session_preferred.decision_token == "aaaaaaaaaaaaaaaa", session_preferred

        # 本会话 session 键不合格时，合格的 task 键 marker 才兜底。
        arm_in_flight_marker(
            task_id=None, session_id="sess-mine", run_dir=old_run, decision_token="aaaaaaaaaaaaaaaa",
            state=kanban_followup_lock.STATE_DELIVERED_AWAITING_AGENT_DECISION,
        )
        task_fallback = module.resolve_current_rvf_run_context()
        assert task_fallback.resolution_source == module.RUN_CONTEXT_SOURCE_IN_FLIGHT_MARKER, task_fallback
        assert task_fallback.run_dir == other_session_run, task_fallback
        del os.environ["KANBAN_TASK_ID"]


def test_rvf_shortcut_infers_decision_token_for_start_and_dispatch(root: Path) -> None:
    """start / decline 从 token store 挑本会话本 worktree 最新待表态 token；dispatch 只认 acknowledged。"""
    module = _load_entrypoint_module()
    worktree = _git_repo(root / "worktree")
    environment = _isolated_environment(root, RVF_SESSION_ID="sess-mine")
    rvf_home = Path(environment["RVF_HOME"])
    _write_decision_token(rvf_home, token="1111111111111111", state="declined", worktree=worktree,
                          session_id="sess-mine", minted_at="2020-01-01T05:00:00Z")
    _write_decision_token(rvf_home, token="2222222222222222", state="injected", worktree=worktree,
                          session_id="sess-mine", minted_at="2020-01-01T04:00:00Z")
    _write_decision_token(rvf_home, token="3333333333333333", state="injected", worktree=worktree,
                          session_id="sess-other", minted_at="2020-01-01T06:00:00Z")
    with _process_environment_and_cwd(environment, worktree):
        token, how = module.infer_decision_token_for_subcommand("start")
        assert token == "2222222222222222", (token, how)
        token, how = module.infer_decision_token_for_subcommand("dispatch")
        assert token is None, (token, how)

        run_dir = root / "runs" / "run-a"
        (run_dir / "artifacts").mkdir(parents=True)
        _write_decision_token(rvf_home, token="2222222222222222", state="acknowledged", worktree=worktree,
                              session_id="sess-mine", minted_at="2020-01-01T04:00:00Z")
        _write_scope_overlay(rvf_home, token="2222222222222222", worktree=worktree, session_id="sess-mine",
                             run_dir=run_dir, updated_at="2020-01-01T04:10:00Z")
        token, how = module.infer_decision_token_for_subcommand("dispatch")
        assert token == "2222222222222222", (token, how)

    # 端到端：`rvf decline` 不带 --token 也能落到正确的 token 上（真实 CLI、真实铸造的 token）。
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    with _process_environment_and_cwd(environment, worktree):
        minted = dts.mint_decision_token(
            worktree=str(worktree), session_id="sess-mine", candidate_fingerprint="fp-e2e-decline",
            candidate_summary={"candidates": []},
        ).token
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "decline", "--reason", "测试谢绝"],
        cwd=worktree, env=environment, capture_output=True, text=True, check=False,
    )
    output = completed.stdout + completed.stderr
    assert f"token {minted}" in output, output
    stored = json.loads((rvf_home / "agentic-initiation-decision-tokens" / f"{minted}.json").read_text())
    assert stored["state"] == "declined", (stored, output)

    # RVF-002 回归：解析不出当前会话 id（Kimi / shell 未注入会话变量）时 fail-closed——
    # 不得捡走同 worktree 上别的会话最新的 token / overlay，只认无会话归属的记录。
    session_unknown_environment = {key: value for key, value in environment.items() if key != "RVF_SESSION_ID"}
    with _process_environment_and_cwd(session_unknown_environment, worktree):
        assert module.resolve_current_agent_session_id_from_environment() is None
        token, how = module.infer_decision_token_for_subcommand("start")
        assert token is None, (token, how)  # 旧行为：捡走 sess-other 的 3333
        assert "会话 id" in how, how
        token, how = module.infer_decision_token_for_subcommand("dispatch")
        assert token is None, (token, how)  # 旧行为：落到 sess-mine 的 run-a
        unresolved = module.resolve_current_rvf_run_context()
        assert unresolved.run_dir is None and unresolved.notes, unresolved
        assert "会话 id 未知" in "；".join(unresolved.notes), unresolved.notes

        _write_decision_token(rvf_home, token="4444444444444444", state="injected", worktree=worktree,
                              session_id="", minted_at="2020-01-01T03:00:00Z")
        token, how = module.infer_decision_token_for_subcommand("start")
        assert token == "4444444444444444", (token, how)


def _write_reviewer_leg(reviewers: Path, reviewer_id: str, *, result: dict | None, returncode: int) -> dict:
    leg_directory = reviewers / reviewer_id
    leg_directory.mkdir(parents=True, exist_ok=True)
    result_path = leg_directory / "review-result.json"
    if result is not None:
        _write_json(result_path, result)
    (leg_directory / "reviewer.stderr.txt").write_text(f"{reviewer_id} stderr\n", encoding="utf-8")
    return {"slot": 1, "harness_id": reviewer_id, "reviewer_id": reviewer_id, "returncode": returncode,
            "review_result_path": str(result_path)}


def test_rvf_shortcut_results_summary_reads_canonical_legs_only(root: Path) -> None:
    """摘要只读 canonical review-result.json（superseded 归档不进摘要），失败腿给 stderr 路径，判定只看 plan_status。"""
    run_dir = root / "run"
    reviewers = run_dir / "artifacts" / "reviewers"
    issue = {"path": "a.py", "line": 3, "kind": "REAL", "severity": "high", "message": "边界条件漏了负数"}
    legs = [
        _write_reviewer_leg(reviewers, "cursor-cli", returncode=0,
                            result={"schema_version": 1, "kind": "issues", "issues": [issue], "requests": []}),
        _write_reviewer_leg(reviewers, "kimi-code", returncode=0,
                            result={"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [],
                                    "audit_summary": "审了 a.py 的全部分支"}),
    ]
    # superseded 归档里的旧结论不得泄漏进摘要。
    _write_json(reviewers / "kimi-code" / "superseded-review-result-artifacts" / "review-result.json",
                {"schema_version": 1, "kind": "issues", "issues": [{**issue, "message": "SUPERSEDED_OLD_FINDING"}]})
    _write_json(reviewers / "reviewer-plan.json",
                {"status": "completed", "effective_reviewer_count": 2, "reviewers": legs, "fallbacks": []})
    _write_json(reviewers / ".dispatch-thread.status.json",
                {"launch_status": "launched", "returncode": 0, "finished_at": "2020-01-01T00:00:00Z"})

    environment = _isolated_environment(root)
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "wait", "--run-dir", str(run_dir), "--max-wait", "0"],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    assert "plan_status=completed 有效腿=2/2" in output, output
    assert "[high/REAL] a.py:3 — 边界条件漏了负数" in output, output
    assert "audit_summary: 审了 a.py 的全部分支" in output, output
    assert "SUPERSEDED_OLD_FINDING" not in output, output
    assert "双评审成立" in output and "fix prepare" in output, output

    # 失败腿：rc≠0、无 review-result → 摘要给出 stderr 路径，判定不成立。
    legs[1] = _write_reviewer_leg(reviewers, "claude-code", returncode=1, result=None)
    _write_json(reviewers / "reviewer-plan.json",
                {"status": "failed", "reason": "reviewer_leg_produced_no_review_result",
                 "effective_reviewer_count": 1, "reviewers": legs,
                 "fallbacks": [{"slot": 2, "from": "kimi_code", "to": "claude_code", "reason": "no_review_result"}]})
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "results", "--run-dir", str(run_dir)],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    output = completed.stdout
    assert "plan_status=failed" in output and "reviewer_leg_produced_no_review_result" in output, output
    assert str(reviewers / "claude-code" / "reviewer.stderr.txt") in output, output
    assert "fallback: slot2 kimi_code→claude_code" in output, output
    assert "不成立" in output and "redispatch-reviewers" in output, output

    # --json 形态：机器可读、同一份事实。
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "results", "--run-dir", str(run_dir), "--json"],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    payload = json.loads(completed.stdout)
    assert payload["summary"]["plan_status"] == "failed"
    assert [leg["kind"] for leg in payload["summary"]["legs"]] == ["issues", None]
    assert payload["summary"]["dual_review_established_from_canonical_legs"] is False, payload

    # plan 缓存仍写 completed / 有效腿=2/2，但一腿 canonical review-result 事后缺失、另一腿损坏：
    # 必须 fail-close 为不成立，不得走 clean path / fix prepare。
    stale_legs = [
        _write_reviewer_leg(reviewers, "cursor-cli", returncode=0, result=None),
        _write_reviewer_leg(reviewers, "kimi-code", returncode=0, result=None),
    ]
    (reviewers / "cursor-cli" / "review-result.json").write_text("{not json", encoding="utf-8")
    (reviewers / "kimi-code" / "review-result.json").unlink(missing_ok=True)
    _write_json(reviewers / "reviewer-plan.json",
                {"status": "completed", "effective_reviewer_count": 2, "reviewers": stale_legs, "fallbacks": []})
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "results", "--run-dir", str(run_dir)],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    output = completed.stdout
    assert "plan_status=completed 有效腿=2/2" in output, output
    assert "双评审成立" not in output and "clean path" not in output and "fix prepare" not in output, output
    assert "双评审**不成立**" in output and "redispatch-reviewers" in output, output
    assert "腿 cursor-cli 当前 canonical review-result 不是合法的" in output, output
    assert "腿 kimi-code 当前 canonical review-result 不是合法的" in output, output
    completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "results", "--run-dir", str(run_dir), "--json"],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    payload = json.loads(completed.stdout)
    assert payload["summary"]["dual_review_established_from_canonical_legs"] is False, payload
    assert len(payload["summary"]["dual_review_not_established_reasons"]) == 2, payload


def test_rvf_shortcut_wait_mode_and_default_duration_by_host(root: Path) -> None:
    """Claude → 后台等完成通知、Codex → 单次长 exec 会话、其它 host → 有界轮询；默认等待时长随之变。"""
    del root
    from core.host_adapter import current_agent_session_identity_from_environment as identity  # noqa: PLC0415

    module = _load_entrypoint_module()
    claude = identity.resolve_current_host_reviewer_wait_mode({"CLAUDE_CODE_SESSION_ID": "x"})
    codex_thread = identity.resolve_current_host_reviewer_wait_mode({"CODEX_THREAD_ID": "t"})
    codex_session = identity.resolve_current_host_reviewer_wait_mode({"CODEX_SESSION_ID": "t"})
    kimi = identity.resolve_current_host_reviewer_wait_mode({"CODEX_RVF_CLINE_KANBAN_AGENT_ID": "kimi"})
    unknown = identity.resolve_current_host_reviewer_wait_mode({})
    assert claude == identity.REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION
    assert codex_thread == codex_session == identity.REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD
    assert kimi == unknown == identity.REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING

    import dispatch_reviewers  # noqa: PLC0415

    backstop = dispatch_reviewers.DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS
    assert module.default_reviewer_wait_seconds_for_host(claude) > backstop
    assert module.default_reviewer_wait_seconds_for_host(codex_thread) > backstop
    assert module.default_reviewer_wait_seconds_for_host(kimi) < 600  # 前台工具上限之内

    assert "run_in_background" in module.render_host_specific_reviewer_wait_instruction("rvf", claude)
    assert "write_stdin" in module.render_host_specific_reviewer_wait_instruction("rvf", codex_thread)
    bounded = module.render_host_specific_reviewer_wait_instruction("rvf", kimi)
    assert "再调一次" in bounded and "run_in_background" not in bounded


def test_rvf_shortcut_fix_prepare_stop_apply_all_stopped_end_to_end(root: Path) -> None:
    """fix prepare（upsert+prepare+start 一步）→ 子代理在 attempt worktree 里 `fix stop`（不传任何 id）→
    `apply --all-stopped` 按创建顺序合回，遇冲突停下并报出剩余未合回的 attempt。"""
    repo = _git_repo(root / "repo")
    (repo / "a.txt").write_text("line-1\nline-2\n", encoding="utf-8")
    (repo / "c.txt").write_text("c-1\n", encoding="utf-8")
    _commit_all(repo, "base")
    run_dir = root / "runs" / "rvf-run-e2e"
    _write_json(run_dir / "summary.json", {"run_id": "rvf-run-e2e", "repo": str(repo)})
    _write_json(run_dir / "artifacts" / "inputs" / "scope.contract.json",
                {"run_id": "rvf-run-e2e", "repo": str(repo), "fix_allowlist": ["a.txt", "c.txt"],
                 "scope_hash": "sha256:" + "0" * 64, "protected_files": [], "background_files": [],
                 "excluded_path_prefixes": []})
    issue_files = [
        _write_json(root / "issues" / f"{issue_id}.json",
                    {"issue_id": issue_id, "path": path, "line": 1, "message": message, "run_id": "rvf-run-e2e"})
        for issue_id, path, message in (
            ("RVF-001", "a.txt", "line-2 写错了"),
            ("RVF-002", "a.txt", "line-2 另一种改法"),
            ("RVF-003", "c.txt", "c 也要改"),
        )
    ]
    environment = _isolated_environment(root)
    # 合并处置是 fix prepare 的前置闸：没写处置时 prepare 必须拒绝；`rvf merge write` 从当前 run 补 --run-dir。
    reviewers = run_dir / "artifacts" / "reviewers"
    legs = [
        _write_reviewer_leg(reviewers, reviewer_id, returncode=0, result={
            "schema_version": 1, "kind": "issues", "requests": [],
            "issues": [{"path": path, "line": 1, "kind": "REAL", "severity": "medium", "message": message}
                       for path, message in issues]})
        for reviewer_id, issues in (
            ("cursor-cli", (("a.txt", "line-2 错"), ("a.txt", "line-2 可另改"))),
            ("kimi-code", (("c.txt", "c 要改"), ("a.txt", "line-2 同根因"))),
        )
    ]
    _write_json(reviewers / "reviewer-plan.json",
                {"status": "completed", "effective_reviewer_count": 2, "reviewers": legs, "fallbacks": []})
    refused = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", str(issue_files[0]),
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert refused.returncode != 0 and "merge dispositions are missing" in refused.stderr, refused.stderr
    merged = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "merge", "write"],
        input=json.dumps({"dispositions": [
            {"reviewer_id": "cursor-cli", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
            {"reviewer_id": "cursor-cli", "index": 2, "disposition": "adopted", "issue_id": "RVF-002"},
            {"reviewer_id": "kimi-code", "index": 1, "disposition": "adopted", "issue_id": "RVF-003"},
            {"reviewer_id": "kimi-code", "index": 2, "disposition": "merged_into", "issue_id": "RVF-001"},
        ]}),
        cwd=repo, env={**environment, "RVF_RUN_DIR": str(run_dir)}, capture_output=True, text=True, check=False,
    )
    assert merged.returncode == 0, merged.stderr + merged.stdout
    assert (run_dir / "artifacts" / "merge" / "merge_dispositions.json").is_file()
    prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", *map(str, issue_files),
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    attempts = json.loads(prepared.stdout)["prepared"]
    assert [item["issue_id"] for item in attempts] == ["RVF-001", "RVF-002", "RVF-003"]
    checked = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "merge", "check", "--run-dir", str(run_dir)],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert checked.returncode == 0, checked.stderr + checked.stdout
    for item in attempts:
        assert item["scope_hash"] == "sha256:" + "0" * 64
        assert item["fix_allowlist"] == ["a.txt", "c.txt"]
        attempt = json.loads((Path(item["attempt_worktree"]).parent / "attempt.json").read_text())
        assert attempt["status"] == "started", attempt  # prepare 已把 start 合进来
        assert attempt.get("created_at"), attempt

    edits = {"RVF-001": ("a.txt", "line-1\nline-2-fixed-by-001\n"),
             "RVF-002": ("a.txt", "line-1\nline-2-fixed-by-002\n"),
             "RVF-003": ("c.txt", "c-1-fixed\n")}
    for item in attempts:
        worktree = Path(item["attempt_worktree"])
        relative, text = edits[item["issue_id"]]
        (worktree / relative).write_text(text, encoding="utf-8")
        stopped = subprocess.run(
            [sys.executable, str(_entrypoint_script()), "fix", "stop", "--status", "fixed"],
            cwd=worktree, env=environment, capture_output=True, text=True, check=False,
        )
        assert stopped.returncode == 0, stopped.stderr + stopped.stdout
        assert json.loads(stopped.stdout)["attempt_id"] == item["attempt_id"]

    applied = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--all-stopped", "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert applied.returncode == 3, applied.stderr + applied.stdout
    report = json.loads(applied.stdout)
    assert [o["status"] for o in report["outcomes"]] == ["applied", "merge_conflict"], report
    assert report["remaining_not_applied"] == [attempts[2]["attempt_id"]], report
    assert (repo / "a.txt").read_text(encoding="utf-8") == "line-1\nline-2-fixed-by-001\n"
    assert (repo / "c.txt").read_text(encoding="utf-8") == "c-1\n"  # 冲突后停下，没有继续合

    # agent 处置完冲突后，剩下那条单独合回。
    single = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--attempt-id", attempts[2]["attempt_id"],
         "--run-dir", str(run_dir)],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert single.returncode == 0, single.stderr + single.stdout
    assert (repo / "c.txt").read_text(encoding="utf-8") == "c-1-fixed\n"
    # 单独点名合回时，未点名的遗留冲突照样可见。
    assert attempts[1]["attempt_id"] in single.stdout and "未解决的合回冲突" in single.stdout, single.stdout

    # 再跑一次 --all-stopped：已无 fixed 可合，但 RVF-002 的冲突仍未解决 → 必须继续报出且不得以 0 退出。
    rerun = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--all-stopped", "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert rerun.returncode == 3, rerun.stderr + rerun.stdout
    rerun_report = json.loads(rerun.stdout)
    assert rerun_report["outcomes"] == [], rerun_report
    assert rerun_report["unresolved_apply_conflict_attempts"] == [
        {"attempt_id": attempts[1]["attempt_id"], "issue_id": "RVF-002", "status": "merge_conflict"}
    ], rerun_report
    rerun_text = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--all-stopped", "--run-dir", str(run_dir)],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert rerun_text.returncode == 3, rerun_text.stderr + rerun_text.stdout
    assert attempts[1]["attempt_id"] in rerun_text.stdout and "merge_conflict" in rerun_text.stdout, rerun_text.stdout


def test_rvf_shortcut_redispatch_reviewers_argv_carries_current_run_dir(root: Path) -> None:
    """重派必须带当前 run 目录（否则 dispatcher 静默新建 run）；request-only 与 require-external 一并透传。"""
    module = _load_entrypoint_module()
    run_dir = root / "run"
    for relative in ("artifacts/review-packet.md", "artifacts/scope-of-work.md", "artifacts/inputs/scope.contract.json"):
        (run_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / relative).write_text("{}", encoding="utf-8")
    context = module.ResolvedRvfRunContext(
        run_dir=run_dir, resolution_source="test",
        scope_adjustment_overlay={"run_id": "rvf-x", "repo": str(root / "repo"),
                                  "dispatched_preset": {"external_reviewer_count_target": 2}},
    )
    argv = module.build_redispatch_reviewers_argv(
        context, review_request_legs_only=True, dry_run=False, selection_seed=None
    )
    assert argv[argv.index("--rvf-run-dir") + 1] == str(run_dir), argv
    assert argv[argv.index("--rvf-run-id") + 1] == "rvf-x", argv
    assert "--redispatch-review-request-legs-only" in argv and "--require-external" in argv, argv
    assert "--execute" in argv and "--detached" in argv, argv
    assert argv[argv.index("--review-packet") + 1] == str(run_dir / "artifacts/review-packet.md"), argv

    # `rvf validate` 同样必须尊重显式参数：干净 shell（不在 git 仓内、无 RVF_RUN_DIR）里
    # `--help` 直接透传、显式 `--rvf-run-dir` 用来解析 context，都不得 exit 2。
    validate_repo = _git_repo(root / "validate-repo")
    (validate_repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit_all(validate_repo, "base")
    _write_json(run_dir / "summary.json", {"run_id": "rvf-x", "repo": str(validate_repo)})
    clean_shell_cwd = root / "not-a-git-worktree"
    clean_shell_cwd.mkdir(parents=True, exist_ok=True)
    clean_shell_environment = _isolated_environment(root)
    assert "RVF_RUN_DIR" not in clean_shell_environment
    help_completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", "--help"],
        cwd=clean_shell_cwd, env=clean_shell_environment, capture_output=True, text=True, check=False,
    )
    assert help_completed.returncode == 0, help_completed.stderr + help_completed.stdout
    assert "--rvf-run-dir" in help_completed.stdout, help_completed.stdout
    explicit_completed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", "--rvf-run-dir", str(run_dir),
         "--name", "x", "--", "true"],
        cwd=clean_shell_cwd, env=clean_shell_environment, capture_output=True, text=True, check=False,
    )
    assert explicit_completed.returncode == 0, explicit_completed.stderr + explicit_completed.stdout
    assert '"cache_status": "executed"' in explicit_completed.stdout, explicit_completed.stdout


def test_rvf_command_line_shortcut_shim_install_and_invocation_resolution(root: Path) -> None:
    """部署装 shim + PATH 链接；渲染侧 `rvf` > shim 绝对路径 > 入口脚本；别人的同名命令不覆盖也不认。"""
    import importlib.util  # noqa: PLC0415

    from core import rvf_command_line_shortcut_shim_location as shim_location  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "rvf_local_deploy_for_shim_test", ROOT / "scripts" / "rvf_local_deploy.py"  # noqa: F821
    )
    deploy = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = deploy  # dataclass 装饰器要按 __module__ 回查模块
    spec.loader.exec_module(deploy)

    home = root / "home"
    payload = root / "payload"
    entrypoint = payload / "skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("print('stub')\n", encoding="utf-8")
    fallback = Path("/opt/rvf/scripts/rvf_command_line_shortcut_entrypoint.py")

    assert shim_location.resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=fallback, environ={"PATH": ""}, home=home,
    ).startswith("python3 "), "未部署时退到入口脚本"

    missing = deploy.install_rvf_command_line_shortcut_shim(
        home=home, deployed_plugin_payload_root=None, python_executable=sys.executable
    )
    assert missing["status"] == deploy.RVF_COMMAND_LINE_SHORTCUT_SHIM_SKIPPED_PAYLOAD_ROOT_UNRESOLVED

    installed = deploy.install_rvf_command_line_shortcut_shim(
        home=home, deployed_plugin_payload_root=payload, python_executable=sys.executable
    )
    shim_path = home / ".rvf/bin/rvf"
    link_path = home / ".local/bin/rvf"
    assert installed["status"] == deploy.RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALLED, installed
    assert shim_location.is_rvf_owned_command_line_shortcut_shim(shim_path)
    assert str(entrypoint) in shim_path.read_text(encoding="utf-8")
    assert os.access(shim_path, os.X_OK)
    assert link_path.is_symlink() and link_path.resolve() == shim_path.resolve()
    # shim 真能跑：经 PATH 调 `rvf` 落到入口脚本。
    ran = subprocess.run(["rvf"], env={"PATH": f"{link_path.parent}:/usr/bin:/bin"}, capture_output=True,
                         text=True, check=False)
    assert ran.stdout.strip() == "stub", (ran.stdout, ran.stderr)

    on_path = shim_location.resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=fallback, environ={"PATH": str(link_path.parent)}, home=home,
    )
    assert on_path == "rvf", on_path
    off_path = shim_location.resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=fallback, environ={"PATH": ""}, home=home,
    )
    assert off_path == str(shim_path), off_path

    # 重复部署幂等；PATH 上已有别人的 `rvf` 时不覆盖，渲染侧也不认它。
    again = deploy.install_rvf_command_line_shortcut_shim(
        home=home, deployed_plugin_payload_root=payload, python_executable=sys.executable
    )
    assert again["user_path_link_status"] == deploy.RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_CREATED_OR_REFRESHED
    link_path.unlink()
    link_path.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    link_path.chmod(0o755)
    foreign = deploy.install_rvf_command_line_shortcut_shim(
        home=home, deployed_plugin_payload_root=payload, python_executable=sys.executable
    )
    assert foreign["user_path_link_status"] == deploy.RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_SKIPPED_FOREIGN_COMMAND
    assert "foreign" in link_path.read_text(encoding="utf-8")
    assert shim_location.resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=fallback, environ={"PATH": str(link_path.parent)}, home=home,
    ) == str(shim_path)


def test_current_agent_session_id_reads_claude_code_session_id(root: Path) -> None:
    """Claude Code 实际注入的是 CLAUDE_CODE_SESSION_ID；历史读的 CLAUDE_SESSION_ID 从来不存在。"""
    del root
    from core.host_adapter.current_agent_session_identity_from_environment import (  # noqa: PLC0415
        resolve_current_agent_session_id_from_environment as resolve,
    )

    assert resolve({"CLAUDE_CODE_SESSION_ID": "claude-uuid"}) == "claude-uuid"
    assert resolve({"CLAUDE_SESSION_ID": "never-set-by-claude"}) is None
    assert resolve({"CODEX_THREAD_ID": "codex-thread"}) == "codex-thread"
    assert resolve({"RVF_SESSION_ID": "explicit", "CLAUDE_CODE_SESSION_ID": "claude-uuid"}) == "explicit"

    source = (SCRIPT_DIR / "seal_round_baseline_to_head.py").read_text(encoding="utf-8")  # noqa: F821
    assert "resolve_current_agent_session_id_from_environment" in source
    assert '"CLAUDE_SESSION_ID"' not in source


def test_agent_cli_start_receipt_renders_intent_status_and_ordered_next_steps(root: Path) -> None:
    """start 回执：人类可读行带 intent_artifact_status_file；下一步按顺序排好、参数已填好，且不再出现 rvf-agent。"""
    cli = load_rvf_agent_decision_cli_module()  # noqa: F821
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _agent_cli_roots(cli, root)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    worktree = _agent_cli_worktree(root)  # noqa: F821
    token = _mint_agent_decision_token(  # noqa: F821
        dts, roots, worktree=worktree, now=t0,
        candidate_summary={"strictness_reference": {"suggested_strictness_tier": "light"},
                           "candidates": [{"path": "a.py", "included_in_candidate_set": True}]},
    )
    stub = _agent_cli_stub_prepare(root)  # noqa: F821

    def _prepare_with_intent_status(**kwargs):
        prepared = stub(**kwargs)
        intent = Path(prepared["run_dir"]) / "artifacts" / "base-task-intent-reconciliation-status.json"
        intent.write_text("{}", encoding="utf-8")
        prepared["artifacts"]["intent_artifact_status_file"] = str(intent)
        return prepared

    with _only_these_host_session_environment_variables(CLAUDE_CODE_SESSION_ID="sess-receipt"):
        result = cli.run_start(token=token, reason="开审", roots=roots, now=t0,
                               prepare_pipeline=_prepare_with_intent_status)
    assert result.exit_code == cli.EXIT_OK, result.message
    rendered = result.render()
    assert "    - intent_artifact_status_file: " in rendered, rendered
    assert "rvf-agent" not in rendered, rendered
    steps = result.next_steps
    assert [step[0] for step in steps] == ["①", "②", "③", "④", "⑤"], steps
    assert f"scope rerender-scope-of-work --token {token}" in steps[0], steps
    assert f"dispatch --token {token} --strictness light" in steps[2], steps
    assert " wait`" in steps[3], steps


# ---------------------------------------------------------------------------
# F2（T1/T2/T3 集成审计修复包：执行段）
# ---------------------------------------------------------------------------

def _stop_hook_script() -> Path:
    return SCRIPT_DIR / "stop_review_validate_fix.py"  # noqa: F821 - injected


def _awaiting_dispatched_agent_marker_directory(environment: dict[str, str]) -> Path:
    # 与 rvf_awaiting_dispatched_agent_marker._root 同一口径：锁根 env 之下的固定子目录。
    return Path(environment["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"]) / "awaiting-dispatched-agent"


def _awaiting_marker_files_for_session(environment: dict[str, str], session_id: str) -> list[Path]:
    directory = _awaiting_dispatched_agent_marker_directory(environment)
    return sorted(directory.glob(f"sess-{session_id}__dispatch-*.json")) if directory.is_dir() else []


def _invoke_real_stop_hook(environment: dict[str, str], *, repo: Path, session_id: str, state: Path) -> dict:
    """跑**真** Stop hook（子进程），返回本次 ledger summary。env 与 ``rvf wait`` 同一套隔离根，
    保证 marker 的写入方与 Stop 侧读取方看到的是同一个目录。"""
    # ledger 根：CODEX_RVF_LOG_ROOT 优先于 CODEX_RVF_STATE_DIR（run_ledger），这里只留后者，summary 落 ``state``。
    stop_environment = {
        key: value for key, value in environment.items()
        if key not in HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES and key != "CODEX_RVF_LOG_ROOT"
    }
    stop_environment.update(
        {
            "CODEX_RVF_STATE_DIR": str(state),
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "10",
            "RVF_TMUX_BIN": "/usr/bin/true",
            "RVF_TERMINAL_NOTIFIER_BIN": "/usr/bin/true",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(_stop_hook_script())],
        input=json.dumps({"cwd": str(repo), "session_id": session_id, "stop_hook_active": False,
                          "last_user_message": "已后台跑 rvf wait，等完成通知。"}),
        capture_output=True, text=True, check=False, env=stop_environment,
    )
    assert completed.returncode == 0, completed.stderr
    pointer = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    return json.loads(Path(pointer["summary_path"]).read_text(encoding="utf-8"))


def _write_dispatch_status(run_dir: Path, *, finished: bool) -> None:
    reviewers = run_dir / "artifacts" / "reviewers"
    _write_json(reviewers / "reviewer-plan.json",
                {"status": "completed", "effective_reviewer_count": 0, "reviewers": [], "fallbacks": []})
    status = {"launch_status": "launched", "returncode": None}
    if finished:
        status.update({"returncode": 0, "finished_at": "2020-01-01T00:00:00Z"})
    _write_json(reviewers / ".dispatch-thread.status.json", status)


def _start_background_rvf_wait(environment: dict[str, str], *, run_dir: Path, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_entrypoint_script()), "wait", "--run-dir", str(run_dir), "--max-wait", "60"],
        cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_rvf_wait_awaiting_marker_keeps_stop_silent_while_waiting_and_reengage_returns_after_exit_kill_or_ttl(
    root: Path,
) -> None:
    """H3：Claude 下后台 ``rvf wait`` 在跑时，真 Stop hook 不得发续跑提示（走 awaiting shield 静默）；
    wait 正常退出 / 被 SIGKILL 后 TTL 过期 / 被 SIGTERM 终止后，续跑提示恢复。

    修复前 ``rvf wait`` 从不写 marker ⇒ 等待期间那次 Stop 直接 reengage（run beb3c652 的事故形态）。"""
    _load_entrypoint_module()  # 顺带把 scripts 目录放上 sys.path（下面进程内 import 锁模块）
    session_id = "sess-rvf-wait-shield"
    repo = _git_repo(root / "repo").resolve()
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit_all(repo, "base")
    run_dir = root / "runs" / "rvf-run-wait-shield"
    _write_json(run_dir / "summary.json", {"run_id": run_dir.name, "repo": str(repo)})
    environment = _isolated_environment(root, CLAUDE_CODE_SESSION_ID=session_id)
    state = root / "stop-state"

    # rvf start 接受这一轮时落下的在途锁：Stop 看到它且 agent 未 handoff ⇒ 预算内会 reengage。
    with _process_environment_and_cwd(environment, repo):
        import kanban_followup_lock  # noqa: PLC0415

        assert kanban_followup_lock.write_marker(
            task_id=None, session_id=session_id, run_id=run_dir.name, run_dir=str(run_dir), repo=str(repo),
            cwd=str(repo), decision_token="dddddddddddddddd", state=kanban_followup_lock.STATE_IN_PROGRESS,
        ) is not None

    # ① 等待期间：marker 已登记（TTL = 最长等待 + 余量），Stop 静默、不耗续跑预算。
    _write_dispatch_status(run_dir, finished=False)
    waiting = _start_background_rvf_wait(environment, run_dir=run_dir, cwd=repo)
    try:
        _wait_until(lambda: bool(_awaiting_marker_files_for_session(environment, session_id)),
                    timeout_seconds=20, what="rvf wait 登记 awaiting marker")
        marker = json.loads(_awaiting_marker_files_for_session(environment, session_id)[0].read_text("utf-8"))
        assert marker["ttl_seconds"] == 60 + 300, marker
        assert marker["dispatched_agent_id"].endswith(f"-pid-{waiting.pid}"), marker
        summary = _invoke_real_stop_hook(environment, repo=repo, session_id=session_id, state=state)
        assert summary["reason_code"] == "awaiting_dispatched_agent", summary

        # ② wait 正常退出（reviewer 终态）：finally 清 marker ⇒ 下一次 Stop 恢复续跑提示。
        _write_dispatch_status(run_dir, finished=True)
        stdout, stderr = waiting.communicate(timeout=30)
        assert waiting.returncode == 0, stderr
        assert "已登记 awaiting marker" in stdout, stdout
    finally:
        if waiting.poll() is None:
            waiting.kill()
    assert _awaiting_marker_files_for_session(environment, session_id) == []
    summary = _invoke_real_stop_hook(environment, repo=repo, session_id=session_id, state=state)
    assert summary["reason_code"] == "kanban_followup_in_progress_reengage", summary

    # ③ 进程被 SIGKILL（finally 跑不到）：marker 残留 ⇒ TTL 内仍静默；TTL 一过即恢复续跑提示。
    _write_dispatch_status(run_dir, finished=False)
    killed = _start_background_rvf_wait(environment, run_dir=run_dir, cwd=repo)
    try:
        _wait_until(lambda: bool(_awaiting_marker_files_for_session(environment, session_id)),
                    timeout_seconds=20, what="第二个 rvf wait 登记 awaiting marker")
    finally:
        killed.send_signal(signal.SIGKILL)
        killed.communicate(timeout=30)
    leftover = _awaiting_marker_files_for_session(environment, session_id)
    assert len(leftover) == 1, leftover
    summary = _invoke_real_stop_hook(environment, repo=repo, session_id=session_id, state=state)
    assert summary["reason_code"] == "awaiting_dispatched_agent", summary
    expired = json.loads(leftover[0].read_text(encoding="utf-8"))
    expired["expires_at"] = "2020-01-01T00:00:00Z"
    leftover[0].write_text(json.dumps(expired), encoding="utf-8")
    summary = _invoke_real_stop_hook(environment, repo=repo, session_id=session_id, state=state)
    assert summary["reason_code"] == "kanban_followup_in_progress_reengage", summary
    assert _awaiting_marker_files_for_session(environment, session_id) == []  # 读侧惰性清掉过期 marker

    # ④ host 用 SIGTERM 砍后台任务（如 Claude 后台 Bash 超时）：信号转成 SystemExit，finally 照样清。
    terminated = _start_background_rvf_wait(environment, run_dir=run_dir, cwd=repo)
    try:
        _wait_until(lambda: bool(_awaiting_marker_files_for_session(environment, session_id)),
                    timeout_seconds=20, what="第三个 rvf wait 登记 awaiting marker")
    finally:
        terminated.send_signal(signal.SIGTERM)
        terminated.communicate(timeout=30)
    assert terminated.returncode == 128 + signal.SIGTERM, terminated.returncode
    assert _awaiting_marker_files_for_session(environment, session_id) == []


def test_rvf_wait_awaiting_marker_armed_only_on_hosts_that_wake_main_agent_after_background_work(
    root: Path,
) -> None:
    """只有「后台工作结束会唤起主 agent」的 host（Claude）才登记：Codex / Kimi 上静默 = 把 agent 晾住。
    异常路径也必须清掉。"""
    module = _load_entrypoint_module()
    environment = _isolated_environment(root)
    with _process_environment_and_cwd(environment, root):
        for non_waking_host_environment in ({"CODEX_THREAD_ID": "codex-thread"}, {}):
            with module.awaiting_dispatched_reviewers_marker_armed_while_waiting(
                max_wait_seconds=100.0, environ=non_waking_host_environment,
            ) as marker_path:
                assert marker_path is None, non_waking_host_environment
        claude_environment = {"CLAUDE_CODE_SESSION_ID": "sess-claude"}
        with module.awaiting_dispatched_reviewers_marker_armed_while_waiting(
            max_wait_seconds=0.0, environ=claude_environment,
        ) as marker_path:
            assert marker_path is None, "results（不等待）不登记"
        try:
            with module.awaiting_dispatched_reviewers_marker_armed_while_waiting(
                max_wait_seconds=100.0, environ=claude_environment,
            ) as marker_path:
                assert marker_path is not None and Path(marker_path).is_file()
                raise RuntimeError("等待中途抛错")
        except RuntimeError:
            pass
        assert not Path(marker_path).exists(), "异常路径也必须清掉 marker"


def _fix_loop_repo_and_run(root: Path) -> tuple[Path, Path, list[Path]]:
    repo = _git_repo(root / "repo").resolve()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (repo / name).write_text(f"{name}-1\n", encoding="utf-8")
    _commit_all(repo, "base")
    run_dir = (root / "runs" / "rvf-run-fix-shield").resolve()
    _write_json(run_dir / "summary.json", {"run_id": run_dir.name, "repo": str(repo)})
    _write_json(run_dir / "artifacts" / "inputs" / "scope.contract.json",
                {"run_id": run_dir.name, "repo": str(repo), "fix_allowlist": ["a.txt", "b.txt", "c.txt", "d.txt"],
                 "scope_hash": "sha256:" + "1" * 64, "protected_files": [], "background_files": [],
                 "excluded_path_prefixes": []})
    issue_files = [
        _write_json(root / "issues" / f"{issue_id}.json",
                    {"issue_id": issue_id, "path": path, "line": 1, "message": f"{path} 要改", "run_id": run_dir.name})
        for issue_id, path in (("RVF-001", "a.txt"), ("RVF-002", "b.txt"), ("RVF-003", "c.txt"), ("RVF-004", "d.txt"))
    ]
    return repo, run_dir, issue_files


def test_rvf_fix_prepare_arms_awaiting_marker_per_attempt_and_full_stop_command_works_from_any_cwd(
    root: Path,
) -> None:
    """H3（fix attempt 那一半）+ M2：

    * Claude 下 ``fix prepare`` 按 attempt 登记 awaiting marker，主会话 id 记进 attempt.json；Stop 侧读取方
      认得出它们（同一会话、ACTIVE）。
    * 参数块给出的整行收尾命令自带 ``cd`` / ``--attempt-id`` / ``--run-dir``：从任意 cwd 照抄即成功（Claude
      子代理每次 Bash 都重置 cwd）；旧写法 ``rvf fix stop --status …`` 离开 attempt worktree 即 exit 2。
    * 子代理 stop 清掉自己那条；``apply --attempt-id`` 兜底清掉已 stop 的残留、仍在跑的不动；
      ``apply --all-stopped``（协议：全部子代理已结束）连没调 stop 的也清。
    * Codex 下不登记。
    * 批量中途失败（exit 4）时整批都不登记：失败前已建好的 attempt 不留 awaiting marker。"""
    _load_entrypoint_module()  # 顺带把 scripts 目录放上 sys.path（下面进程内 import marker / attempt 模块）
    session_id = "sess-fix-shield"
    repo, run_dir, issue_files = _fix_loop_repo_and_run(root)
    environment = _isolated_environment(root, CLAUDE_CODE_SESSION_ID=session_id)
    prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", *map(str, issue_files[:3]),
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    attempts = {item["issue_id"]: item for item in json.loads(prepared.stdout)["prepared"]}
    for item in attempts.values():
        assert item["awaiting_dispatched_agent_marker_path"] and Path(item["awaiting_dispatched_agent_marker_path"]).is_file()
        attempt = json.loads((Path(item["attempt_worktree"]).parent / "attempt.json").read_text(encoding="utf-8"))
        assert attempt["main_agent_session_id_awaiting_fix_subagent"] == session_id, attempt
    with _process_environment_and_cwd(environment, root):
        import rvf_awaiting_dispatched_agent_marker  # noqa: PLC0415

        active_ids = sorted(m["dispatched_agent_id"]
                            for m in rvf_awaiting_dispatched_agent_marker.iter_active_awaiting_for_session(session_id))
    assert active_ids == sorted(item["attempt_id"] for item in attempts.values()), active_ids

    # M2：旧写法离开 attempt worktree 即失败；整行命令从无关 cwd 照抄即成功。
    unrelated_cwd = root / "unrelated-cwd"
    unrelated_cwd.mkdir()
    subagent_environment = {k: v for k, v in environment.items() if k not in HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES}
    legacy = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "stop", "--status", "fixed"],
        cwd=unrelated_cwd, env=subagent_environment, capture_output=True, text=True, check=False,
    )
    assert legacy.returncode == 2 and "--attempt-id is required" in legacy.stderr, legacy.stderr
    first = attempts["RVF-001"]
    stop_command = first["fix_stop_command"]
    assert stop_command.startswith("cd ") and f"--attempt-id {first['attempt_id']}" in stop_command, stop_command
    assert "--run-dir " in stop_command and "--status <fixed|false_positive|elevated|failed>" in stop_command
    (Path(first["attempt_worktree"]) / "a.txt").write_text("a.txt-fixed\n", encoding="utf-8")
    # 子代理 shell 里没有 `rvf` 也没有主会话 session id：把命令前缀换成入口脚本、状态填 fixed 后照抄。
    runnable = stop_command.replace("--status <fixed|false_positive|elevated|failed>", "--status fixed")
    runnable = runnable.replace(f"&& {_rvf_invocation_for_test(stop_command)} fix stop",
                                f"&& {sys.executable} {_entrypoint_script()} fix stop")
    stopped = subprocess.run(["bash", "-c", runnable], cwd=unrelated_cwd, env=subagent_environment,
                             capture_output=True, text=True, check=False)
    assert stopped.returncode == 0, stopped.stderr + stopped.stdout
    assert not Path(first["awaiting_dispatched_agent_marker_path"]).exists(), "子代理 stop 必须清掉自己那条"
    assert Path(attempts["RVF-002"]["awaiting_dispatched_agent_marker_path"]).is_file(), "别的 attempt 不受影响"

    # RVF-002 stop 成 false_positive，但模拟「stop 时清除失败」留下残留；RVF-003 仍在跑。
    second = attempts["RVF-002"]
    runnable_second = second["fix_stop_command"].replace(
        "--status <fixed|false_positive|elevated|failed>", "--status false_positive"
    ).replace(f"&& {_rvf_invocation_for_test(second['fix_stop_command'])} fix stop",
              f"&& {sys.executable} {_entrypoint_script()} fix stop")
    assert subprocess.run(["bash", "-c", runnable_second], cwd=unrelated_cwd, env=subagent_environment,
                          capture_output=True, text=True, check=False).returncode == 0
    with _process_environment_and_cwd(environment, root):
        import rvf_fix_attempt  # noqa: PLC0415

        rvf_fix_attempt.arm_fix_attempt_subagent_awaiting_marker(
            run_dir=run_dir, attempt_id=second["attempt_id"], main_agent_session_id=session_id
        )
    assert Path(second["awaiting_dispatched_agent_marker_path"]).is_file()
    # 边收结果边合回（--attempt-id）：兜底清掉已 stop 的残留，仍在跑的 RVF-003 不动。
    applied_one = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--attempt-id", first["attempt_id"],
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert applied_one.returncode == 0, applied_one.stderr + applied_one.stdout
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a.txt-fixed\n"
    assert not Path(second["awaiting_dispatched_agent_marker_path"]).exists(), "apply 兜底清掉已 stop 的残留"
    assert Path(attempts["RVF-003"]["awaiting_dispatched_agent_marker_path"]).is_file(), "仍在跑的 attempt 不动"
    # --all-stopped 按协议意味着「全部子代理已结束」：没调 stop 就死掉的 RVF-003 也不能把 marker 留到 TTL
    # （否则本会话此后每次 Stop 都被静默，连新工作的决策点都发不出）。
    applied_all = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "apply", "--all-stopped", "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert applied_all.returncode == 0, applied_all.stderr + applied_all.stdout
    assert json.loads(applied_all.stdout)["attempts_not_stopped"] == [attempts["RVF-003"]["attempt_id"]]
    assert not Path(attempts["RVF-003"]["awaiting_dispatched_agent_marker_path"]).exists()

    # Codex：子代理结束不会唤起主 agent ⇒ 不登记（静默会把 agent 晾住）。
    codex_environment = {k: v for k, v in environment.items() if k not in HOST_SESSION_ENVIRONMENT_VARIABLE_NAMES}
    codex_environment["CODEX_THREAD_ID"] = "codex-thread"
    codex_prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", str(issue_files[3]),
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=codex_environment, capture_output=True, text=True, check=False,
    )
    assert codex_prepared.returncode == 0, codex_prepared.stderr + codex_prepared.stdout
    assert json.loads(codex_prepared.stdout)["prepared"][0]["awaiting_dispatched_agent_marker_path"] is None

    # 批量中途失败（后一条 issue JSON 损坏）⇒ exit 4，主会话拿到的是失败、通常不派发子代理：前面已建好的
    # attempt 不得留下 awaiting marker，否则本会话的 Stop 会被静默到 TTL、续跑提示发不出。
    failed_batch_session_id = "sess-fix-shield-failed-batch"
    failed_batch_environment = _isolated_environment(root, CLAUDE_CODE_SESSION_ID=failed_batch_session_id)
    valid_issue_file_before_corrupt_one = _write_json(
        root / "issues" / "RVF-005.json",
        {"issue_id": "RVF-005", "path": "d.txt", "line": 1, "message": "d.txt 要改", "run_id": run_dir.name},
    )
    corrupt_issue_file = root / "issues" / "RVF-006-corrupt.json"
    corrupt_issue_file.write_text("{不是合法 JSON", encoding="utf-8")
    failed_batch = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare",
         str(valid_issue_file_before_corrupt_one), str(corrupt_issue_file), "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=failed_batch_environment, capture_output=True, text=True, check=False,
    )
    assert failed_batch.returncode == 4, failed_batch.stderr + failed_batch.stdout
    failed_batch_payload = json.loads(failed_batch.stdout)
    assert failed_batch_payload["failure"] and str(corrupt_issue_file) in failed_batch_payload["failure"]
    assert [item["issue_id"] for item in failed_batch_payload["prepared"]] == ["RVF-005"], failed_batch_payload
    assert failed_batch_payload["prepared"][0]["awaiting_dispatched_agent_marker_path"] is None, failed_batch_payload
    with _process_environment_and_cwd(failed_batch_environment, root):
        import rvf_awaiting_dispatched_agent_marker  # noqa: PLC0415

        leftover_active_markers_after_failed_batch = list(
            rvf_awaiting_dispatched_agent_marker.iter_active_awaiting_for_session(failed_batch_session_id)
        )
    assert leftover_active_markers_after_failed_batch == [], leftover_active_markers_after_failed_batch


def _rvf_invocation_for_test(stop_command: str) -> str:
    """从渲染出的收尾命令里取 `rvf` 前缀（PATH 上的 rvf / shim 绝对路径 / python3 入口脚本三种之一）。"""
    after_cd = stop_command.split(" && ", 1)[1]
    return after_cd.split(" fix stop", 1)[0]


def test_rvf_validate_inside_fix_attempt_worktree_or_suppressed_subprocess_stays_off_main_run(root: Path) -> None:
    """H2b：attempt worktree 里的 ``rvf validate`` 验证的是 attempt 自己（``--repo`` 取 cwd），且不写主 run；
    Stop hook 被抑制的子进程同理。对照组：主会话在主 worktree 里照常写进 run。

    修复前：attempt 里推断出主 run，``--repo`` 取主 worktree（验错对象，只存在于 attempt 的文件找不到），
    记录写进主 run、land 时被重放。

    RVF-001：隔离分支只「不注入 ``--rvf-run-dir``」不够——子进程从调用方 shell 继承来的 ``RVF_RUN_DIR``
    （这里指向本用例自建的主 run）会原样流进被验证的命令（F1 之前还会让 ``validation_result_cache.py``
    回退写进主 run）；继承来的 ``GIT_DIR`` / ``GIT_WORK_TREE``（指向主 worktree）会让 ``--repo`` 解析成
    主 worktree。两者都必须挡掉。"""
    session_id = "sess-validate-isolation"
    repo, run_dir, issue_files = _fix_loop_repo_and_run(root)
    environment = _isolated_environment(root, RVF_SESSION_ID=session_id)
    rvf_home = Path(environment["RVF_HOME"])
    _write_decision_token(rvf_home, token="eeeeeeeeeeeeeeee", state="dispatched", worktree=repo,
                          session_id=session_id, minted_at="2020-01-01T01:00:00Z")
    _write_scope_overlay(rvf_home, token="eeeeeeeeeeeeeeee", worktree=repo, session_id=session_id,
                         run_dir=run_dir, updated_at="2020-01-01T01:00:00Z")
    prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", str(issue_files[0]), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    attempt_worktree = Path(json.loads(prepared.stdout)["prepared"][0]["attempt_worktree"])
    (attempt_worktree / "only-in-attempt-worktree.txt").write_text("x\n", encoding="utf-8")
    run_validation_index = run_dir / "artifacts" / "validation-records.json"
    run_validation_commands = run_dir / "artifacts" / "handoff-validation-commands.json"
    probe = ["--name", "fixer-probe", "--", "sh", "-c", "test -f only-in-attempt-worktree.txt"]
    environment_inherited_from_main_session_shell = {
        **environment,
        "RVF_RUN_DIR": str(run_dir),
        "GIT_DIR": str(repo / ".git"),
        "GIT_WORK_TREE": str(repo),
    }

    in_attempt = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", *probe],
        cwd=attempt_worktree, env=environment_inherited_from_main_session_shell,
        capture_output=True, text=True, check=False,
    )
    assert in_attempt.returncode == 0, in_attempt.stderr + in_attempt.stdout  # 验证的是 attempt 自己
    assert "不写主 run" in in_attempt.stderr, in_attempt.stderr
    assert not run_validation_index.exists() and not run_validation_commands.exists()

    suppressed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", "--name", "reviewer-probe", "--", "true"],
        cwd=repo, env={**environment_inherited_from_main_session_shell, "RVF_SUPPRESS_STOP_HOOK": "1"},
        capture_output=True, text=True, check=False,
    )
    assert suppressed.returncode == 0, suppressed.stderr + suppressed.stdout
    assert not run_validation_index.exists() and not run_validation_commands.exists()

    # 对照：主会话在主 worktree 里跑，照常推断 run 并写进验证记录（否则上面两条是空断言）。
    main_session = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", "--name", "handoff-validation-001", "--", "true"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert main_session.returncode == 0, main_session.stderr + main_session.stdout
    assert run_validation_index.is_file() and run_validation_commands.is_file()
    recorded_names = [entry.get("name") for entry in
                      json.loads(run_validation_commands.read_text(encoding="utf-8")).get("validation_commands", [])]
    assert "handoff-validation-001" in recorded_names, recorded_names
    assert "fixer-probe" not in recorded_names and "reviewer-probe" not in recorded_names, recorded_names


def test_rvf_land_passthrough_commit_defaults_to_most_recent_prepare_run_of_this_repository(root: Path) -> None:
    """M4：``rvf land prepare`` 原样转给 rvf_land.py 并记下本仓这次 prepare 的 run；``rvf land commit`` 不带
    ``--run-dir`` 即落到那个 run。没 prepare 过的仓库诚实报找不到，不猜。"""
    sys.path.insert(0, str(ROOT / "tests"))  # noqa: F821 - injected
    from test_reviewed_task_candidate_evidence import create_completed_run, repository  # noqa: PLC0415

    root.mkdir(parents=True, exist_ok=True)
    repo = repository(root / "repo").resolve()
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    run_dir = create_completed_run(root, repo)
    environment = _isolated_environment(root)
    environment.pop("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", None)
    recorded = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "validate", "--rvf-run-dir", str(run_dir),
         "--name", "handoff-validation-001", "--", "true"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert recorded.returncode == 0, recorded.stderr + recorded.stdout

    other_repo = _git_repo(root / "never-prepared").resolve()
    (other_repo / "x.txt").write_text("x\n", encoding="utf-8")
    _commit_all(other_repo, "base")
    no_pointer = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "land", "commit", "--message-file", "-"],
        cwd=other_repo, env=environment, input="x\n", capture_output=True, text=True, check=False,
    )
    assert no_pointer.returncode == 2 and "最近一次 `rvf land prepare`" in no_pointer.stderr, no_pointer.stderr

    # RVF-001：调用方 shell 里指向别的仓库的 GIT_DIR / GIT_WORK_TREE 不能带偏指针的仓库键——rvf_land 自己
    # 清洗这些变量后按 cwd 定位仓库，入口也必须同口径，否则 prepare 不记指针、commit 误报找不到。
    environment_with_git_location_pointing_at_other_repository = {
        **environment,
        "GIT_DIR": str(other_repo / ".git"),
        "GIT_WORK_TREE": str(other_repo),
    }
    prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "land", "prepare", "--handoff",
         str(run_dir / "artifacts" / "handoff.md"), "--repo", "."],
        cwd=repo, env=environment_with_git_location_pointing_at_other_repository,
        capture_output=True, text=True, check=False,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert prepared.stdout.startswith("RVF_LAND_PREPARE exit=0"), prepared.stdout  # 文本输出原样透传

    committed = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "land", "commit", "--message-file", "-"],
        cwd=repo, env=environment_with_git_location_pointing_at_other_repository,
        input="fix(test): rvf land 透传\n\nsanity-check：自洽\n验证：\n[[rvf-land-validation-summary]]\n",
        capture_output=True, text=True, check=False,
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert f"run_dir={run_dir.resolve()}" in committed.stderr, committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "committed", result
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
                          check=True).stdout == ""


def test_rvf_apply_orders_attempts_by_created_at_without_baseline_patch_mtime_fallback(root: Path) -> None:
    """L-F2a：给「早于 created_at 字段的旧 attempt」用 baseline.patch mtime 兜底，是 RVF 自身的向后兼容代码
    （项目未分发，按 AGENTS.md 清理）。缺 created_at 的记录不再读文件 mtime。"""
    module = _load_entrypoint_module()
    run_dir = root / "run"
    attempts = run_dir / "artifacts" / "fix-attempts"
    _write_json(attempts / "rvf-attempt-new" / "attempt.json",
                {"attempt_id": "rvf-attempt-new", "created_at": "2020-01-02T00:00:00+00:00", "status": "fixed"})
    _write_json(attempts / "rvf-attempt-legacy" / "attempt.json", {"attempt_id": "rvf-attempt-legacy", "status": "fixed"})
    (attempts / "rvf-attempt-legacy" / "baseline.patch").write_text("", encoding="utf-8")
    records = module._attempt_records(run_dir)
    by_id = {record["attempt_id"]: record for record in records}
    assert by_id["rvf-attempt-legacy"]["_created_at_sort_key"] == "", by_id["rvf-attempt-legacy"]
    assert [record["attempt_id"] for record in records] == ["rvf-attempt-legacy", "rvf-attempt-new"]


def test_rvf_receipts_carry_explicit_run_dir_only_when_current_session_is_unresolvable(root: Path) -> None:
    """L-F2c：Kimi 等拿不到会话 id 的 host 上 run 推断一律 fail-closed ⇒ 回执给的下一步命令直接带 run 目录；
    拿得到会话 id 时保持短命令。覆盖 ``rvf wait`` 文案、reviewer 摘要与 start 回执三处。"""
    module = _load_entrypoint_module()
    run_dir = root / "runs" / "rvf run with space"
    quoted = f"--run-dir '{run_dir}'"
    assert module.render_explicit_run_dir_argument_when_current_session_unresolvable(run_dir, environ={}) == " " + quoted
    assert module.render_explicit_run_dir_argument_when_current_session_unresolvable(
        run_dir, environ={"CLAUDE_CODE_SESSION_ID": "s"}) == ""
    assert module.render_explicit_run_dir_argument_when_current_session_unresolvable(
        run_dir, option_name="--rvf-run-dir", environ={}) == f" --rvf-run-dir '{run_dir}'"

    summary = {"plan_status": "completed", "effective_reviewer_count": 2, "reviewer_count": 2, "plan_path": "p",
               "legs": [{"reviewer_id": r, "kind": "issues", "valid": True, "issues": [{"path": "a", "line": 1}]}
                        for r in ("a", "b")]}
    with _only_these_host_session_environment_variables():
        kimi_wait = module.render_host_specific_reviewer_wait_instruction(
            "rvf", module.REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING, run_dir=run_dir)
        kimi_summary = module.render_reviewer_results_summary(summary, dispatch_state=None, run_dir=run_dir)
    assert f"rvf wait {quoted}" in kimi_wait, kimi_wait
    assert f"fix prepare <issue.json>… {quoted}" in kimi_summary, kimi_summary
    with _only_these_host_session_environment_variables(CLAUDE_CODE_SESSION_ID="s"):
        claude_wait = module.render_host_specific_reviewer_wait_instruction("rvf", run_dir=run_dir)
    assert "--run-dir" not in claude_wait and "`rvf wait`" in claude_wait, claude_wait
    # M1：Claude 文案必须显式给后台 Bash 的 timeout，且它盖得住 Claude 下的缺省等待时长。
    assert f"`timeout: {module.CLAUDE_BACKGROUND_BASH_TIMEOUT_MILLISECONDS_FOR_REVIEWER_WAIT}`" in claude_wait
    assert module.CLAUDE_BACKGROUND_BASH_TIMEOUT_MILLISECONDS_FOR_REVIEWER_WAIT > 1000 * module.default_reviewer_wait_seconds_for_host(
        module.REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION)
    assert module.CLAUDE_BACKGROUND_BASH_TIMEOUT_MILLISECONDS_FOR_REVIEWER_WAIT <= 7_200_000

    cli = load_rvf_agent_decision_cli_module()  # noqa: F821
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _agent_cli_roots(cli, root)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    token = _mint_agent_decision_token(dts, roots, worktree=_agent_cli_worktree(root), now=t0)  # noqa: F821
    with _only_these_host_session_environment_variables():
        result = cli.run_start(token=token, reason="开审", roots=roots, now=t0,
                               prepare_pipeline=_agent_cli_stub_prepare(root))  # noqa: F821
    assert result.exit_code == cli.EXIT_OK, result.message
    wait_step = result.next_steps[3]
    assert "--run-dir" in wait_step and str(result.detail["run_dir"]) in wait_step, wait_step


def test_rvf_execution_segment_docs_teach_rvf_validate_background_timeout_and_no_polling_protocol(root: Path) -> None:
    """M1 / M3 / M2 / L-F2b 的文档面：agent 按文档照做就不会踩回旧坑。"""
    del root
    skill_directory = SCRIPT_DIR.parent  # noqa: F821
    skill = (skill_directory / "SKILL.md").read_text(encoding="utf-8")
    validate_fix = (skill_directory / "prompts" / "validate-fix.md").read_text(encoding="utf-8")
    merge_policy = (skill_directory / "references" / "review-merge-policy.md").read_text(encoding="utf-8")
    decision_cli = (SCRIPT_DIR / "rvf_agent_decision_cli.py").read_text(encoding="utf-8")  # noqa: F821
    land_skill = (skill_directory.parent / "rvf-land" / "SKILL.md").read_text(encoding="utf-8")

    # M3：验证段教 `rvf validate`，不再教 Claude Bash 里为空的 $RVF_REPO / $RVF_RUN_DIR。
    assert "rvf validate --name" in skill
    assert 'validation_result_cache.py run --repo "$RVF_REPO"' not in skill
    # M1：Claude 后台等待必须显式给 timeout。
    assert "`timeout: 3000000`" in skill
    # M2：子代理收尾是整行命令，不再宣称「从 cwd 反推」。
    assert "attempt 与 run 从 cwd 反推" not in skill + validate_fix
    assert "cd <attempt worktree> && rvf fix stop --attempt-id" in skill + validate_fix
    # L-F2b：已删除的轮询协议 / --wait-status 不再出现在 agent 面文档与 decision CLI docstring；
    # fix prepare 自带 upsert，不再要求先手动 upsert。
    assert "轮询协议" not in merge_policy and "--wait-status" not in merge_policy
    assert "``--wait-status`` 有界轮询" not in decision_cli
    assert "也必须先运行 `rvf_fix_issue.py upsert`" not in validate_fix
    # M4：land 两步都经 `rvf land`。
    assert "rvf land prepare" in land_skill and "rvf land commit" in land_skill
    assert "<skill-scripts-dir>/rvf_land.py" not in land_skill
