"""测试侧读取 ``StopDecision`` 的 agent-directive 正文的**唯一**入口。

Stop hook 出口做了受众分级之后（``core.stop_hook_output_audience``），「必须驱动
agent 立刻行动」这一类决策不再在构造期就写死 ``{"decision": "block", …}``——线上形状
由 host adapter 在 emit 时翻译。因此进程内单测不能再读 ``decision.payload["reason"]``：
那个字段对这类决策恒为 ``None``。

本 helper 把断言收敛成「受众对不对 + 正文说了什么」，比旧写法更准确：旧的
``payload.get("decision") == "block"`` 把**受众**与**线材**混为一谈，换 host 就假红。
"""

from __future__ import annotations

from typing import Any


def agent_directive_body(decision: Any) -> str:
    """断言 ``decision`` 是 agent-directive 受众，并返回喂给模型的正文。

    同时钉死 ``payload is None``——agent-directive 与 developer-only payload 不得共存
    （两条 payload 合流的优先级在各 host 上均未实测）。
    """
    assert decision is not None, "期望一个 agent-directive 决策，拿到 None"
    assert decision.audience == "agent_must_act_now", (
        f"期望受众 agent_must_act_now，实际 {decision.audience!r}（reason_code="
        f"{getattr(decision, 'reason_code', None)!r}）"
    )
    assert decision.agent_directive is not None, "agent_must_act_now 决策必须带 agent_directive"
    assert decision.payload is None, (
        f"agent-directive 决策不得同时带 developer-only payload：{decision.payload!r}"
    )
    return decision.agent_directive.agent_directive_body


def emitted_agent_directive_body(payload: Any) -> str:
    """从**已 emit 的** Stop hook payload 里取出喂给模型的正文，两条线材通吃。

    子进程端到端用例不该关心当前 fixture 恰好被判成哪个 host——只有专测通道分流的
    用例才该断言线材。断言的是「这份出口确实驱动 agent，且正文是这些字」。
    """
    assert isinstance(payload, dict), payload
    hook_specific = payload.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        assert hook_specific.get("hookEventName") == "Stop", payload
        assert "decision" not in payload, (
            "非错误通道不得同时带 decision——两条 payload 合流的优先级各 host 均未实测"
        )
        body = hook_specific.get("additionalContext")
    else:
        assert payload.get("decision") == "block", payload
        body = payload.get("reason")
    assert isinstance(body, str) and body.strip(), payload
    return body


def developer_visible_notice(decision: Any) -> str | None:
    """返回该 agent-directive 决策附带的 developer-only 旁注（``systemMessage`` 正文）。"""
    assert decision is not None and decision.agent_directive is not None
    return decision.agent_directive.developer_visible_notice
