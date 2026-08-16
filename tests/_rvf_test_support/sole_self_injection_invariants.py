"""自注入不变量 INV-1 / INV-2 的**共享**断言（跨 gate 复用，避免各写一份而漏掉分支）。

* **INV-1** —— 一次 Stop 至多产生**一条**投给 agent 的正文。受众分级落地后这条已由
  ``core.stop_hook_output_audience.render_stop_hook_payload`` 结构性保证（一份 payload 只承载
  一条正文、两条通道互斥），本断言是它的行为侧复核。
* **INV-2** —— ``$review-validate-fix`` 只能由 agent 在接受之后自己发出，不得是任何注入正文
  的第一行。

⚠️ 历史坑：INV-2 曾只在 payload **顶层**找 ``reason`` / ``additionalContext``。Stop 的
``additionalContext`` 实际嵌在 ``hookSpecificOutput`` 里，于是 Claude 通道上那条断言恒不命中
——一条永远为真的断言就是假绿。本模块统一从两条通道取正文，杜绝再犯。
"""

from __future__ import annotations

from typing import Any

IMPERATIVE_SELF_INJECTION_PREFIX = "$review-validate-fix"


def agent_facing_bodies(payload: Any) -> list[str]:
    """取出 payload 里**所有**投给模型的正文（两条通道通吃）。"""
    if not isinstance(payload, dict):
        return []
    bodies: list[str] = []
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        bodies.append(reason)
    hook_specific = payload.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        additional = hook_specific.get("additionalContext")
        if isinstance(additional, str) and additional.strip():
            bodies.append(additional)
    return bodies


def assert_at_most_one_self_injection(payload: Any) -> str | None:
    """INV-1：至多一条投给 agent 的正文；返回那一条（没有则 ``None``）。"""
    bodies = agent_facing_bodies(payload)
    assert len(bodies) <= 1, (
        f"一次 Stop 产生了 {len(bodies)} 条自注入正文，违反 INV-1：{payload!r}"
    )
    return bodies[0] if bodies else None


def assert_no_imperative_self_injection(payload: Any) -> None:
    """INV-2：任何注入正文的首行都不得是那条祈使句。"""
    for body in agent_facing_bodies(payload):
        first_line = body.strip().splitlines()[0].strip()
        assert not first_line.startswith(IMPERATIVE_SELF_INJECTION_PREFIX), (
            f"注入正文的首行是祈使句自注入，违反 INV-2：{first_line!r}"
        )


def assert_self_injection_invariants(payload: Any) -> str | None:
    """一次跑完 INV-1 + INV-2，返回唯一那条正文（没有则 ``None``）。"""
    body = assert_at_most_one_self_injection(payload)
    assert_no_imperative_self_injection(payload)
    return body
