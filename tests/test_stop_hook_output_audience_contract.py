#!/usr/bin/env python3
"""结构性 + 行为 gate：Stop hook 出口的**受众分级**契约。

护的不变量：

* **AUD-1（结构）** —— 驱动 agent 的线上键（``decision`` / ``hookSpecificOutput``）只允许在
  ``core/stop_hook_output_audience`` 的翻译层里被构造；hook 出口文件里出现即红，除非在本文件
  的显式豁免表里带理由登记。
* **AUD-2（行为·Claude）** —— 受众 ``agent_must_act_now`` 在 Claude host 下必须落
  ``hookSpecificOutput.additionalContext``，**且不带** ``decision``（否则 harness 把刻意注入
  渲染成 ``Stop hook error:``，历史上 92/92 条全是这种假阳性）。
* **AUD-3（行为·Codex）** —— 同一条决策在 Codex host（含 host 未知兜底）下必须落
  ``decision:block`` + ``reason``，**且不带** ``hookSpecificOutput``（Codex 的
  ``stop.command.output`` 是 ``additionalProperties:false``，多一个未知键整条 hook 输出 Failed）。
* **AUD-4（可分辨）** —— 每条 agent-directive 出口都必须带 ``systemMessage``：没有它，开发者在
  终端里无法把「RVF 刻意注入」与「hook 真崩了」区分开。

**为什么是 AST 而不是字面量扫描**：``"decision"`` / ``"block"`` 这两个词在本仓的注释、docstring、
常量元组（如 ``PLAN_DOC_REVIEW_NAME_MARKERS`` 里的 ``"decision"``）里到处都是——按字面量扫会
**假红**；而 ``dict(decision=…)`` / ``payload["decision"] = …`` / 常量拼接又会让字面量扫**假绿**。
本 gate 走 AST，三种构造形态全覆盖。

**扫描集不是手写清单**：从 ``hooks/hooks.json`` 注册的 shim 出发，再经 AST 取每个 shim 的
``core_script=(…)`` 元组解析出它转发的核心脚本。新增一个 hook 而忘了登记，扫描集会自动纳入它，
不会静默漏扫。

⚠️ 自扫型 checker 假绿前例：本文件由 ``scripts/check_skill_contracts.sh`` 的
``repo_required_files`` 清单要求存在 + 一条 ``run_step`` pytest 条目执行。新增此类 gate 时务必在
**tracked** 状态下先验一次真红，否则 untracked 期的自扫会给出假绿。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "review-validate-fix"
HOOKS_DIR = PLUGIN_ROOT / "hooks"
STOP_HOOK_CORE = (
    PLUGIN_ROOT / "skills" / "review-validate-fix" / "scripts" / "stop_review_validate_fix.py"
)
AUDIENCE_CONTRACT_MODULE = (
    ROOT / "core" / "stop_hook_output_audience" / "stop_hook_output_audience_contract.py"
)

AGENT_DRIVING_PAYLOAD_KEYS = ("decision", "hookSpecificOutput")

# ── 两层扫描 ────────────────────────────────────────────────────────────────
# **Stop 层（严格）**：Stop 事件的出口文件。这两个键只允许在翻译层里被构造。
# **非-Stop 层（登记制）**：UserPromptSubmit / PostToolUse 的出口文件。同名键在那儿语义完全
#   不同——UPS 的 ``additionalContext`` 是搭用户**这一条** prompt 的车、不额外开回合；UPS 的
#   ``decision:block`` 是**挡掉这次提交**（prompt 抑制），不是「驱动 agent 再跑一回合」。
#   Stop 的受众分级不适用，但每处构造仍须在下表登记理由，防止无声漂移。
SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS: dict[tuple[str, str], str] = {
    (
        "core/stop_hook_output_audience/stop_hook_output_audience_contract.py",
        "render_stop_hook_payload",
    ): "受众→host 线材的唯一翻译层，本契约的法定归属地。",
    (
        "core/stop_hook_output_audience/stop_hook_output_audience_contract.py",
        "_self_check",
    ): "翻译层自检：必须能写出两条通道的期望形状才能断言它们。",
    (
        "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
        "_apply_withdrawn_dispatch_hard_neutralization",
    ): (
        "UPS 的 decision:block = 挡掉这次提交（prompt 抑制），与 Stop 的「驱动 agent 再跑一回合」"
        "是两回事。这条硬中和刻意三管齐下（decision + additionalContext + systemMessage）："
        "撤回可能在「查 tombstone」与「抢 consumed」之间才胜出，只写一句提示不可靠。"
    ),
    (
        "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
        "_render_hook_payload",
    ): "同上机制的出口端：把硬中和的键与 manual 决策点的 additionalContext 透传到 UPS stdout。",
    (
        "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
        "_handle_manual_decision_point",
    ): "UPS turn-riding injection：manual 决策点搭用户自己那条 prompt 的车，不额外开回合。",
    (
        "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
        "inspect_user_prompt_submit",
    ): "UPS turn-riding injection：bootstrap checkpoint 上下文，同样搭用户这一条 prompt 的车。",
}

STOP_EVENT_NAME = "Stop"


def _registered_hook_shims() -> dict[Path, str]:
    """从 ``hooks.json`` 解析出 shim 文件，并 AST 取它自报的 ``event_name``（不手写清单）。"""
    registry = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    shims: dict[Path, str] = {}
    for registered_event, entries in registry["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                command = hook["command"]
                marker = "${CLAUDE_PLUGIN_ROOT}/"
                assert marker in command, command
                relative = command.split(marker, 1)[1].strip().strip('"')
                path = PLUGIN_ROOT / relative
                declared = _declared_event_name(path)
                assert declared == registered_event, (
                    f"{relative} 自报 event_name={declared!r}，但 hooks.json 把它注册在 "
                    f"{registered_event!r} 下——注册与实现不一致。"
                )
                shims[path] = registered_event
    return shims


def _hook_shim_call(shim: Path) -> ast.Call | None:
    tree = ast.parse(shim.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_claude_hook"
        ):
            return node
    return None


def _declared_event_name(shim: Path) -> str | None:
    call = _hook_shim_call(shim)
    if call is None:
        return None
    for keyword in call.keywords:
        if keyword.arg == "event_name" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _core_script_forwarded_by(shim: Path) -> Path | None:
    """AST 取 shim 里 ``core_script=(…)`` 的字符串元组，解析成它转发的核心脚本路径。"""
    call = _hook_shim_call(shim)
    if call is None:
        return None
    for keyword in call.keywords:
        if keyword.arg != "core_script" or not isinstance(keyword.value, ast.Tuple):
            continue
        parts = [
            element.value
            for element in keyword.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if len(parts) == len(keyword.value.elts):
            return PLUGIN_ROOT.joinpath(*parts)
    return None


def _stop_tier_files() -> list[Path]:
    """严格层：Stop 事件的出口文件 + 翻译层 + 全 shim 共享的 fail-open 骨架。

    ``_claude_hook_entry`` 归严格层是刻意的：它在核心异常 / 空输出时**自己** emit，是 core
    收口之外的第三个出口；把它钉在「只准发 developer-only」上，才不会有人顺手在 fail-open
    路径上加一条驱动 agent 的键。
    """
    files: set[Path] = {AUDIENCE_CONTRACT_MODULE, HOOKS_DIR / "_claude_hook_entry.py"}
    for shim, event in _registered_hook_shims().items():
        if event != STOP_EVENT_NAME:
            continue
        files.add(shim)
        core = _core_script_forwarded_by(shim)
        if core is not None:
            files.add(core)
    return sorted(path for path in files if path.exists())


def _non_stop_tier_files() -> list[Path]:
    """登记制层：非-Stop 事件的出口文件（同名键在那儿语义不同，但不许无声漂移）。"""
    files: set[Path] = set()
    for shim, event in _registered_hook_shims().items():
        if event == STOP_EVENT_NAME:
            continue
        files.add(shim)
        core = _core_script_forwarded_by(shim)
        if core is not None:
            files.add(core)
    return sorted(path for path in files if path.exists())


def _enclosing_function_name(tree: ast.Module, target: ast.AST) -> str:
    """返回包含 ``target`` 的**最内层**函数名；不在任何函数内则返回 ``"<module>"``。"""
    best = "<module>"
    best_span = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None or not (node.lineno <= target.lineno <= end):
            continue
        span = end - node.lineno
        if best_span is None or span < best_span:
            best, best_span = node.name, span
    return best


def _agent_driving_key_constructions(path: Path) -> list[tuple[str, int, str]]:
    """找出该文件里**构造**驱动-agent 键的三种形态：dict 字面量 / dict(**kw) / 下标赋值。

    只算「写」，不算「读」——``payload.get("decision")`` / ``if "decision" in payload`` 是消费者，
    不产出线上形状，不该被本 gate 判红。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int, str]] = []

    def record(node: ast.AST, key: str) -> None:
        hits.append((key, node.lineno, _enclosing_function_name(tree, node)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key_node in node.keys:
                if (
                    isinstance(key_node, ast.Constant)
                    and key_node.value in AGENT_DRIVING_PAYLOAD_KEYS
                ):
                    record(node, key_node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            for keyword in node.keywords:
                if keyword.arg in AGENT_DRIVING_PAYLOAD_KEYS:
                    record(node, keyword.arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in AGENT_DRIVING_PAYLOAD_KEYS
                ):
                    record(target, target.slice.value)
    return hits


def test_scan_set_covers_every_registered_hook_and_its_core_script() -> None:
    """非空 + 非 vacuous：两层扫描集必须真的涵盖 Stop hook 核心与全部已注册 shim。"""
    stop_tier = _stop_tier_files()
    assert STOP_HOOK_CORE in stop_tier, stop_tier
    assert AUDIENCE_CONTRACT_MODULE in stop_tier, stop_tier
    assert HOOKS_DIR / "_claude_hook_entry.py" in stop_tier, stop_tier
    assert HOOKS_DIR / "stop.py" in stop_tier, stop_tier

    non_stop_tier = _non_stop_tier_files()
    ups_core = (
        PLUGIN_ROOT / "skills" / "review-validate-fix" / "scripts" / "rvf_user_prompt_submit.py"
    )
    assert ups_core in non_stop_tier, non_stop_tier
    assert not set(stop_tier) & set(non_stop_tier), "同一文件不得同时落两层"


@pytest.mark.parametrize("path", _stop_tier_files(), ids=lambda p: p.name)
def test_stop_tier_builds_agent_driving_keys_only_in_the_translation_layer(path: Path) -> None:
    """AUD-1（严格层）：Stop 出口文件里，驱动 agent 的线上键只能在翻译层里被构造。"""
    relative = path.relative_to(ROOT).as_posix()
    offending = [
        (key, lineno, function)
        for key, lineno, function in _agent_driving_key_constructions(path)
        if (relative, function) not in SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS
    ]
    assert not offending, (
        f"{relative} 在翻译层之外构造了驱动 agent 的线上键：{offending}。\n"
        "core 只该声明 audience（见 core/stop_hook_output_audience），线材由 host adapter 决定。"
    )


@pytest.mark.parametrize("path", _non_stop_tier_files(), ids=lambda p: p.name)
def test_non_stop_tier_constructions_are_all_registered_with_a_reason(path: Path) -> None:
    """AUD-1（登记制层）：非-Stop 出口不受 Stop 受众分级管辖，但每处构造都得有登记理由。

    这样新增一处就会红，逼着写清「为什么它不是 Stop 那套语义」，而不是无声混进来。
    """
    relative = path.relative_to(ROOT).as_posix()
    unregistered = [
        (key, lineno, function)
        for key, lineno, function in _agent_driving_key_constructions(path)
        if (relative, function) not in SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS
    ]
    assert not unregistered, (
        f"{relative} 新增了未登记的 decision / hookSpecificOutput 构造：{unregistered}。\n"
        "若它确实是 UPS/PostToolUse 语义（搭车注入 / prompt 抑制），请在 "
        "SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS 里带理由登记；若它其实是 Stop 出口，"
        "请改走 core/stop_hook_output_audience 的翻译层。"
    )


def test_every_sanctioned_exemption_still_points_at_real_code() -> None:
    """豁免表不许烂尾：登记的 (文件, 函数) 必须仍然存在且确实构造那些键，否则就是过期豁免。"""
    for (relative, function), reason in SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS.items():
        path = ROOT / relative
        assert path.exists(), f"豁免表指向不存在的文件：{relative}"
        assert reason.strip(), f"豁免必须写理由：{relative}::{function}"
        functions = {
            hit_function for _key, _lineno, hit_function in _agent_driving_key_constructions(path)
        }
        assert function in functions, (
            f"过期豁免：{relative}::{function} 已不再构造驱动 agent 的键，请删掉这条豁免。"
        )


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
        "session_id": "session-audience-contract",
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


def test_claude_host_agent_directive_lands_on_the_non_error_channel(tmp_path: Path) -> None:
    """AUD-2 + AUD-4：Claude host 下发起决策点走 additionalContext，无 decision，且带 systemMessage。"""
    payload = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "dirty"),
        state_dir=tmp_path / "state",
        # Claude plugin 入口亲手打的 host 标（hooks/_claude_hook_entry.py:114）。
        event_overrides={"source": {"provider": "claude-code", "plugin": "review-validate-fix"}},
    )
    hook_specific = payload.get("hookSpecificOutput")
    assert isinstance(hook_specific, dict), payload
    assert hook_specific.get("hookEventName") == "Stop", payload
    body = hook_specific.get("additionalContext")
    assert isinstance(body, str) and "RVF_DECISION_TOKEN=" in body, payload
    assert "decision" not in payload, (
        "Claude 下仍走阻塞通道 ⇒ harness 会把这条刻意注入渲染成 Stop hook error:，"
        "而历史上 92/92 条这类「错误」一条真失败也没有。"
    )
    assert "reason" not in payload, payload
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"], payload


def test_codex_host_agent_directive_lands_on_the_blocking_channel(tmp_path: Path) -> None:
    """AUD-3 + AUD-4：未标明 host（兜底 Codex）下走 decision:block，且**不得**发 hookSpecificOutput。"""
    payload = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "dirty"),
        state_dir=tmp_path / "state",
        event_overrides={},
    )
    assert payload.get("decision") == "block", payload
    assert "RVF_DECISION_TOKEN=" in str(payload.get("reason") or ""), payload
    assert "hookSpecificOutput" not in payload, (
        "Codex 的 stop.command.output 是 additionalProperties:false——多发一个未知键会让整条 "
        "hook 输出被判 Failed，等于 RVF 在 Codex 上静默停摆。"
    )
    assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"], payload


def test_both_hosts_deliver_the_same_agent_facing_body(tmp_path: Path) -> None:
    """同一条决策，两条线材上喂给模型的正文必须是同一份（差别只在标签，不在内容）。

    正文里嵌了每次都不同的 token，故比对首行 + 结构性锚点，而非整串相等。
    """
    claude = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "claude-repo"),
        state_dir=tmp_path / "claude-state",
        event_overrides={"source": {"provider": "claude-code", "plugin": "review-validate-fix"}},
    )
    codex = _invoke_stop_hook_core(
        repo=_init_dirty_repo(tmp_path / "codex-repo"),
        state_dir=tmp_path / "codex-state",
        event_overrides={},
    )
    claude_body = claude["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    codex_body = str(codex["reason"])
    assert claude_body.splitlines()[0] == codex_body.splitlines()[0], (claude_body[:200], codex_body[:200])
    for anchor in ("RVF_DECISION_TOKEN=", "changed.txt"):
        assert anchor in claude_body and anchor in codex_body, anchor


if __name__ == "__main__":  # pragma: no cover - 与仓内其它 pytest 文件一致的直跑入口
    raise SystemExit(pytest.main([__file__, "-q"]))
