#!/usr/bin/env python3
"""行为 gate：**当前会话 host** 的判定必须偏向保守——先否定 Claude，再采信 stamp。

护的不变量：

* **HOST-1** —— 任何一条「这是 Codex rollout」的正向证据（会话路径落在
  ``~/.codex/sessions/`` 或 ``$CODEX_HOME/sessions/`` 下；transcript 首批 record 是 Codex
  rollout schema）都必须**压过** ``event["source"]["provider"] == "claude-code"`` stamp。
* **HOST-2** —— 没有任何 Codex 反证时，stamp 仍然算数（Claude 会话该拿 ``additionalContext``
  就得拿到），包括 transcript 读不到 / schema 不认识的场合。
* **HOST-3** —— ``_claude_hook_entry.is_foreign_invocation`` 与核心侧用**同一套路径判据**：
  会话路径键取全集（含 ``log_path``），且认 ``$CODEX_HOME/sessions/``。
* **HOST-4（端到端）** —— 带 ``claude-code`` stamp 的**真 Codex 会话**，其 Stop 出口必须落
  ``decision:block``，**不得**出现 ``hookSpecificOutput``。
* **HOST-5（Kimi）** —— Kimi wire schema 的证据必须**压过** stamp。Kimi 方向的误判代价比
  Claude 方向重得多：见下方第三条不对称性。
* **HOST-6（端到端·Kimi）** —— Kimi 会话的 Stop 出口必须落
  ``hookSpecificOutput.permissionDecision == "deny"``，**不得**出现顶层 ``decision``、
  **不得**出现 ``additionalContext``。
* **HOST-7（kimi-code stamp，HOST-2 的 Kimi 对偶）** —— 没有任何 schema / 路径反证时，
  ``kimi-code`` stamp 必须算数。Kimi 的 Stop 事件原本**没有** ``transcript_path``，那个键由
  shim 合成，合成有两条正常的落空路径（会话目录未找到 / ``wire.jsonl`` 尚未落盘）；落空即
  探测为 ``None`` ⇒ 兜底 Codex ⇒ 发 ``decision:block`` ⇒ 静默 allow。
  同时钉住两条**方向性**约束：（a）Codex 的两条正向证据仍压过这枚新 stamp；（b）新 stamp
  不得抢走一个内容上明确是 Claude 的会话——所以 **所有 schema 证据整层排在所有 stamp 之前**。

**为什么这条顺序值得一个专门的 gate**：两个方向的误判代价严重不对称。

- Claude 被误判成 Codex：只是少了个非错误通道（降级到 ``decision:block``），仍能工作。
- Codex 被误判成 Claude：出口里带上 ``hookSpecificOutput``，而 Codex 的
  ``stop.command.output`` 是 ``additionalProperties: false`` ⇒ **整条 hook 输出被判 Failed**，
  RVF 在 Codex 上静默停摆（``codex exec`` 下零警告，没有任何东西会提醒你）。
- **Kimi 被误判成任何一家：静默 allow、零报错、RVF 完全失声。** Kimi 的 hook 输出 schema 是
  ``looseObject({message, hookSpecificOutput})``，block 判据逐字
  ``hookSpecificOutput?.permissionDecision !== "deny"`` ⇒ 顶层 ``decision`` 与
  ``additionalContext`` 都落在未命名区、从不被读取。**「未知兜底 Codex」这条纪律对 Kimi
  不提供任何保护**——Codex 方向的兜底之所以安全，正是因为它的失败是响亮的；Kimi 只能靠
  正向识别。所以 Kimi 的 schema 判据必须排在 stamp 之前。

  2026-08-23 实测：不加这条时，一个带 ``claude-code`` stamp 的 Kimi 会话确实被判成 Claude。
  这不是假想场景——Kimi 的 Stop 事件只有 ``hook_event_name`` / ``session_id`` /
  ``stop_hook_active``，shim 要做大量事件归一，复用或误配 Claude 入口逻辑就会带上那枚 stamp。

而那枚 stamp 是 ``hooks/_claude_hook_entry.py`` 在**它自己那道 foreign check 之后**
``setdefault`` 上去的：check 一旦漏判（自定义 ``CODEX_HOME``、或事件只带它没查的路径键），
一个真实的 Codex 会话就带着 ``claude-code`` stamp 走进核心。所以 stamp 只能当「没有反证时
的正向 Claude 证据」，绝不能当「先信它、再看别的」的第一判据。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "review-validate-fix"
STOP_HOOK_CORE = (
    PLUGIN_ROOT / "skills" / "review-validate-fix" / "scripts" / "stop_review_validate_fix.py"
)
CLAUDE_HOOK_ENTRY = PLUGIN_ROOT / "hooks" / "_claude_hook_entry.py"

# Claude plugin 入口亲手打的 host 标（hooks/_claude_hook_entry.py 的 setdefault）。
CLAUDE_PLUGIN_STAMP = {"provider": "claude-code", "plugin": "review-validate-fix"}
# Kimi plugin 入口亲手打的 host 标（hooks/_kimi_hook_entry.py 的 setdefault）。
KIMI_PLUGIN_STAMP = {"provider": "kimi-code", "plugin": "review-validate-fix"}

CODEX_ROLLOUT_FIRST_RECORD = {"type": "session_meta", "payload": {"id": "rollout-under-test"}}
CLAUDE_TRANSCRIPT_FIRST_RECORD = {
    "type": "user",
    "message": {"role": "user", "content": "实现完了。"},
}
# Kimi 的 per-agent wire.jsonl 首行恒为 metadata（实测本机 42/42 份）。
KIMI_WIRE_FIRST_RECORD = {"type": "metadata", "protocol_version": "1.4", "created_at": 1787371449575}


def _isolated_module_environment() -> dict[str, str]:
    """import 期临时摘掉 RVF_* / CODEX_RVF_*：不让宿主会话的 RVF 状态根漏进测试。"""
    return {
        name: value
        for name, value in os.environ.items()
        if name.startswith(("RVF_", "CODEX_RVF_"))
    }


def _load_module(path: Path, name: str):
    saved = _isolated_module_environment()
    for key in saved:
        os.environ.pop(key, None)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.update(saved)


def _stop_hook_core():
    return _load_module(STOP_HOOK_CORE, "rvf_stop_hook_for_host_resolution_tests")


def _claude_hook_entry():
    return _load_module(CLAUDE_HOOK_ENTRY, "rvf_claude_hook_entry_for_host_resolution_tests")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _resolved_host_name(event: dict[str, object]) -> str:
    return _stop_hook_core().resolve_current_session_host_adapter(event).host_display_name()


# ── HOST-1：Codex 正向证据压过 claude-code stamp ───────────────────────────────


def test_default_codex_sessions_path_beats_claude_code_stamp(tmp_path: Path) -> None:
    """带 stamp **且** 会话路径落在默认 Codex 会话目录下 —— 必须解析成 Codex。"""
    rollout = _write_jsonl(
        tmp_path / "home" / ".codex" / "sessions" / "2026" / "08" / "rollout-x.jsonl",
        [CODEX_ROLLOUT_FIRST_RECORD],
    )
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(rollout)})
        == "Codex"
    )


def test_custom_codex_home_sessions_path_beats_claude_code_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CODEX_HOME`` 被改过 ⇒ 路径里没有 ``/.codex/sessions/`` 字面量，仍须判成 Codex。"""
    codex_home = tmp_path / "elsewhere" / "codex-home"
    rollout = _write_jsonl(codex_home / "sessions" / "rollout-y.jsonl", [CODEX_ROLLOUT_FIRST_RECORD])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(rollout)})
        == "Codex"
    )


def test_codex_evidence_carried_only_by_log_path_is_not_missed(tmp_path: Path) -> None:
    """证据只挂在 ``log_path`` 上（守卫历史上漏查的那个键）也必须判成 Codex。"""
    rollout = _write_jsonl(
        tmp_path / "home" / ".codex" / "sessions" / "rollout-z.jsonl", [CODEX_ROLLOUT_FIRST_RECORD]
    )
    assert _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "log_path": str(rollout)}) == "Codex"


def test_codex_rollout_schema_beats_stamp_without_any_path_convention(tmp_path: Path) -> None:
    """路径约定完全失效（rollout 放在任意目录）时，transcript schema 是最后的真相源。"""
    rollout = _write_jsonl(tmp_path / "neutral" / "session.jsonl", [CODEX_ROLLOUT_FIRST_RECORD])
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(rollout)})
        == "Codex"
    )


# ── HOST-2：没有反证时 stamp 仍然算数 ─────────────────────────────────────────


def test_claude_transcript_with_stamp_still_resolves_claude(tmp_path: Path) -> None:
    """对偶方向的回归闸：真 Claude 会话不许被这次收紧误伤成 Codex。"""
    transcript = _write_jsonl(
        tmp_path / "home" / ".claude" / "projects" / "-encoded" / "session.jsonl",
        [CLAUDE_TRANSCRIPT_FIRST_RECORD],
    )
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(transcript)})
        == "Claude Code"
    )


def test_stamp_still_decides_when_transcript_is_unreadable(tmp_path: Path) -> None:
    """transcript 读不到 / schema 不认识时，stamp 是唯一的 Claude 线索，仍须被采信。"""
    missing = tmp_path / "gone" / "session.jsonl"
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(missing)})
        == "Claude Code"
    )


def test_no_evidence_at_all_falls_back_to_codex() -> None:
    """全落空 → 兜底到最保守的通道（Codex + decision:block），不赌未知 host。"""
    assert _resolved_host_name({}) == "Codex"


# ── HOST-5：Kimi wire schema 压过 stamp（误判即静默失声）────────────────────────


def test_kimi_wire_schema_resolves_kimi(tmp_path: Path) -> None:
    """纯 Kimi 证据（无 stamp）必须解析成 Kimi，而不是落进「未知 → Codex」兜底。"""
    wire = _write_jsonl(
        tmp_path / "kimi-home" / "sessions" / "wd_proj_abc" / "session_x" / "agents" / "main" / "wire.jsonl",
        [KIMI_WIRE_FIRST_RECORD],
    )
    assert _resolved_host_name({"transcript_path": str(wire)}) == "Kimi Code CLI"


def test_kimi_wire_schema_beats_claude_code_stamp(tmp_path: Path) -> None:
    """**本 gate 的核心用例**：Kimi 的内容证据必须压过 stamp。

    2026-08-23 实测：判据链没有这条分支时，该事件被解析成 Claude Code ⇒ 出口落
    ``additionalContext`` ⇒ Kimi 的 looseObject 静默吞掉 ⇒ allow ⇒ RVF 完全失声、零报错。
    """
    wire = _write_jsonl(tmp_path / "neutral" / "wire.jsonl", [KIMI_WIRE_FIRST_RECORD])
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(wire)})
        == "Kimi Code CLI"
    )


def test_codex_evidence_still_wins_over_kimi_shaped_events(tmp_path: Path) -> None:
    """对偶回归闸：新增 Kimi 分支不得削弱「Codex 正向证据最优先」这条既有不变量。"""
    rollout = _write_jsonl(
        tmp_path / "home" / ".codex" / "sessions" / "rollout-vs-kimi.jsonl",
        [CODEX_ROLLOUT_FIRST_RECORD],
    )
    assert (
        _resolved_host_name({"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(rollout)})
        == "Codex"
    )


def test_every_host_gets_a_distinct_stop_hook_wire(tmp_path: Path) -> None:
    """三家的出口线材必须两两不同——共用即意味着其中一家在线上是静默失效的。"""
    core = _stop_hook_core()
    events = {
        "Codex": {"transcript_path": str(
            _write_jsonl(tmp_path / "c" / "rollout.jsonl", [CODEX_ROLLOUT_FIRST_RECORD])
        )},
        "Claude Code": {"transcript_path": str(
            _write_jsonl(tmp_path / "l" / "session.jsonl", [CLAUDE_TRANSCRIPT_FIRST_RECORD])
        )},
        "Kimi Code CLI": {"transcript_path": str(
            _write_jsonl(tmp_path / "k" / "wire.jsonl", [KIMI_WIRE_FIRST_RECORD])
        )},
    }
    channels = {}
    for expected_host, event in events.items():
        adapter = core.resolve_current_session_host_adapter(event)
        assert adapter.host_display_name() == expected_host, (expected_host, event)
        channels[expected_host] = adapter.stop_hook_agent_directive_channel()
    assert len(set(channels.values())) == 3, channels


# ── HOST-3：两侧守卫同判据 ────────────────────────────────────────────────────


def test_foreign_guard_uses_the_same_path_evidence_as_the_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_foreign_invocation`` 须覆盖全部会话路径键并认 ``$CODEX_HOME/sessions/``。

    同时钉住它的**保守侧**：本函数返回 True 会让 Claude 入口整体静默 no-op，故对非 Codex
    路径 / 缺键 / 非 str 值一律 False（误判成 foreign ⇒ RVF 在 Claude 上彻底不触发）。
    """
    entry = _claude_hook_entry()

    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.codex/sessions/2026/08/rollout-a.jsonl"}
    ) is True
    # 历史缺口：只挂在 log_path 上的 Codex 事件曾整个溜过守卫。
    assert entry.is_foreign_invocation(
        {"log_path": "/Users/me/.codex/sessions/2026/08/rollout-b.jsonl"}
    ) is True

    codex_home = tmp_path / "elsewhere" / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert entry.is_foreign_invocation(
        {"session_path": str(codex_home / "sessions" / "rollout-c.jsonl")}
    ) is True
    # CODEX_HOME 只用来定位 sessions 目录，绝不是「当前会话是 Codex」的身份信号。
    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.claude/projects/-encoded/session.jsonl"}
    ) is False

    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert entry.is_foreign_invocation({}) is False
    assert entry.is_foreign_invocation({"transcript_path": None}) is False
    assert entry.is_foreign_invocation({"transcript_path": 12345}) is False


# ── HOST-4：端到端，出口线材真的翻过去了 ──────────────────────────────────────


def _init_dirty_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    (path / "changed.txt").write_text("dirty\n", encoding="utf-8")
    return path


def _invoke_stop_hook_core(
    *, repo: Path, state_dir: Path, event_overrides: dict[str, object]
) -> dict[str, object]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("CODEX_RVF_", "RVF_", "KANBAN_", "CLINE_KANBAN_")):
            env.pop(name, None)
    env.pop("CODEX_THREAD_ID", None)
    # 隔离铁律：决策点会铸 token / 写注入尝试 ledger / declined 冷却，默认落盘根是 $RVF_HOME。
    env["RVF_HOME"] = str(state_dir / "rvf-home")
    env["CODEX_RVF_STATE_DIR"] = str(state_dir)
    env["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state_dir / "kanban-followup-in-progress")
    env["CODEX_SESSION_INDEX_PATH"] = str(state_dir / "empty-session-index.jsonl")
    event: dict[str, object] = {
        "cwd": str(repo),
        "stop_hook_active": False,
        "session_id": "session-current-host-resolution",
        "last_user_message": "实现完了，先停一下。",
    }
    event.update(event_overrides)
    completed = subprocess.run(
        [sys.executable, str(STOP_HOOK_CORE)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout or "{}")


def test_stamped_codex_session_stop_output_stays_on_the_blocking_channel(tmp_path: Path) -> None:
    """真 Codex 会话被打上 ``claude-code`` stamp 时，出口仍必须是 Codex schema 认识的形状。"""
    rollout = _write_jsonl(
        tmp_path / "home" / ".codex" / "sessions" / "2026" / "08" / "rollout-e2e.jsonl",
        [CODEX_ROLLOUT_FIRST_RECORD],
    )
    payload = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "dirty"),
        state_dir=tmp_path / "state",
        event_overrides={"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(rollout)},
    )
    assert "hookSpecificOutput" not in payload, (
        "Codex 的 stop.command.output 是 additionalProperties:false——多发一个未知键会让整条 "
        "hook 输出被判 Failed，等于 RVF 在 Codex 上静默停摆（codex exec 下零警告）。"
    )
    assert payload.get("decision") == "block", payload
    assert "RVF_DECISION_TOKEN=" in str(payload.get("reason") or ""), payload


def test_kimi_session_stop_output_lands_on_permission_decision_deny(tmp_path: Path) -> None:
    """HOST-6（端到端）：Kimi 会话的 Stop 出口必须是 Kimi **唯一**读得懂的那个形状。

    发错线材在 Kimi 上不会报错——只会 allow。所以这条只能靠 gate 钉，运行期没有任何信号。
    """
    wire = _write_jsonl(
        tmp_path / "kimi-home" / "sessions" / "wd_p_1" / "session_e2e" / "agents" / "main" / "wire.jsonl",
        [KIMI_WIRE_FIRST_RECORD],
    )
    payload = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "dirty"),
        state_dir=tmp_path / "state",
        event_overrides={"transcript_path": str(wire)},
    )
    assert "decision" not in payload, (
        f"Kimi 收到顶层 decision 会被 looseObject 静默吞掉并 allow（零报错）：{payload}"
    )
    inner = payload.get("hookSpecificOutput") or {}
    assert inner.get("permissionDecision") == "deny", payload
    assert "additionalContext" not in inner, payload
    assert "RVF_DECISION_TOKEN=" in str(inner.get("permissionDecisionReason") or ""), payload


# ── HOST-7：kimi-code stamp 是 HOST-2 的 Kimi 对偶 ─────────────────────────────


def test_kimi_code_stamp_decides_when_the_transcript_is_unreadable(tmp_path: Path) -> None:
    """``transcript_path`` 合成失败时，stamp 是唯一的 Kimi 线索，必须被采信。

    Kimi 的 Stop 事件原本**没有** ``transcript_path``——它由 shim 合成（会话目录 →
    ``agents/main/wire.jsonl``）。合成有两条正常的落空路径：会话目录还没建索引 / 扫桶没命中，
    以及 ``wire.jsonl`` 尚未落盘（``resolve_agent_wire_transcript_path`` 要求 ``is_file()``）。
    此时探测返回 ``None`` ⇒ 兜底 Codex ⇒ 发 ``decision:block`` ⇒ Kimi 的 looseObject
    静默 allow ⇒ RVF 完全失声。对照 HOST-2：Claude 在同样情形下仍采信自己的 stamp。
    """
    missing = tmp_path / "kimi-home" / "sessions" / "wd_p" / "session_x" / "agents" / "main" / "wire.jsonl"
    assert _resolved_host_name({"source": KIMI_PLUGIN_STAMP, "transcript_path": str(missing)}) == (
        "Kimi Code CLI"
    )
    # transcript_path 根本没被合成出来的那条路径（Kimi 原生 Stop 事件的形状）。
    assert _resolved_host_name(
        {"source": KIMI_PLUGIN_STAMP, "session_id": "session_x", "stop_hook_active": False}
    ) == "Kimi Code CLI"


def test_kimi_stamped_session_gets_the_kimi_wire_not_the_blocking_channel(tmp_path: Path) -> None:
    """判定对了还不够——出口线材必须真的翻到 Kimi 唯一读得懂的那个形状。"""
    adapter = _stop_hook_core().resolve_current_session_host_adapter(
        {"source": KIMI_PLUGIN_STAMP, "session_id": "session_x"}
    )
    assert adapter.stop_hook_agent_directive_channel() == (
        "hook_specific_output_permission_decision_deny"
    ), adapter.stop_hook_agent_directive_channel()


def test_codex_evidence_still_beats_the_kimi_code_stamp(tmp_path: Path) -> None:
    """**不得削弱 Codex 方向**：Codex 的正向证据仍须压过任何 stamp，包括新增的这枚。

    Codex 被误判的代价是整条 hook 输出 Failed（``additionalProperties:false``），比 Kimi
    方向更重且更难察觉，所以两条 Codex 判据必须继续排在所有 stamp 之前。
    """
    rollout_by_path = _write_jsonl(
        tmp_path / "home" / ".codex" / "sessions" / "2026" / "08" / "rollout-vs-kimi-stamp.jsonl",
        [CODEX_ROLLOUT_FIRST_RECORD],
    )
    assert _resolved_host_name(
        {"source": KIMI_PLUGIN_STAMP, "transcript_path": str(rollout_by_path)}
    ) == "Codex"
    # 路径约定完全失效时，schema 仍是真相源。
    rollout_by_schema = _write_jsonl(
        tmp_path / "neutral" / "session.jsonl", [CODEX_ROLLOUT_FIRST_RECORD]
    )
    assert _resolved_host_name(
        {"source": KIMI_PLUGIN_STAMP, "transcript_path": str(rollout_by_schema)}
    ) == "Codex"


def test_claude_transcript_schema_is_not_overridden_by_a_kimi_stamp(tmp_path: Path) -> None:
    """对偶回归闸：新增的 Kimi stamp 不得把一个内容上是 Claude 的会话抢走。"""
    transcript = _write_jsonl(
        tmp_path / "home" / ".claude" / "projects" / "-encoded" / "session.jsonl",
        [CLAUDE_TRANSCRIPT_FIRST_RECORD],
    )
    assert _resolved_host_name(
        {"source": KIMI_PLUGIN_STAMP, "transcript_path": str(transcript)}
    ) == "Claude Code"


def test_claude_code_stamp_behaviour_is_unchanged_by_the_new_kimi_stamp(tmp_path: Path) -> None:
    """HOST-2 的字面回归：加第二枚 stamp 不得改动 claude-code stamp 的任何既有结论。"""
    missing = tmp_path / "gone" / "session.jsonl"
    assert _resolved_host_name(
        {"source": CLAUDE_PLUGIN_STAMP, "transcript_path": str(missing)}
    ) == "Claude Code"
    assert _resolved_host_name({}) == "Codex"


def test_kimi_stamped_session_stop_output_lands_on_permission_decision_deny(tmp_path: Path) -> None:
    """端到端：只有 stamp、没有可读 transcript 的 Kimi 会话，出口仍不得落 ``decision``。"""
    payload = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "dirty"),
        state_dir=tmp_path / "state",
        event_overrides={"source": KIMI_PLUGIN_STAMP},
    )
    assert "decision" not in payload, (
        f"Kimi 收到顶层 decision 会被 looseObject 静默吞掉并 allow（零报错）：{payload}"
    )
    inner = payload.get("hookSpecificOutput") or {}
    assert inner.get("permissionDecision") == "deny", payload
    assert "additionalContext" not in inner, payload
