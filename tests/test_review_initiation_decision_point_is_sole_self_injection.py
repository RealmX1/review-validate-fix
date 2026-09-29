#!/usr/bin/env python3
"""结构性 gate：**一次 Stop 投给 agent 的发起消息**，有且只有发起决策点横幅。

护的不变量（计划 §二）：

* **INV-1** —— 一次 Stop 至多产生**一条**面向 agent 的发起正文，**无论它走哪条 transport**
  （stdout 自注入 / cline-kanban 带外投递）。文件名里的 ``sole_self_injection`` 是上一轮留下的
  历史口径——那时只有自注入一条路；口径已扩，文件名待独立一刀改，不混进本轮行为改动。
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

**第 5/6 组：带外投递（本轮新增）**。横幅改经 cline-kanban 终端投递成为一条真实 user message
之后，正文**根本不进 stdout payload**——只看 payload 的 INV-1 断言对新 transport 恒为真（第二代
假绿）。故这两组分别验：投递成功时 stdout **不驱动 agent**（只有 systemMessage）且带外投递恰好
一条；投递**确定失败**时当场回落自注入（INV-B）且带外投递为零。两组合起来才让
``len(stdout 正文) + len(带外投递) <= 1`` 这条口径在两条 transport 上都非 vacuous。

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
    assert_agent_facing_initiation_message_invariants,
    out_of_band_decision_point_deliveries,
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

DECISION_POINT_BANNER_FIRST_LINE = "=== REVIEW-VALIDATE-FIX 发起决策点：由你自主决定 start 或 decline ==="
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


def _assert_sole_initiation_message(
    payload: dict[str, object], state_dir: Path
) -> str | None:
    """INV-1 + INV-2：stdout 正文 + 带外投递合计至多一条，且 stdout 正文首行不是那条祈使句。

    ⚠️ 两代假绿都收在 ``_rvf_test_support.sole_self_injection_invariants`` 里治了：第一代是只看
    payload **顶层**（Stop 的 ``additionalContext`` 嵌在 ``hookSpecificOutput`` 里）；第二代是只看
    payload（带外投递的正文根本不进 payload）。故 ``run_log_root`` 是必填参数。
    """
    return assert_agent_facing_initiation_message_invariants(
        payload, run_log_root=state_dir
    )


def test_in_kanban_task_dirty_stop_injects_decision_point_banner(tmp_path: Path) -> None:
    """task 内 + 显式钉住自注入 transport：仍必须是横幅，不是落地即执行的祈使句。

    ⚠️ 本轮之后 task 内的**默认** transport 已改为带外投递（见
    ``test_in_kanban_task_dirty_stop_delivers_banner_out_of_band``）。这条用例钉的是「P1 那道
    lifecycle 判据没有回退」——即便强制走自注入通道，出口也仍是横幅而非祈使句。故显式设置逃生口
    env，让它与 transport 的默认值解耦（否则它测的就变成另一件事了）。
    """
    repo = _init_dirty_repo(tmp_path / "dirty")
    state = tmp_path / "state"
    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=state,
        # 默认 fork mode（auto）+ 在 task 内：历史上这一组合推出 backend=kanban-followup，
        # 被当成「延续」而绕过决策点。P1 之后必须走横幅。
        extra_env={
            "KANBAN_TASK_ID": "task-sole-injection",
            "RVF_DECISION_POINT_DELIVERY_TRANSPORT": "stop_hook_self_injection",
        },
    )
    assert payload.get("decision") == "block", payload
    reason = str(payload.get("reason") or "")
    assert reason.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE, reason[:400]
    assert "RVF_DECISION_TOKEN=" in reason, reason[:400]
    _assert_sole_initiation_message(payload, state)


def test_outside_kanban_task_dirty_stop_injects_decision_point_banner(tmp_path: Path) -> None:
    repo = _init_dirty_repo(tmp_path / "dirty")
    state = tmp_path / "state"
    payload = _invoke_stop_hook(repo=repo, state_dir=state)
    assert payload.get("decision") == "block", payload
    reason = str(payload.get("reason") or "")
    assert reason.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE, reason[:400]
    _assert_sole_initiation_message(payload, state)


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

    body = _assert_sole_initiation_message(payload, state)
    assert body is not None, payload
    # 走的确实是 reengage，不是发起决策点——否则这条用例又退回去测第一支了。
    assert DECISION_POINT_BANNER_FIRST_LINE not in body, body[:400]
    assert "rvf-inv1-in-flight" in body, body[:400]
    assert channel in payload, payload
    # AUD-4：每条 agent-directive 都要带 developer-only 旁注，否则终端里与真崩溃不可分辨。
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"], payload


# --- 第 5/6 组：带外投递（横幅成为一条真实 user message） ----------------------------

_STUB_CLINE_KANBAN_CLIENT = '''#!/usr/bin/env python3
"""测试替身：冒充 ``cline_kanban_client.py message``，回一份可控的终端投递回执。

它同时把收到的 ``--prompt-file`` 正文原样另存一份，供用例断言「投出去的确实是横幅原文」。
"""
import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("subcommand")
    parser.add_argument("--repo")
    parser.add_argument("--task-cmd")
    parser.add_argument("--task-id")
    parser.add_argument("--prompt-file")
    parser.add_argument("--source")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--attempt-id")
    parser.add_argument("--wait-for-terminal-status-timeout-ms")
    args, _unknown = parser.parse_known_args()
    capture = os.environ["RVF_TEST_DELIVERED_PROMPT_CAPTURE_PATH"]
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as handle:
            body = handle.read()
        with open(capture, "w", encoding="utf-8") as handle:
            handle.write(body)
    receipt = {
        "message_id": f"terminal:{args.task_id}:{args.idempotency_key}",
        "task_id": args.task_id,
        "idempotency_key": args.idempotency_key,
        "terminal_delivery_status": os.environ["RVF_TEST_TERMINAL_DELIVERY_STATUS"],
    }
    failure_reason = os.environ.get("RVF_TEST_TERMINAL_DELIVERY_FAILURE_REASON")
    if failure_reason:
        receipt["terminal_delivery_failure_reason"] = failure_reason
    sys.stdout.write(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_stub_cline_kanban_client(path: Path) -> Path:
    path.write_text(_STUB_CLINE_KANBAN_CLIENT, encoding="utf-8")
    return path


def test_in_kanban_task_dirty_stop_delivers_banner_out_of_band(tmp_path: Path) -> None:
    """本轮存在的理由：task 内的默认 transport 是**带外投递**，横幅成为一条真实 user message。

    断言四件事：
    1. 当前会话的 stdout **不驱动 agent**（既无 ``decision`` 也无 ``hookSpecificOutput``，
       只有给开发者看的 ``systemMessage``）——D3 的「走 developer_only，不加第三个 audience」；
    2. 带外投递恰好一条（INV-1 在两条 transport 上合计 ≤ 1）；
    3. 粘贴通道投出去的**只有一行无指令的边界标记**：Claude Code 把粘贴包成
       ``<pasted_content>``，其中的指令只有用户亲口要求时才执行，正文放在粘贴里 agent 会反问开发者；
    4. 正文（带 kanban 变体的时效说明、不带任何投递元信息）在投递前已写进 token payload，
       供目标会话的 UPS 以 additionalContext 注入。
    """
    repo = _init_dirty_repo(tmp_path / "dirty")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    capture = state / "delivered-prompt.md"
    client = _write_stub_cline_kanban_client(state / "stub-cline-kanban-client.py")

    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=state,
        extra_env={
            "KANBAN_TASK_ID": "task-out-of-band",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(client),
            "RVF_TEST_DELIVERED_PROMPT_CAPTURE_PATH": str(capture),
            # 默认回执：契约唯一的**非终态**，且因为短等默认 0ms，它是最常见的返回。
            "RVF_TEST_TERMINAL_DELIVERY_STATUS": "accepted_pending_submit_confirmation",
        },
    )

    assert "decision" not in payload, payload
    assert "hookSpecificOutput" not in payload, payload
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"], payload
    assert "cline-kanban" in str(payload["systemMessage"]), payload["systemMessage"]

    deliveries = out_of_band_decision_point_deliveries(state)
    assert len(deliveries) == 1, deliveries
    token = deliveries[0].get("decision_token")
    assert isinstance(token, str) and token, deliveries[0]

    delivered_prompt = capture.read_text(encoding="utf-8")
    assert len(delivered_prompt.strip().splitlines()) == 1, delivered_prompt
    assert f"RVF_DECISION_TOKEN={token}" in delivered_prompt
    for instruction_fragment in (
        DECISION_POINT_BANNER_FIRST_LINE,
        "rvf_agent_decision_cli",
        " start --token",
        " decline --token",
        "候选摘要",
    ):
        assert instruction_fragment not in delivered_prompt, (instruction_fragment, delivered_prompt)

    # token 停在 minted：推进权交给 UPS（观测到 needle 才是「agent 确实拿到了」）。
    token_path = state / "rvf-home" / "agentic-initiation-decision-tokens" / f"{token}.json"
    token_payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert token_payload["state"] == "minted", token_payload
    body_for_agent = token_payload["decision_point_banner_body_text_for_agent"]
    assert body_for_agent.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE
    assert f"RVF_DECISION_TOKEN={token}" in body_for_agent
    assert f" start --token {token}" in body_for_agent
    assert "时效：这枚 token 有 TTL" in body_for_agent
    assert "开发者已预先授权你独立做这个决定" in body_for_agent
    # 投递元信息不进正文：「不是开发者打的」在粘贴防注入规则下会被读成「不要执行」。
    for meta_fragment in ("非自动发起", "不是开发者打的", "rewind", "来源："):
        assert meta_fragment not in body_for_agent, meta_fragment
    assert (
        token_payload["decision_point_delivery_transport"] == "cline_kanban_task_chat_session"
    ), token_payload
    assert (
        token_payload["out_of_band_delivery_status"] == "accepted_pending_submit_confirmation"
    ), token_payload

    # 投递意图已落盘并标成「送出去了、等 agent 接到」。
    intents = sorted(
        (state / "kanban-followup-in-progress" / "kanban-decision-point-delivery-intent").glob(
            "task-*.json"
        )
    )
    assert len(intents) == 1, intents
    intent = json.loads(intents[0].read_text(encoding="utf-8"))
    assert intent["decision_token"] == token, intent
    assert intent["state"] == "sent_awaiting_agent_receipt", intent

    _assert_sole_initiation_message(payload, state)


def test_out_of_band_delivery_definite_failure_falls_back_to_self_injection(
    tmp_path: Path,
) -> None:
    """INV-B 的「确定失败」那一半：``delivery_failed`` ⇒ 当场回落自注入，且带外投递计数为 0。

    没有这一条，上一条用例就可能因为「带外是唯一出口」而 vacuous——也看不出 INV-1 在
    「投出去了又自注入一次」这个具体失败场景上是否真的成立。
    """
    repo = _init_dirty_repo(tmp_path / "dirty")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    capture = state / "delivered-prompt.md"
    client = _write_stub_cline_kanban_client(state / "stub-cline-kanban-client.py")

    payload = _invoke_stop_hook(
        repo=repo,
        state_dir=state,
        extra_env={
            "KANBAN_TASK_ID": "task-out-of-band-failed",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(client),
            "RVF_TEST_DELIVERED_PROMPT_CAPTURE_PATH": str(capture),
            "RVF_TEST_TERMINAL_DELIVERY_STATUS": "delivery_failed",
            "RVF_TEST_TERMINAL_DELIVERY_FAILURE_REASON": "no_active_terminal_session",
        },
    )

    assert payload.get("decision") == "block", payload
    reason = str(payload.get("reason") or "")
    assert reason.splitlines()[0].strip() == DECISION_POINT_BANNER_FIRST_LINE, reason[:400]
    # 回落的是**自注入变体**：kanban 变体那行时效说明不该出现在当前会话的注入正文里。
    assert "时效：这枚 token 有 TTL" not in reason, reason[:600]
    # 降级必须响亮：开发者旁注要说清「这一条不会成为可 rewind 的 user message」。
    assert "回落" in str(payload.get("systemMessage") or ""), payload.get("systemMessage")

    assert out_of_band_decision_point_deliveries(state) == []
    # 确定失败即清意图：留一份 active 意图只会把接下来 15 分钟的重新决策白白挡住。
    intent_dir = (
        state / "kanban-followup-in-progress" / "kanban-decision-point-delivery-intent"
    )
    assert sorted(intent_dir.glob("task-*.json")) == []

    _assert_sole_initiation_message(payload, state)


if __name__ == "__main__":  # pragma: no cover - 与仓内其它 pytest 文件一致的直跑入口
    raise SystemExit(pytest.main([__file__, "-q"]))
