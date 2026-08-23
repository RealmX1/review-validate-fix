"""Stop hook 出口的**受众分级**契约：core 声明受众，host adapter 决定线上形状。

## 这个模块解决什么

Claude Code 的 Stop hook 有**三条**出口，它们的差别不在正文、而在 harness 给正文
打的标签与投递语义：

| 出口 | transcript 形态 | 是否驱动 agent 再跑一回合 | 开发者呈现 |
| --- | --- | --- | --- |
| ``{"continue": true, "systemMessage": …}`` | ``hook_system_message`` | ❌ | ``Stop says:`` |
| ``{"decision": "block", "reason": …}`` | ``hook_blocking_error`` + 一条 ``type:"user"`` / ``isMeta:true`` 的 ``Stop hook feedback:`` 消息 | ✅ | ``Stop hook error:``，正文进 ``hookErrors[]`` |
| ``{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": …}}`` | ``hook_additional_context`` | ✅ | 无错误框架 |

第三条出口的权威描述来自 Claude Code 2.1.233 二进制内嵌 schema 文案：「Hook-specific
output for the Stop event. ``additionalContext`` is non-error feedback delivered to the
model; the conversation continues so the model can act on it.」

**通道决定标签，内容不决定，且无法 opt out。** 历史上 RVF 走 ``decision:block`` 投出的
92 条注入**全部**是刻意的、可执行的 agent 指令，一条真失败也没有——于是开发者终端里
100% 的 ``Stop hook error:`` 都是假阳性。一个恒假阳性的告警通道会训练人忽略它，真故障
随之被一并忽略。本契约把这件事结构化：

* ``STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY`` —— 失败、降级、skip、诊断。**一切错误
  归此类**，永不驱动 agent。
* ``STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW`` —— 只有两类消息够格：(a) 「是否发起
  RVF」的决策点；(b) 「RVF 未完成，请继续收尾」的续跑提示。

## ⚠️ ``additionalContext`` 同名不同义（最容易踩的坑）

同一个字段名在两个事件上的**投递时机相反**：

* **UserPromptSubmit 的** ``additionalContext`` —— 搭用户**这一条** prompt 的车，
  不额外开回合；
* **Stop 的** ``additionalContext`` —— **立刻续跑**，不等任何用户输入。

所以 Stop 上它**不是**「留到下次再看」的旁路，而是 ``decision:block`` 的同义替身，
差别只在少了错误框架。想要「模型可见且不开回合」，Stop 时刻**没有**这种通道。

## 跨宿主

同一条「驱动 agent」的决策，在三家 host 上要走三条**互不兼容**的线材：

| 线材常量 | 线上形状 | 认它的 host |
| --- | --- | --- |
| ``…_NON_ERROR_ADDITIONAL_CONTEXT`` | ``hookSpecificOutput.additionalContext`` | Claude Code / Oh My Pi |
| ``…_BLOCKING_DECISION`` | ``decision:block`` + ``reason`` | Codex / Cursor / DeepSeek |
| ``…_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY`` | ``hookSpecificOutput.permissionDecision:"deny"`` | Kimi Code CLI |

发错线材的代价是**不对称**的，且大多是静默的：Codex 的 ``stop.command.output`` schema 是
``additionalProperties: false``，多发一个未知键会让整条 hook 输出 Failed（响亮）；而 Kimi 的
hook 输出 schema 是 ``looseObject``，收到 ``decision:block`` 或 ``additionalContext`` 会被
**静默吞掉并 allow** —— RVF 在 Kimi 上完全失声、零报错。因此「哪条线材」是 **host 行为**，
经 ``HostAdapter.stop_hook_agent_directive_channel()`` 注入；core 只声明受众，绝不在业务
逻辑里判 host。

## 退出码不是本契约的一部分

Stop hook 出口在**所有** host 上恒 ``exit 0``。Kimi 另有一条 ``exit 2`` + stderr 的 block
通道（``resultFromExitCode``：``exitCode === 2`` ⇒ block，reason 取 stderr），**已知存在且
刻意不用**，理由有四条，每条都独立成立：

1. ``argparse`` 的退出码就是 2 —— 任何参数解析失败都会被 Kimi 当成 deliberate block，
   把一段 usage 文本当作 agent 指令注入。
2. reason 取的是 **stderr**，而 RVF 全程把诊断写 stderr ⇒ 所有杂音都会变成注入正文。
3. ``stop_hook_router.py::route_to_target`` 已把非零 rc 占用为「target 崩了」：rc=2 会让
   真正的注入正文被吞进 artifact 文件。
4. 本仓的 AST gate 只扫 payload 的 **dict 构造**（``ast.Dict`` / ``dict(**kw)`` / 下标赋值），
   ``stderr + exit code`` 是它**完全看不见**的第二出口。

⇒ 该纪律由 gate ``AUD-6`` 结构性封死：Stop 严格层文件禁止 ``SystemExit(2)`` / ``sys.exit(2)``
/ ``exit(2)``。

本模块 stdlib-only、不 import 任何 adapter，是 host-中性的纯契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY = "developer_only"
"""只给开发者看：失败 / 降级 / skip / 诊断。**永不**驱动 agent 再跑一回合。"""

STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW = "agent_must_act_now"
"""必须驱动 agent 立刻行动。仅限两类：(a) 发起 RVF 的决策点；(b) 未完成 RVF 的续跑提示。"""

STOP_HOOK_OUTPUT_AUDIENCES = frozenset(
    {
        STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
        STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    }
)

STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT = (
    "non_error_additional_context"
)
"""``hookSpecificOutput.additionalContext``：驱动 agent，但**不**带错误框架（Claude / Oh My Pi）。"""

STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION = "blocking_decision"
"""``decision:block`` + ``reason``：驱动 agent，harness 会打上错误标（Codex / Cursor / DeepSeek）。"""

STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY = (
    "hook_specific_output_permission_decision_deny"
)
"""``hookSpecificOutput.permissionDecision:"deny"`` + ``permissionDecisionReason``：驱动 agent（Kimi Code CLI）。

名字里带完整字段路径是刻意的：Kimi 的 deny 藏在 ``hookSpecificOutput`` **内层**，与 Claude 的
``hookSpecificOutput.additionalContext`` 是**同一个外层键、完全不同的内层语义**。不写全路径，
review 时就看不出「这条也发 ``hookSpecificOutput``，所以 AUD-3 那句
``assert "hookSpecificOutput" not in payload`` 只对 Codex 成立」。
"""

STOP_HOOK_AGENT_DIRECTIVE_CHANNELS = frozenset(
    {
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY,
    }
)

STOP_HOOK_PERMISSION_DECISION_DENY = "deny"
"""Kimi 的 block 判据逐字是 ``permissionDecision !== "deny"`` 才 allow —— **全等比较**。

单列成常量而不在渲染处内联，是为了让「这个字面量一旦拼错就静默降级成 allow（零报错）」这件事
有一个可被 gate 钉住的名字。**绝不允许**由任何变量拼接得到它。
"""

STOP_HOOK_EVENT_NAME = "Stop"
"""``hookSpecificOutput.hookEventName`` 的取值；Claude 用它校验这份 hook-specific 输出属于哪个事件。"""


class StopHookOutputAudienceContractViolation(ValueError):
    """构造出口时违反受众契约（如 developer_only 却带着驱动 agent 的键）。"""


@dataclass(frozen=True)
class AgentDirectiveStopHookOutput:
    """一条**必须驱动 agent 立刻行动**的 Stop hook 出口正文（host-中性）。

    ``agent_directive_body`` 是喂给模型的正文；``developer_visible_notice`` 是同时给
    开发者终端的旁注（``systemMessage``），用来把「RVF 刻意注入」与「hook 真崩了」
    在终端里区分开——历史上 90/92 条阻塞注入不带这一项，于是二者不可分辨。
    """

    agent_directive_body: str
    developer_visible_notice: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_directive_body, str) or not self.agent_directive_body.strip():
            raise StopHookOutputAudienceContractViolation(
                "agent_must_act_now 的正文不得为空——空正文在两条通道上的行为都未定义。"
            )


def render_stop_hook_payload(
    *,
    audience: str,
    agent_directive: AgentDirectiveStopHookOutput | None,
    developer_only_payload: Mapping[str, Any] | None,
    agent_directive_channel: str,
) -> dict[str, Any] | None:
    """把「受众 + 正文」翻译成本 host 的线上 payload。**全仓唯一**允许写出 ``decision`` 键的地方。

    * ``developer_only``：``developer_only_payload`` 原样透传（``None`` = 静默停，合法）。
      该 payload **不得**含任何驱动 agent 的键——违反即抛，因为那正是本契约要杜绝的事。
    * ``agent_must_act_now``：按 ``agent_directive_channel`` 渲染成三条线材之一
      （``hookSpecificOutput.additionalContext`` / ``decision:block`` /
      ``hookSpecificOutput.permissionDecision:"deny"``）；三者都可附 ``systemMessage``
      （host 不支持时它只是被忽略，不会让输出失效）。

    三个刻意的形状决定：

    1. 刻意**不**在 additionalContext 分支补 ``"continue": true``：实测该分支只回
       ``hookSpecificOutput`` 一个键即可续跑，多发一个键是未经实测的形状。
    2. **Kimi 分支不发 ``hookEventName``。** 它是 Claude 用来校验「这份 hook-specific 输出
       属于哪个事件」的键；Kimi 的内层 schema 只命名 ``message`` / ``permissionDecision`` /
       ``permissionDecisionReason``。虽然 ``looseObject`` 会静默吞掉它，但同上一条纪律：
       未经实测的多余键不发。
    3. ``systemMessage`` 三条通道一律照附。Kimi 顶层 ``looseObject`` 会静默丢弃它 = 无害；
       而 ``_apply_guardian_liveness_alarm`` 在有告警时**本就会**往 payload 写
       ``systemMessage``，Kimi 分支若拿掉它，只会造成「翻译层不写、guardian 又写回去」的
       自相矛盾。ledger 的 ``emitted_payload`` 也是跨 host 复盘的唯一真相源。

    通道分派是**穷举 + 末尾 raise**，没有 ``else`` 兜底：漏掉一条新通道必须当场炸，
    而不是悄悄退化成某个 host 的形状（那正是 Kimi 上「静默失声」的成因）。
    """
    if audience not in STOP_HOOK_OUTPUT_AUDIENCES:
        raise StopHookOutputAudienceContractViolation(
            f"未知受众 {audience!r}；合法取值只有 {sorted(STOP_HOOK_OUTPUT_AUDIENCES)}。"
        )

    if audience == STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY:
        if agent_directive is not None:
            raise StopHookOutputAudienceContractViolation(
                "developer_only 出口不得携带 agent_directive——受众与正文自相矛盾。"
            )
        if developer_only_payload is None:
            return None
        payload = dict(developer_only_payload)
        offending = sorted(
            key for key in ("decision", "reason", "hookSpecificOutput") if key in payload
        )
        if offending:
            raise StopHookOutputAudienceContractViolation(
                f"developer_only 出口携带了驱动 agent 的键 {offending}；"
                "失败/降级/skip/诊断一律不得驱动 agent 再跑一回合。"
            )
        return payload

    if agent_directive is None:
        raise StopHookOutputAudienceContractViolation(
            "agent_must_act_now 出口必须携带 agent_directive 正文。"
        )
    if developer_only_payload is not None:
        raise StopHookOutputAudienceContractViolation(
            "agent_must_act_now 出口不得同时携带 developer_only_payload——"
            "两条 payload 合流的优先级在各 host 上均未实测。"
        )
    if agent_directive_channel not in STOP_HOOK_AGENT_DIRECTIVE_CHANNELS:
        raise StopHookOutputAudienceContractViolation(
            f"未知 agent-directive 通道 {agent_directive_channel!r}；"
            f"合法取值只有 {sorted(STOP_HOOK_AGENT_DIRECTIVE_CHANNELS)}。"
        )

    if agent_directive_channel == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": STOP_HOOK_EVENT_NAME,
                "additionalContext": agent_directive.agent_directive_body,
            }
        }
    elif agent_directive_channel == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION:
        payload = {"decision": "block", "reason": agent_directive.agent_directive_body}
    elif (
        agent_directive_channel
        == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY
    ):
        payload = {
            "hookSpecificOutput": {
                "permissionDecision": STOP_HOOK_PERMISSION_DECISION_DENY,
                "permissionDecisionReason": agent_directive.agent_directive_body,
            }
        }
    else:  # pragma: no cover —— 成员校验已在上面拦截，这里是穷举漏项的结构性兜底
        raise StopHookOutputAudienceContractViolation(
            f"通道 {agent_directive_channel!r} 已登记进 STOP_HOOK_AGENT_DIRECTIVE_CHANNELS，"
            "但 render_stop_hook_payload 没有对应的渲染分支——"
            "新增通道必须同时加渲染分支，否则该 host 会拿到空 payload 而静默失声。"
        )

    if agent_directive.developer_visible_notice:
        payload["systemMessage"] = agent_directive.developer_visible_notice
    return payload


def _self_check() -> None:
    """runnable check：两条通道的渲染形状 + 四条契约违反都必须真的抛。"""
    directive = AgentDirectiveStopHookOutput(
        agent_directive_body="继续收尾当前 RVF。",
        developer_visible_notice="RVF: 刻意注入，非失败。",
    )

    claude_shape = render_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=directive,
        developer_only_payload=None,
        agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
    )
    assert claude_shape == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "继续收尾当前 RVF。",
        },
        "systemMessage": "RVF: 刻意注入，非失败。",
    }, claude_shape
    assert "decision" not in claude_shape, "non-error 通道不得出现 decision 键"

    codex_shape = render_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=directive,
        developer_only_payload=None,
        agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
    )
    assert codex_shape == {
        "decision": "block",
        "reason": "继续收尾当前 RVF。",
        "systemMessage": "RVF: 刻意注入，非失败。",
    }, codex_shape
    assert "hookSpecificOutput" not in codex_shape, (
        "blocking 通道不得发 hookSpecificOutput——Codex schema 是 additionalProperties:false，"
        "多一个未知键会让整条 hook 输出 Failed。"
    )

    kimi_shape = render_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=directive,
        developer_only_payload=None,
        agent_directive_channel=(
            STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY
        ),
    )
    assert kimi_shape == {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "继续收尾当前 RVF。",
        },
        "systemMessage": "RVF: 刻意注入，非失败。",
    }, kimi_shape
    assert "decision" not in kimi_shape, (
        "Kimi 通道不得发顶层 decision——Kimi 的 hook 输出 schema 是 looseObject，"
        "它会被静默吞掉并 allow（零报错），RVF 因此完全失声。"
    )
    assert kimi_shape["hookSpecificOutput"]["permissionDecision"] == "deny", (
        "Kimi 的判据逐字是 permissionDecision !== \"deny\" 才 allow：全等比较，拼错即静默降级。"
    )
    assert "hookEventName" not in kimi_shape["hookSpecificOutput"], (
        "Kimi 的内层 schema 只命名 message / permissionDecision / permissionDecisionReason；"
        "hookEventName 是 Claude 专属，多发未经实测的键不符合本模块既有纪律。"
    )
    assert "additionalContext" not in kimi_shape["hookSpecificOutput"], kimi_shape

    # 穷举完备性：每条已登记通道都必须真的有渲染分支。少一条就意味着某个 host 拿到空 payload
    # 而静默失声——这正是本模块要杜绝的失败形态，所以让它在 self-check 当场炸。
    rendered_by_channel = {
        channel: render_stop_hook_payload(
            audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
            agent_directive=directive,
            developer_only_payload=None,
            agent_directive_channel=channel,
        )
        for channel in STOP_HOOK_AGENT_DIRECTIVE_CHANNELS
    }
    for channel, shape in rendered_by_channel.items():
        assert shape, f"通道 {channel!r} 渲染出空 payload"
        assert shape.get("decision") or shape.get("hookSpecificOutput"), (
            f"通道 {channel!r} 的 payload 里没有任何驱动 agent 的键：{shape}"
        )
    distinct = {
        channel: tuple(sorted(shape["hookSpecificOutput"]))
        if "hookSpecificOutput" in shape
        else ("decision",)
        for channel, shape in rendered_by_channel.items()
    }
    assert len(set(distinct.values())) == len(STOP_HOOK_AGENT_DIRECTIVE_CHANNELS), (
        f"两条通道渲染出了同一种线上形状，host 之间将不可分辨：{distinct}"
    )

    passthrough = render_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
        agent_directive=None,
        developer_only_payload={"continue": True, "systemMessage": "skipped"},
        agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
    )
    assert passthrough == {"continue": True, "systemMessage": "skipped"}, passthrough
    assert (
        render_stop_hook_payload(
            audience=STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
            agent_directive=None,
            developer_only_payload=None,
            agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
        )
        is None
    ), "静默停（payload=None）必须合法"

    def _expect_violation(label: str, thunk: Any) -> None:
        try:
            thunk()
        except StopHookOutputAudienceContractViolation:
            return
        raise AssertionError(f"{label} 应当抛 StopHookOutputAudienceContractViolation")

    _expect_violation(
        "developer_only 带 decision 键",
        lambda: render_stop_hook_payload(
            audience=STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
            agent_directive=None,
            developer_only_payload={"decision": "block", "reason": "boom"},
            agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
        ),
    )
    _expect_violation(
        "agent_must_act_now 缺正文",
        lambda: render_stop_hook_payload(
            audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
            agent_directive=None,
            developer_only_payload=None,
            agent_directive_channel=STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
        ),
    )
    _expect_violation(
        "未知通道",
        lambda: render_stop_hook_payload(
            audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
            agent_directive=directive,
            developer_only_payload=None,
            agent_directive_channel="carrier-pigeon",
        ),
    )
    _expect_violation(
        "空正文",
        lambda: AgentDirectiveStopHookOutput(agent_directive_body="   "),
    )
    print("stop_hook_output_audience_contract self-check OK")


if __name__ == "__main__":
    _self_check()
