#!/usr/bin/env python3
"""结构性 gate：**投给当前活会话**的自注入，有且只有发起决策点横幅。

护的不变量（计划 §二）：

* **INV-1** —— 一次 Stop 至多产生一种自注入：发起决策点横幅。
* **INV-2** —— ``$review-validate-fix`` 只能由 agent 在接受之后自己发出，不得是任何注入正文
  的第一行。

**为什么是行为断言而不是 grep**：``$review-validate-fix`` 这个字面量在仓里到处都是（SKILL.md、
fork prompt、新 task prompt、文档、测试）。真正要钉死的不是「这个字符串存在」，而是「默认发起
路径吐出来的那份注入正文长什么样」。所以本 gate 端到端跑一次 Stop hook，直接看它的 stdout。

三条用例刻意成对：
1. **kanban task 内 + DIRTY** —— 历史上正是这里被判成「延续后端」而绕过决策点、直接投一条落地
   即执行的祈使句（2026-08-08 事故成因）。断言现在拿到的是横幅。
2. **kanban task 外 + DIRTY** —— 对照组，行为本来就正确，钉住它不被顺手改坏。
3. **显式 ``CODEX_RVF_FORK_MODE=kanban-followup``** —— 保留的终端投递逃生口**必须仍然可达**
   （P4a：40+ 既有 followup 用例依赖它）。断言它不产生横幅，从反面证明前两条不是 vacuous truth。

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
    *, repo: Path, state_dir: Path, extra_env: dict[str, str] | None = None
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
    event = {
        "cwd": str(repo),
        "stop_hook_active": False,
        "session_id": "session-sole-self-injection",
        "last_user_message": "实现完了，先停一下。",
    }
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout or "{}")


def _assert_no_imperative_self_injection(payload: dict[str, object]) -> None:
    """payload 里任何投给模型的正文，首行都不得是那条祈使句（INV-2）。"""
    for key in ("reason", "additionalContext"):
        body = payload.get(key)
        if not isinstance(body, str) or not body.strip():
            continue
        first_line = body.strip().splitlines()[0].strip()
        assert not first_line.startswith(IMPERATIVE_SELF_INJECTION_PREFIX), (
            f"payload[{key!r}] 的首行是祈使句自注入，违反 INV-2：{first_line!r}"
        )


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


if __name__ == "__main__":  # pragma: no cover - 与仓内其它 pytest 文件一致的直跑入口
    raise SystemExit(pytest.main([__file__, "-q"]))
