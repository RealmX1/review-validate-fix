"""发起消息不变量 INV-1 / INV-2 的**共享**断言（跨 gate 复用，避免各写一份而漏掉分支）。

* **INV-1** —— 一次 Stop 至多产生**一条**面向 agent 的发起正文，**无论它走哪条 transport**。
* **INV-2** —— ``$review-validate-fix`` 只能由 agent 在接受之后自己发出，不得是任何注入/投递
  正文的第一行。

## ⚠️ 两代假绿，读之前先看清

1. **第一代**：INV-2 曾只在 payload **顶层**找 ``reason`` / ``additionalContext``。Stop 的
   ``additionalContext`` 实际嵌在 ``hookSpecificOutput`` 里，于是 Claude 通道上那条断言恒不命中
   ——一条永远为真的断言就是假绿。修法 = 从两条**线材**统一取正文。
2. **第二代（本轮）**：横幅改走 cline-kanban 带外投递之后，正文**根本不进 stdout payload**。
   一个只看 payload 的断言对新 transport 同样恒为真——同一个假绿换了个入口又长了出来。修法 =
   口径改成 ``len(stdout 正文) + len(本轮带外投递) <= 1``，带外那一项从 RunLedger 事件
   ``decision_point_delivered_out_of_band`` 读。

因此 ``run_log_root`` 是**必填**：把它做成可选参数等于把第二代假绿留了一道后门。调用方必须显式
交出这一轮的 run 日志根（通常就是测试的 ``CODEX_RVF_STATE_DIR``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

IMPERATIVE_SELF_INJECTION_PREFIX = "$review-validate-fix"

#: Stop hook 在把横幅交给 cline-kanban 终端投递（带外）成功之后写的 RunLedger 事件名。
#: 与 ``stop_review_validate_fix._decision_point_out_of_band_delivery_decision`` 同源。
OUT_OF_BAND_DECISION_POINT_DELIVERY_LEDGER_EVENT = "decision_point_delivered_out_of_band"


def agent_facing_bodies(payload: Any) -> list[str]:
    """取出 stdout payload 里**所有**投给模型的正文（两条线材通吃）。"""
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


def out_of_band_decision_point_deliveries(run_log_root: str | Path) -> list[dict[str, Any]]:
    """本轮 run 日志里记了几条「横幅已带外投出去」。

    扫 ``<run_log_root>/runs/*/events.jsonl``（RunLedger 的落盘布局），坏行/坏文件一律跳过
    ——gate 的判据是「有没有多投」，不该被一行坏 JSON 拖成 error。
    """
    root = Path(run_log_root).expanduser()
    deliveries: list[dict[str, Any]] = []
    runs_dir = root / "runs"
    if not runs_dir.is_dir():
        return deliveries
    for events_path in sorted(runs_dir.glob("*/events.jsonl")):
        try:
            text = events_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(record, dict)
                and record.get("event") == OUT_OF_BAND_DECISION_POINT_DELIVERY_LEDGER_EVENT
            ):
                deliveries.append(record)
    return deliveries


def assert_at_most_one_agent_facing_initiation_message(
    payload: Any, *, run_log_root: str | Path
) -> str | None:
    """INV-1：stdout 正文 + 带外投递合计至多一条；返回 stdout 那一条（没有则 ``None``）。"""
    bodies = agent_facing_bodies(payload)
    deliveries = out_of_band_decision_point_deliveries(run_log_root)
    total = len(bodies) + len(deliveries)
    assert total <= 1, (
        f"一次 Stop 产生了 {len(bodies)} 条 stdout 自注入正文 + {len(deliveries)} 条带外投递"
        f"（合计 {total}），违反 INV-1：payload={payload!r}；"
        f"带外投递 token={[d.get('decision_token') for d in deliveries]}"
    )
    return bodies[0] if bodies else None


def assert_no_imperative_self_injection(payload: Any) -> None:
    """INV-2：任何注入正文的首行都不得是那条祈使句。"""
    for body in agent_facing_bodies(payload):
        first_line = body.strip().splitlines()[0].strip()
        assert not first_line.startswith(IMPERATIVE_SELF_INJECTION_PREFIX), (
            f"注入正文的首行是祈使句自注入，违反 INV-2：{first_line!r}"
        )


def assert_agent_facing_initiation_message_invariants(
    payload: Any, *, run_log_root: str | Path
) -> str | None:
    """一次跑完 INV-1 + INV-2，返回 stdout 上那条正文（没有则 ``None``）。"""
    body = assert_at_most_one_agent_facing_initiation_message(
        payload, run_log_root=run_log_root
    )
    assert_no_imperative_self_injection(payload)
    return body
