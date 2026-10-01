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
* **AUD-5（契约·Kimi）** —— 同一条决策在 Kimi 通道下必须落
  ``hookSpecificOutput.permissionDecision == "deny"`` + 非空 ``permissionDecisionReason``，
  **且不带顶层** ``decision``、**不带** ``additionalContext``。Kimi 的 hook 输出 schema 是
  ``looseObject``：发错线材会被**静默吞掉并 allow**，RVF 完全失声且零报错——所以这条只能靠
  gate 钉，不能指望运行期报错。
* **AUD-6（结构·退出码）** —— exit code 是 payload 之外的**第二个自由度**，且 Kimi 把
  ``exit 2`` 定义为 block（reason 取 stderr）。本仓刻意不用那条通道，故结构性封死两处：
  **(a)** Stop 严格层禁止任何**字面量非 0** 退出码；**(b)** Stop 核心 ``main()`` 的每个
  ``return`` 都必须是字面量 ``0``——只禁 (a) 会留下真缺口，因为 ``main`` 里一个 ``return 2``
  同样会经 ``raise SystemExit(main())`` 变成进程退出码 2。

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
        "_apply_void_decision_point_boundary_marker_hard_neutralization",
    ): (
        "作废的带外决策点边界标记落地（token 已撤回 / 已决策，或标记到达时判定铸出之后 agent 已复工）："
        "UPS 的 decision:block = 挡掉这次提交（prompt 抑制，空闲时不白开一个回合、回合中途并入时直接丢弃），"
        "不是 Stop 的「驱动 agent 再跑一回合」。照 _apply_withdrawn_dispatch_hard_neutralization 三管齐下："
        "additionalContext 只说「已作废、无需处理、手上有活就继续」，**不含决策正文**，挡不住时 agent 也不会"
        "对着「未随附即为重复提交」那一行去猜或反问开发者。2026-10-02 取证：Claude Code 对排队消息的 block 生效。"
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
    (
        "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
        "_observe_delivered_decision_point_banner",
    ): (
        "带外决策点正文的 UPS 出口（铁律允许驱动 agent 的「是否发起 RVF 的决策点」那一类）。"
        "cline-kanban 粘贴通道只送一行无指令的边界标记，Claude Code 把粘贴包成 <pasted_content>、"
        "其中的指令只有用户亲口要求时才执行 ⇒ 正文必须改由 hook 以 additionalContext 注入，"
        "来源标注如实。它搭的是标记那条 user message 的车，不额外开回合；只在 token 首次 "
        "minted→injected 时注入，终态 / 重复到达 / 正文缺失 / 推进失败一律只出 systemMessage。"
    ),
    (
        "plugins/review-validate-fix/hooks/_kimi_hook_entry.py",
        "translate_core_payload_to_kimi_user_prompt_submit_wire",
    ): (
        "Kimi 的 **UPS 出口翻译层**（ENTRY-7）。该文件整体落严格层是因为它同时是 Stop 入口的"
        "fail-open 骨架，但**本函数只处理 UserPromptSubmit**，与 Stop 受众分级无关。\n"
        "它存在的理由与 render_stop_hook_payload 同构：UPS 核心是 host-blind 的、发 Claude 形状的 "
        "additionalContext，而 Kimi 二进制里 additionalContext / systemMessage 各 0 次命中，"
        "且 UPS 是唯一会把 stdout 回落成正文注入的事件 ⇒ 不翻译就等于把整份 JSON（含 "
        "developer-only 的 systemMessage）逐字喂给模型。2026-08-25 已真实发生过一次。\n"
        "差异刻意不上提到 core：UPS 的 additionalContext 是另外五家通吃的，Kimi 是唯一例外。"
    ),
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
                # 命令串不再是裸 `python3 "<marker><rel>"`：spawn 守卫把路径搬进了 shell
                # 变量赋值（`rvf_hook_target_script="<marker><rel>"; if [ -f … ]`），marker
                # 之后还跟着整条守卫逻辑。所以切到**第一个收尾双引号**为止，两种形态通吃。
                relative = command.split(marker, 1)[1].split('"', 1)[0]
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
    files: set[Path] = {
        AUDIENCE_CONTRACT_MODULE,
        HOOKS_DIR / "_claude_hook_entry.py",
        # Kimi 的两个入口刻意**不**在 hooks.json 里（它们走 .kimi-plugin/plugin.json；混进
        # hooks.json 会被 Codex 的 plugin loader 一并加载 ⇒ 三重触发）。所以从 hooks.json
        # 出发的自动发现看不见它们，必须显式纳入——它们同样是 core 收口之外的 emit 点。
        HOOKS_DIR / "_kimi_hook_entry.py",
        HOOKS_DIR / "kimi_stop.py",
        HOOKS_DIR / "kimi_user_prompt_submit.py",
    }
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
    assert HOOKS_DIR / "kimi_stop.py" in stop_tier, (
        "Kimi 的 Stop 入口不在 hooks.json 里，自动发现看不见它——必须显式纳入严格层，"
        "否则 AUD-1 / AUD-6 对它是盲的。"
    )
    assert HOOKS_DIR / "_kimi_hook_entry.py" in stop_tier, stop_tier

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


# ── AUD-5（契约级）：Kimi 通道的线上形状 ─────────────────────────────────────
# 端到端那半（真实 Kimi host adapter 被选中后出口确实落 deny）依赖 adapters/kimi/ 落地，
# 由 S-B4 的 HOST-8 覆盖；本节钉的是翻译层契约本身，不依赖任何 adapter。
def _render_via_contract(channel: str) -> dict:
    """在子进程里调翻译层，避免把 core 包 import 进 pytest 进程（与既有行为测试同口径）。"""
    script = f"""
import json, sys
sys.path.insert(0, {str(ROOT)!r})
from core.stop_hook_output_audience import (
    AgentDirectiveStopHookOutput,
    STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    render_stop_hook_payload,
)
payload = render_stop_hook_payload(
    audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    agent_directive=AgentDirectiveStopHookOutput(
        agent_directive_body="RVF_DECISION_TOKEN=tok-aud5 继续收尾当前 RVF。",
        developer_visible_notice="RVF: 刻意注入，非失败。",
    ),
    developer_only_payload=None,
    agent_directive_channel={channel!r},
)
print(json.dumps(payload))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def test_kimi_channel_lands_on_permission_decision_deny() -> None:
    """AUD-5：Kimi 通道必须落 permissionDecision:"deny"，且不带 decision / additionalContext。"""
    payload = _render_via_contract("hook_specific_output_permission_decision_deny")

    assert "decision" not in payload, (
        f"Kimi 通道发了顶层 decision：{payload}。Kimi 的 hook 输出 schema 是 looseObject，"
        "它会被静默吞掉并 allow —— RVF 完全失声且零报错。"
    )
    inner = payload["hookSpecificOutput"]
    assert inner["permissionDecision"] == "deny", (
        f'Kimi 的判据逐字是 permissionDecision !== "deny" 才 allow（全等比较）：{inner}'
    )
    assert "additionalContext" not in inner, inner
    reason = inner["permissionDecisionReason"]
    assert reason.strip(), "permissionDecisionReason 不得为空——空 reason 的 block 行为未实测。"
    assert "RVF_DECISION_TOKEN=" in reason, reason
    assert payload["systemMessage"], "AUD-4 同样适用于 Kimi 通道。"


def test_each_agent_directive_channel_has_a_distinguishable_wire_shape() -> None:
    """AUD-5 的对偶：三条通道两两形状可辨——否则某个 host 的误判会变成无声降级。"""
    shapes = {
        channel: _render_via_contract(channel)
        for channel in (
            "non_error_additional_context",
            "blocking_decision",
            "hook_specific_output_permission_decision_deny",
        )
    }
    assert "additionalContext" in shapes["non_error_additional_context"]["hookSpecificOutput"]
    assert shapes["blocking_decision"]["decision"] == "block"
    assert "hookSpecificOutput" not in shapes["blocking_decision"], (
        "Codex 的 stop.command.output 是 additionalProperties:false，多一个未知键整条输出 Failed。"
    )
    assert (
        shapes["hook_specific_output_permission_decision_deny"]["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "deny"
    )
    fingerprints = {
        channel: tuple(sorted(shape.get("hookSpecificOutput", {}))) or ("decision",)
        for channel, shape in shapes.items()
    }
    assert len(set(fingerprints.values())) == len(shapes), (
        f"两条通道渲染出同一种线上形状，host 之间将不可分辨：{fingerprints}"
    )


# ── AUD-6：退出码这个第二自由度必须保持「未被使用」──────────────────────────
STOP_HOOK_CORE_MAIN_FUNCTION_NAME = "main"


def _literal_nonzero_exit_calls(path: Path) -> list[tuple[int, str]]:
    """找 ``SystemExit(<非0字面量>)`` / ``sys.exit(<非0字面量>)`` / ``exit(<非0字面量>)``。

    只认**字面量**：``SystemExit(main())`` 这种把退出码委托给函数的形态由 AUD-6b 从另一端钉住
    （钉 ``main`` 的 return），两条合起来才封得住整条路径。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name not in ("SystemExit", "exit", "_exit"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, int) and first.value != 0:
            hits.append((node.lineno, ast.unparse(node)))
    return hits


@pytest.mark.parametrize("path", _stop_tier_files(), ids=lambda p: p.name)
def test_stop_tier_never_uses_a_literal_nonzero_exit_code(path: Path) -> None:
    """AUD-6a：Stop 严格层禁止字面量非 0 退出码。

    Kimi 把 ``exit 2`` 定义为 deliberate block 且 **reason 取 stderr**，而 RVF 全程把诊断写
    stderr —— 一旦有人在 Stop 出口上写 ``sys.exit(2)``，所有杂音都会变成注入给 agent 的正文，
    而本仓的 AUD-1 AST gate 只扫 payload 的 dict 构造，**完全看不见**这条第二出口。
    """
    offending = _literal_nonzero_exit_calls(path)
    assert not offending, (
        f"{path.relative_to(ROOT).as_posix()} 使用了字面量非 0 退出码：{offending}。\n"
        "Stop hook 出口在所有 host 上恒 exit 0；Kimi 的 exit-2 + stderr 通道已知存在且刻意不用"
        "（理由见 core/stop_hook_output_audience 的模块 docstring「退出码不是本契约的一部分」）。"
    )


def test_stop_hook_core_main_returns_only_literal_zero() -> None:
    """AUD-6b：Stop 核心 ``main()`` 的每个 return 都必须是字面量 0。

    只有 AUD-6a 是不够的：``hooks/stop.py`` 与本核心都以 ``raise SystemExit(main())`` 收尾，
    所以 ``main`` 里任何一个 ``return 2`` 都会**绕过** AUD-6a 直接变成进程退出码 2。两条断言
    从 payload 端与 return 端合围，才真的封死这个自由度。
    """
    tree = ast.parse(STOP_HOOK_CORE.read_text(encoding="utf-8"))
    mains = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == STOP_HOOK_CORE_MAIN_FUNCTION_NAME
    ]
    assert len(mains) == 1, f"期望恰好一个 main()，实得 {len(mains)} 个"

    returns = [node for node in ast.walk(mains[0]) if isinstance(node, ast.Return)]
    assert returns, "main() 没有任何 return —— 本断言会变成 vacuous truth"
    offending = [
        (node.lineno, ast.unparse(node.value) if node.value is not None else "<bare>")
        for node in returns
        if not (
            isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and node.value.value == 0
        )
    ]
    assert not offending, (
        f"{STOP_HOOK_CORE.relative_to(ROOT).as_posix()}::main 有非「字面量 0」的 return："
        f"{offending}。Stop hook 出口恒 exit 0；驱动 agent 只走 payload，绝不走退出码。"
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
