#!/usr/bin/env python3
"""结构性 gate：**投给当前活会话**的自注入，有且只有发起决策点横幅。

护的不变量（计划 §二）：

* **INV-1** —— 一次 Stop 至多产生一种自注入：发起决策点横幅。
* **INV-2** —— ``$review-validate-fix`` 只能由 agent 在接受之后自己发出，不得是任何注入正文
  的第一行。

**为什么是行为断言而不是 grep**：``$review-validate-fix`` 这个字面量在仓里到处都是（SKILL.md、
fork prompt、新 task prompt、文档、测试）。真正要钉死的不是「这个字符串存在」，而是「默认发起
路径吐出来的那份注入正文长什么样」。所以本 gate 端到端跑一次 Stop hook，直接看它的 stdout。

前三条用例刻意成对：
1. **kanban task 内 + DIRTY** —— 历史上正是这里被判成「延续后端」而绕过决策点、直接投一条落地
   即执行的祈使句（2026-08-08 事故成因）。断言现在拿到的是横幅。
2. **kanban task 外 + DIRTY** —— 对照组，行为本来就正确，钉住它不被顺手改坏。
3. **显式 ``CODEX_RVF_FORK_MODE=kanban-followup``** —— 保留的终端投递逃生口**必须仍然可达**
   （P4a：40+ 既有 followup 用例依赖它）。断言它不产生横幅，从反面证明前两条不是 vacuous truth。

**第 4 组：reengage 分支的覆盖缺口（补齐）**。上面三条只跑了「dirty + 无在途 run」一支，从未走到
``kanban_followup_in_progress_decision``，于是 INV-1/INV-2 对三条 ``reengage_decision`` 分支
**恒为假绿**——而实测它们占全部阻塞注入的 90/92。第 4 组把在途锁摆出来，逼 Stop 走 reengage，
并在**两个 host** 下各验一次不变量（Claude 走非错误的 additionalContext，兜底走 decision:block）。

⚠️ 自扫型 checker 假绿前例（见 dev 记录）：本文件被 ``scripts/check_skill_contracts.sh`` 的
``repo_required_files`` 清单要求存在 + 一条 ``run_step`` pytest 条目执行。新增此类 gate 时务必在
**tracked** 状态下先验一次真红，否则 untracked 期的自扫会给出假绿。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _rvf_test_support.sole_self_injection_invariants import (  # noqa: E402
    assert_self_injection_invariants,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "stop_review_validate_fix.py"
)

DECISION_POINT_BANNER_FIRST_LINE = "=== REVIEW-VALIDATE-FIX 发起决策点（agent 决策，非自动发起）==="
IMPERATIVE_SELF_INJECTION_PREFIX = "$review-validate-fix"


def _init_dirty_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    (path / "changed.txt").write_text("dirty\n", encoding="utf-8")
    return path


def _invoke_stop_hook(
    *,
    repo: Path,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
    event_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("CODEX_RVF_", "RVF_", "KANBAN_", "CLINE_KANBAN_")):
            env.pop(name, None)
    env.pop("CODEX_THREAD_ID", None)
    # 隔离铁律：决策点会铸 token / 写注入尝试 ledger / declined 冷却，默认落盘根是 $RVF_HOME。
    # 不显式指向 tmp 就会污染真实 ~/.rvf（live infra，假绿）。
    env["RVF_HOME"] = str(state_dir / "rvf-home")
    env["CODEX_RVF_STATE_DIR"] = str(state_dir)
    env["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state_dir / "kanban-followup-in-progress")
    env["CODEX_SESSION_INDEX_PATH"] = str(state_dir / "empty-session-index.jsonl")
    if extra_env:
        env.update(extra_env)
    event: dict[str, object] = {
        "cwd": str(repo),
        "stop_hook_active": False,
        "session_id": "session-sole-self-injection",
        "last_user_message": "实现完了，先停一下。",
    }
    if event_overrides:
        event.update(event_overrides)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout or "{}")


def _assert_no_imperative_self_injection(payload: dict[str, object]) -> str | None:
    """INV-1 + INV-2：至多一条投给模型的正文，且其首行不得是那条祈使句。

    ⚠️ 本函数曾只在 payload **顶层**找 ``reason`` / ``additionalContext``。Stop 的
    ``additionalContext`` 实际嵌在 ``hookSpecificOutput`` 里，于是 Claude 通道上这条断言恒不
    命中——假绿。现在统一走 ``_rvf_test_support.sole_self_injection_invariants``。
    """
    return assert_self_injection_invariants(payload)


def test_in_kanban_task_dirty_stop_injects_decision_point_banner(tmp_path: Path) -> None:
    repo = _init_dirty_repo(tmp_path / "dirty")
    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=tmp_path / "state",
        # 默认 fork mode（auto）+ 在 task 内：历史上这一组合推出 backend=kanban-followup，
        # 被当成「延续」而绕过决策点。P1 之后必须走横幅。
        extra_env={"KANBAN_TASK_ID": "task-sole-injection"},
    )
    assert payload.get("decision") == "block", payload
    reason = str(payload.get("reason") or "")
    assert reason.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE, reason[:400]
    assert "RVF_DECISION_TOKEN=" in reason, reason[:400]
    _assert_no_imperative_self_injection(payload)


def test_outside_kanban_task_dirty_stop_injects_decision_point_banner(tmp_path: Path) -> None:
    repo = _init_dirty_repo(tmp_path / "dirty")
    payload = _invoke_stop_hook(repo=repo, state_dir=tmp_path / "state")
    assert payload.get("decision") == "block", payload
    reason = str(payload.get("reason") or "")
    assert reason.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE, reason[:400]
    _assert_no_imperative_self_injection(payload)


def test_explicit_kanban_followup_transport_override_stays_reachable(tmp_path: Path) -> None:
    """逃生口反面证明：显式点名延续 transport 时**不**走横幅，仍落到旧的投递路径。

    没有这一条，前两条断言就可能因为「横幅是唯一出口」而 vacuous，也就看不出 P1 的判据是否
    真的按 ``backend_selection_mode`` 区分了 auto 与 explicit。
    """
    repo = _init_dirty_repo(tmp_path / "dirty")
    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=tmp_path / "state",
        extra_env={
            "KANBAN_TASK_ID": "task-explicit-followup",
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            # 没有可用的 kanban client → 投递失败并如实上报；本用例只关心「没走横幅」。
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(tmp_path / "missing-client.py"),
        },
    )
    reason = str(payload.get("reason") or "")
    assert DECISION_POINT_BANNER_FIRST_LINE not in reason, reason[:400]


CLAUDE_HOST_EVENT_MARKER: dict[str, object] = {
    # Claude plugin 入口亲手打的 host 标（hooks/_claude_hook_entry.py:114）——有它才走
    # 非错误的 hookSpecificOutput.additionalContext 通道。
    "source": {"provider": "claude-code", "plugin": "review-validate-fix"}
}


def _load_kanban_followup_lock_module():
    """加载生产侧的 ``kanban_followup_lock``，用它现算 marker 文件名（绝不硬编码命名规则）。"""
    import importlib.util

    script_dir = SCRIPT.parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    spec = importlib.util.spec_from_file_location(
        "kanban_followup_lock_for_inv1_gate", script_dir / "kanban_followup_lock.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _arm_in_progress_kanban_followup_lock(
    *, state_dir: Path, repo: Path, task_id: str, session_id: str
) -> Path:
    """摆一把 active 的 in_progress 锁，逼下一次 Stop 走 ``reengage_decision``（而非发起决策点）。"""
    lock_module = _load_kanban_followup_lock_module()
    marker_dir = state_dir / "kanban-followup-in-progress"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_name = lock_module.marker_paths(task_id=task_id, session_id=None, repo=repo)[0].name
    marker_path = marker_dir / marker_name
    marker_path.write_text(
        json.dumps(
            {
                "marker_version": 1,
                "state": "in_progress",
                "armed_at": "2026-05-21T15:57:55Z",
                "expires_at": "2999-01-01T00:00:00Z",
                "kanban_task_id": task_id,
                "session_id": session_id,
                "run_id": "rvf-inv1-in-flight",
                "run_dir": str(state_dir / "inv1-run"),
                "repo": None,
                "dispatch_token": None,
                "delivered_session_id": None,
                "delivered_transcript_path": None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker_path


@pytest.mark.parametrize(
    "host_marker, channel",
    [
        pytest.param(CLAUDE_HOST_EVENT_MARKER, "hookSpecificOutput", id="claude-host"),
        pytest.param({}, "decision", id="codex-host-fallback"),
    ],
)
def test_in_flight_run_reengage_branch_keeps_self_injection_invariants(
    tmp_path: Path, host_marker: dict[str, object], channel: str
) -> None:
    """INV-1/INV-2 覆盖缺口补齐：在途 run 的 reengage 分支（占实测阻塞注入的 90/92）。

    没有这一条，前三条只跑「dirty + 无在途 run」一支，INV-1 对 reengage 恒为假绿。
    """
    repo = _init_dirty_repo(tmp_path / "dirty")
    state = tmp_path / "state"
    marker_path = _arm_in_progress_kanban_followup_lock(
        state_dir=state, repo=repo, task_id="task-inv1", session_id="session-sole-self-injection"
    )
    assert marker_path.exists()

    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=state,
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "KANBAN_TASK_ID": "task-inv1",
            "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
        },
        event_overrides=dict(host_marker),
    )

    body = assert_self_injection_invariants(payload)
    assert body is not None, payload
    # 走的确实是 reengage，不是发起决策点——否则这条用例又退回去测第一支了。
    assert DECISION_POINT_BANNER_FIRST_LINE not in body, body[:400]
    assert "rvf-inv1-in-flight" in body, body[:400]
    assert channel in payload, payload
    # AUD-4：每条 agent-directive 都要带 developer-only 旁注，否则终端里与真崩溃不可分辨。
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"], payload


if __name__ == "__main__":  # pragma: no cover - 与仓内其它 pytest 文件一致的直跑入口
    raise SystemExit(pytest.main([__file__, "-q"]))
