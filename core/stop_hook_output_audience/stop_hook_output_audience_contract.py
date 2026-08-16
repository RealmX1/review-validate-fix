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

``hookSpecificOutput`` 只有 Claude Code / Oh My Pi 认；Codex 的 ``stop.command.output``
schema 是 ``additionalProperties: false``，多发一个未知键会让整条 hook 输出 Failed。
因此「哪条线材」是 **host 行为**，经 ``HostAdapter.stop_hook_agent_directive_channel()``
注入；core 只声明受众，绝不在业务逻辑里判 host。

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
"""``decision:block`` + ``reason``：驱动 agent，harness 会打上错误标（Codex / Cursor / Kimi / DeepSeek）。"""

STOP_HOOK_AGENT_DIRECTIVE_CHANNELS = frozenset(
    {
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
    }
)

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
    * ``agent_must_act_now``：按 ``agent_directive_channel`` 渲染成
      ``hookSpecificOutput.additionalContext`` 或 ``decision:block``；两者都可附
      ``systemMessage``（host 不支持时它只是被忽略，不会让输出失效）。

    刻意**不**在 additionalContext 分支补 ``"continue": true``：实测该分支只回
    ``hookSpecificOutput`` 一个键即可续跑，多发一个键是未经实测的形状。
    """
    if audience not in STOP_HOOK_OUTPUT_AUDIENCES:
        raise StopHookOutputAudienceContractViolation(
            f"未知受众 {audience!r}；只有 {sorted(STOP_HOOK_OUTPUT_AUDIENCES)} 两种。"
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
            f"只有 {sorted(STOP_HOOK_AGENT_DIRECTIVE_CHANNELS)} 两种。"
        )

    if agent_directive_channel == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": STOP_HOOK_EVENT_NAME,
                "additionalContext": agent_directive.agent_directive_body,
            }
        }
    else:
        payload = {"decision": "block", "reason": agent_directive.agent_directive_body}

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
