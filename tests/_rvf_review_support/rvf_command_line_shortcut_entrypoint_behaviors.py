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
import subprocess
import sys
from pathlib import Path
from typing import Iterator

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
    prepared = subprocess.run(
        [sys.executable, str(_entrypoint_script()), "fix", "prepare", *map(str, issue_files),
         "--run-dir", str(run_dir), "--json"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    attempts = json.loads(prepared.stdout)["prepared"]
    assert [item["issue_id"] for item in attempts] == ["RVF-001", "RVF-002", "RVF-003"]
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
