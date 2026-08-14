#!/usr/bin/env python3
"""cline-kanban「终端投递回执」契约的 RVF 侧唯一判读点（纯函数，无子进程、无 I/O）。

契约真相源：``~/.rvf/cross-repo-coordination-with-cline-kanban/terminal-delivery-interface-contract.md``
（写者 = cline-kanban task ``cdea3``，RVF 只读；2026-08-13 起 ``status: implemented-and-tested``）。
本模块把那份契约的取值集合、判据与失败方向**编码成代码**，使 RVF 各处不必各自解析回执 JSON
——历史上正是「各处自己 `payload.get("status")`」把一个**与实际投递结果无关的快照推断**
当成了投递成功，酿成 2026-08-08 那次「49 分钟后才落地、期间 RVF 以为已送达」的事故。

## 本模块钉死的三条判据

1. **真相字段是 ``terminal_delivery_status``，不是 ``status``。** 后者在新契约里只是遗留镜像，
   且新增了 ``failed`` 取值——任何仍读旧字段的分支都必须能处理它。
2. **``accepted_pending_submit_confirmation`` 不是成功。** 它是**非终态**，契约保证会在
   ≤196.5s 内转成三个终态之一。把它当成功正是事故成因，故本模块永不让它落进
   ``is_delivered``。
3. **判「是不是被取消的」读 ``terminal_delivery_failure_reason``，不读 ``cancel_result``。**
   ``cancel_result`` 只回答「消息是否已确认进入 agent」这一件事；一条**在取消之前就已因别的
   原因失败**的记录同样会回 ``cancelled_before_delivery``。两个问题用两个具名函数分开问。

## 失败方向

回执缺字段 / 只有遗留镜像 / 取值不认识时，一律归入 ``awaiting_terminal_status``（= 「还不知道
结果」），**绝不**归入 delivered。代价是 RVF 会多写一条 pending 对账记录、下一轮再查一次；
反方向（把未知当成功）会让一条从未送达的 review 被静默丢弃，那是不可恢复的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


# --- 契约 § 投递状态取值 -------------------------------------------------------

TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED = "delivered_and_submit_confirmed"
TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN = (
    "delivered_queued_behind_active_agent_turn"
)
TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION = (
    "accepted_pending_submit_confirmation"
)
TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED = "delivery_failed"

#: 契约承认的四种取值。四者语义**不可合并**。
KNOWN_TERMINAL_DELIVERY_STATUSES = frozenset(
    {
        TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED,
        TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN,
        TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION,
        TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
    }
)

#: 「已确认进入 agent」的两种终态。``accepted_pending_submit_confirmation`` **刻意不在此列**。
DELIVERED_TERMINAL_DELIVERY_STATUSES = frozenset(
    {
        TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED,
        TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN,
    }
)

#: 终态集合（写一次即定）。非终态只有 ``accepted_pending_submit_confirmation``。
TERMINAL_TERMINAL_DELIVERY_STATUSES = DELIVERED_TERMINAL_DELIVERY_STATUSES | {
    TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED
}


# --- 契约 § terminal_delivery_failure_reason ----------------------------------

TERMINAL_DELIVERY_FAILURE_REASON_NO_ACTIVE_TERMINAL_SESSION = "no_active_terminal_session"
TERMINAL_DELIVERY_FAILURE_REASON_TERMINAL_PROMPT_READINESS_TIMEOUT = (
    "terminal_prompt_readiness_timeout"
)
TERMINAL_DELIVERY_FAILURE_REASON_SUBMIT_CONFIRMATION_BUDGET_EXHAUSTED = (
    "submit_confirmation_budget_exhausted"
)
TERMINAL_DELIVERY_FAILURE_REASON_HUMAN_TERMINAL_CONTENTION_TIMEOUT = (
    "human_terminal_contention_timeout"
)
TERMINAL_DELIVERY_FAILURE_REASON_AGENT_AWAITING_USER_DECISION_TIMEOUT = (
    "agent_awaiting_user_decision_timeout"
)
TERMINAL_DELIVERY_FAILURE_REASON_SUPERSEDED_BY_LATER_DELIVERY = "superseded_by_later_delivery"
TERMINAL_DELIVERY_FAILURE_REASON_SESSION_ENDED_BEFORE_DELIVERY = "session_ended_before_delivery"
TERMINAL_DELIVERY_FAILURE_REASON_CANCELLED_BEFORE_DELIVERY = "cancelled_before_delivery"
TERMINAL_DELIVERY_FAILURE_REASON_RUNTIME_RESTARTED_BEFORE_CONFIRMATION = (
    "runtime_restarted_before_confirmation"
)

KNOWN_TERMINAL_DELIVERY_FAILURE_REASONS = frozenset(
    {
        TERMINAL_DELIVERY_FAILURE_REASON_NO_ACTIVE_TERMINAL_SESSION,
        TERMINAL_DELIVERY_FAILURE_REASON_TERMINAL_PROMPT_READINESS_TIMEOUT,
        TERMINAL_DELIVERY_FAILURE_REASON_SUBMIT_CONFIRMATION_BUDGET_EXHAUSTED,
        TERMINAL_DELIVERY_FAILURE_REASON_HUMAN_TERMINAL_CONTENTION_TIMEOUT,
        TERMINAL_DELIVERY_FAILURE_REASON_AGENT_AWAITING_USER_DECISION_TIMEOUT,
        TERMINAL_DELIVERY_FAILURE_REASON_SUPERSEDED_BY_LATER_DELIVERY,
        TERMINAL_DELIVERY_FAILURE_REASON_SESSION_ENDED_BEFORE_DELIVERY,
        TERMINAL_DELIVERY_FAILURE_REASON_CANCELLED_BEFORE_DELIVERY,
        TERMINAL_DELIVERY_FAILURE_REASON_RUNTIME_RESTARTED_BEFORE_CONFIRMATION,
    }
)


# --- RVF 侧对每种失败的处置（契约「RVF 该怎么办」那一列的代码化） ----------------

#: 换一枚**新** idempotency key 后可以重投（同 key 不会因上次失败就再投一次，那是幂等的定义）。
REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY = (
    "redispatch_allowed_with_fresh_idempotency_key"
)
#: 可重投，但文本可能仍残留在目标输入框里 —— 重投前宜由人确认，不要自动连投。
REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION = "redispatch_requires_human_confirmation"
#: 本轮不重投，等外部条件（会话起来 / 用户答完 / 人离开输入框）变化后由下一次 Stop 重新决策。
REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION = "redispatch_wait_for_external_condition"
#: 不必重投：结果由**另一条**更晚的投递记录承载，或本就是 RVF 自己取消的。
REDISPATCH_DISPOSITION_NOT_NEEDED = "redispatch_not_needed"

_REDISPATCH_DISPOSITION_BY_FAILURE_REASON: dict[str, str] = {
    # 「不重投，等会话起来」：目标 task 当前根本没有活着的终端会话。
    TERMINAL_DELIVERY_FAILURE_REASON_NO_ACTIVE_TERMINAL_SESSION: (
        REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION
    ),
    # 「可择机重投（新 key）」：TUI 一直没就绪，重投是干净的——一个字节都没写进去。
    TERMINAL_DELIVERY_FAILURE_REASON_TERMINAL_PROMPT_READINESS_TIMEOUT: (
        REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY
    ),
    # 「谨慎」：写进去了但确认不到提交 —— 文本可能残留在输入框里，自动重投会叠成两份。
    TERMINAL_DELIVERY_FAILURE_REASON_SUBMIT_CONFIRMATION_BUDGET_EXHAUSTED: (
        REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION
    ),
    # 「不重投本轮」：人正在那个输入框里打字 —— 这正是「人在改动时这次评审本就不该跑」。
    TERMINAL_DELIVERY_FAILURE_REASON_HUMAN_TERMINAL_CONTENTION_TIMEOUT: (
        REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION
    ),
    # 「不重投，等用户答完」：agent 卡在 AskUserQuestion / 计划评审 / 权限确认。
    TERMINAL_DELIVERY_FAILURE_REASON_AGENT_AWAITING_USER_DECISION_TIMEOUT: (
        REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION
    ),
    # 「后一条的状态才算数」：同 session 有更晚的投递抢占了单飞槽。
    TERMINAL_DELIVERY_FAILURE_REASON_SUPERSEDED_BY_LATER_DELIVERY: REDISPATCH_DISPOSITION_NOT_NEEDED,
    # 「不重投」：投递落定前会话就结束了。
    TERMINAL_DELIVERY_FAILURE_REASON_SESSION_ENDED_BEFORE_DELIVERY: (
        REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION
    ),
    # 「由 RVF 自己发起的，无需处理」。
    TERMINAL_DELIVERY_FAILURE_REASON_CANCELLED_BEFORE_DELIVERY: REDISPATCH_DISPOSITION_NOT_NEEDED,
    # 「语义等同『不知道有没有送到』，按未送达处理但需警惕残留」。
    TERMINAL_DELIVERY_FAILURE_REASON_RUNTIME_RESTARTED_BEFORE_CONFIRMATION: (
        REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION
    ),
}


def redispatch_disposition_for_terminal_delivery_failure_reason(reason: Any) -> str:
    """把契约的失败 reason 映射成 RVF 的重投处置。

    **未知 reason（对方将来新增的取值）一律落 ``requires_human_confirmation``**：既不自动连投
    （可能把文本叠进一个已有残留的输入框），也不静默放弃（stranded 通知仍会把它浮现给用户）。
    """
    if not isinstance(reason, str) or not reason.strip():
        return REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION
    return _REDISPATCH_DISPOSITION_BY_FAILURE_REASON.get(
        reason.strip(), REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION
    )


# --- 契约 § 时序保证 1：pending 必然在 ≤196.5s 内转终态 ------------------------

#: 契约给出的最坏路径预算：60s TUI 就绪 + 120s 模态让位 + 1.5s 摄入证据 + 15s 确认链收敛。
#: 这是**对方的保证**，不是我方的偏好，故它是 RVF 侧任何「等终态」超时的**下界**。
TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS = 196.5
#: 契约原文建议「直接取 200s」，留一点时钟/调度余量。
DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS = 200.0
#: 契约明说这个数还可能再收敛，所以**不得硬编码**——留一个具名旋钮。
TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV = (
    "RVF_CLINE_KANBAN_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS"
)


def resolve_terminal_delivery_status_convergence_timeout_seconds(
    env: Mapping[str, str] | None = None,
) -> float:
    """解析「等 pending 转终态」的超时（秒）。

    可经 :data:`TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV` 覆盖，但**下界锁死在契约预算**：
    配小了会在正常路径上把一条还在有界收敛中的投递误判成 lost，从而重投出第二份 review。
    坏值一律回落默认。
    """
    source = os.environ if env is None else env
    raw = source.get(TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV)
    configured = DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS
    if isinstance(raw, str) and raw.strip():
        try:
            configured = float(raw)
        except ValueError:
            configured = DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS
    return max(configured, TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS)


# --- 回执判读 -----------------------------------------------------------------

#: 回执里真的带了契约字段。
RECEIPT_FIELD_SOURCE_TERMINAL_DELIVERY_STATUS = "terminal_delivery_status"
#: 只有遗留镜像 ``status``（对方是旧版本，或该路径尚未接上新字段）。
RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR = "legacy_status_mirror"
#: 两个字段都没有 —— 回执形状完全不认识。
RECEIPT_FIELD_SOURCE_ABSENT = "absent"

#: 遗留镜像里表示「已失败」的取值（契约新增；旧实现没有这个取值，故它一定来自新版本）。
_LEGACY_STATUS_MIRROR_FAILED = "failed"


@dataclass(frozen=True)
class ClineKanbanTerminalDeliveryReceipt:
    """一条 ``kanban task message`` / ``message-status`` 回执的判读结果。

    ``is_delivered`` / ``is_failed`` / ``is_awaiting_terminal_status`` 三者互斥且必有其一。
    """

    terminal_delivery_status: str | None
    terminal_delivery_failure_reason: str | None
    terminal_delivery_status_updated_at: str | None
    task_id: str | None
    idempotency_key: str | None
    message_id: str | None
    receipt_field_source: str
    legacy_status_mirror: str | None
    is_delivered: bool
    is_failed: bool
    is_awaiting_terminal_status: bool
    redispatch_disposition: str | None

    @property
    def is_terminal(self) -> bool:
        return self.is_delivered or self.is_failed


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def classify_cline_kanban_terminal_delivery_receipt(
    payload: Mapping[str, Any] | None,
) -> ClineKanbanTerminalDeliveryReceipt:
    """把回执 JSON 判读成 :class:`ClineKanbanTerminalDeliveryReceipt`。

    判读顺序（**不可调换**）：
    1. ``terminal_delivery_status`` 在且是契约认识的取值 → 以它为准；
    2. 否则看遗留镜像 ``status``：只有 ``failed`` 能得出「已失败」这个结论。
       ``started`` / ``queued`` 在旧实现里是 **mutate 之前**读的快照推断、与实际投递结果无关，
       故一律判 awaiting，**绝不**当成送达；
    3. 都没有 → awaiting（``receipt_field_source=absent``）。

    未知的 ``terminal_delivery_status`` 取值（对方将来新增）同样落 awaiting：宁可多查一轮，
    也不猜一个可能是「送达」的语义。
    """
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    status = _clean_str(data.get("terminal_delivery_status"))
    reason = _clean_str(data.get("terminal_delivery_failure_reason"))
    updated_at = _clean_str(data.get("terminal_delivery_status_updated_at"))
    task_id = _clean_str(data.get("task_id")) or _clean_str(data.get("taskId"))
    idempotency_key = _clean_str(data.get("idempotency_key")) or _clean_str(
        data.get("idempotencyKey")
    )
    message_id = _clean_str(data.get("message_id")) or _clean_str(data.get("messageId"))
    legacy_mirror = _clean_str(data.get("status"))

    if status is not None and status in KNOWN_TERMINAL_DELIVERY_STATUSES:
        delivered = status in DELIVERED_TERMINAL_DELIVERY_STATUSES
        failed = status == TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED
        return ClineKanbanTerminalDeliveryReceipt(
            terminal_delivery_status=status,
            terminal_delivery_failure_reason=reason if failed else None,
            terminal_delivery_status_updated_at=updated_at,
            task_id=task_id,
            idempotency_key=idempotency_key,
            message_id=message_id,
            receipt_field_source=RECEIPT_FIELD_SOURCE_TERMINAL_DELIVERY_STATUS,
            legacy_status_mirror=legacy_mirror,
            is_delivered=delivered,
            is_failed=failed,
            is_awaiting_terminal_status=not (delivered or failed),
            redispatch_disposition=(
                redispatch_disposition_for_terminal_delivery_failure_reason(reason)
                if failed
                else None
            ),
        )

    if status is None and legacy_mirror == _LEGACY_STATUS_MIRROR_FAILED:
        # 只有遗留镜像、且它说失败：结论可信（``failed`` 是契约新增取值，旧实现产生不了它），
        # 但**拿不到 reason** —— 于是处置只能是「交给人确认」，不自动连投。
        return ClineKanbanTerminalDeliveryReceipt(
            terminal_delivery_status=TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
            terminal_delivery_failure_reason=None,
            terminal_delivery_status_updated_at=updated_at,
            task_id=task_id,
            idempotency_key=idempotency_key,
            message_id=message_id,
            receipt_field_source=RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR,
            legacy_status_mirror=legacy_mirror,
            is_delivered=False,
            is_failed=True,
            is_awaiting_terminal_status=False,
            redispatch_disposition=REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
        )

    source = (
        RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR
        if (status is None and legacy_mirror is not None)
        else RECEIPT_FIELD_SOURCE_ABSENT
    )
    return ClineKanbanTerminalDeliveryReceipt(
        terminal_delivery_status=status,
        terminal_delivery_failure_reason=None,
        terminal_delivery_status_updated_at=updated_at,
        task_id=task_id,
        idempotency_key=idempotency_key,
        message_id=message_id,
        receipt_field_source=source,
        legacy_status_mirror=legacy_mirror,
        is_delivered=False,
        is_failed=False,
        is_awaiting_terminal_status=True,
        redispatch_disposition=None,
    )


def _parse_iso_timestamp(value: Any) -> float | None:
    text = _clean_str(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def terminal_delivery_status_age_seconds(
    receipt: ClineKanbanTerminalDeliveryReceipt,
    *,
    now_ts: float | None = None,
) -> float | None:
    """回执上那个状态戳到现在多久（秒）。戳缺失 / 不可解析一律 None。"""
    stamped = _parse_iso_timestamp(receipt.terminal_delivery_status_updated_at)
    if stamped is None:
        return None
    current = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    return max(0.0, current - stamped)


def awaiting_receipt_exceeded_contract_convergence_budget(
    receipt: ClineKanbanTerminalDeliveryReceipt,
    *,
    now_ts: float | None = None,
    timeout_seconds: float | None = None,
) -> bool:
    """这条仍 awaiting 的回执是否已经**超出对方承诺的收敛预算**。

    True = 对方违背了「≤196.5s 必转终态」的保证（runtime 被杀、账本没落盘等），RVF 可以停止
    等待、按「不知道有没有送到」处置。终态回执恒 False；拿不到状态戳时同样 False
    ——**没有证据就不下这个结论**，交给 pending marker 自己的 TTL 兜底。
    """
    if not receipt.is_awaiting_terminal_status:
        return False
    age = terminal_delivery_status_age_seconds(receipt, now_ts=now_ts)
    if age is None:
        return False
    budget = (
        resolve_terminal_delivery_status_convergence_timeout_seconds()
        if timeout_seconds is None
        else max(
            float(timeout_seconds), TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS
        )
    )
    return age > budget


# --- 契约 § 取消接口：两个问题，两个具名函数 ----------------------------------

CANCEL_RESULT_ALREADY_DELIVERED = "already_delivered"
CANCEL_RESULT_CANCELLED_BEFORE_DELIVERY = "cancelled_before_delivery"


def cancel_receipt_indicates_message_already_reached_agent(
    payload: Mapping[str, Any] | None,
) -> bool:
    """取消回执是否表示「这条消息已经确认进入 agent」（= 取消晚了）。

    这是 ``cancel_result`` **唯一**回答的问题，读它是正当的。
    """
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    return _clean_str(data.get("cancel_result")) == CANCEL_RESULT_ALREADY_DELIVERED


def cancel_receipt_indicates_cancellation_rather_than_another_failure(
    payload: Mapping[str, Any] | None,
) -> bool:
    """这条记录**确实是被取消**落定的，而不是在取消之前就因别的原因失败了。

    判据是 ``terminal_delivery_failure_reason == cancelled_before_delivery``。
    **不要**用 ``cancel_result`` 回答这个问题：契约明确警告「``cancelled_before_delivery``
    不等于『是这次取消拦下的』」——一条早已 ``no_active_terminal_session`` 的记录同样会回它。
    """
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    return (
        _clean_str(data.get("terminal_delivery_failure_reason"))
        == TERMINAL_DELIVERY_FAILURE_REASON_CANCELLED_BEFORE_DELIVERY
    )


# --- 人读摘要 -----------------------------------------------------------------

_STATUS_SUMMARY_ZH = {
    TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED: "已写入终端且提交已确认",
    TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN: (
        "已提交但排在 agent 当前回合之后"
    ),
    TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION: (
        "已受理、尚未确认写入/提交（非终态）"
    ),
    TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED: "未送达",
}


def describe_terminal_delivery_receipt_for_operator(
    receipt: ClineKanbanTerminalDeliveryReceipt,
) -> str:
    """给 systemMessage / ledger 用的一行中文摘要（永不抛）。"""
    if receipt.terminal_delivery_status is None:
        return (
            "cline-kanban 回执未带 terminal_delivery_status"
            f"（遗留 status={receipt.legacy_status_mirror or '<无>'}）；按「结果未知」处置。"
        )
    summary = _STATUS_SUMMARY_ZH.get(
        receipt.terminal_delivery_status, receipt.terminal_delivery_status
    )
    if not receipt.is_failed:
        return f"cline-kanban 投递状态：{summary}（{receipt.terminal_delivery_status}）。"
    reason = receipt.terminal_delivery_failure_reason or "<未给出 reason>"
    return (
        f"cline-kanban 投递状态：{summary}（{receipt.terminal_delivery_status}），"
        f"reason={reason}，RVF 处置={receipt.redispatch_disposition}。"
    )


__all__ = [
    "CANCEL_RESULT_ALREADY_DELIVERED",
    "CANCEL_RESULT_CANCELLED_BEFORE_DELIVERY",
    "DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS",
    "DELIVERED_TERMINAL_DELIVERY_STATUSES",
    "KNOWN_TERMINAL_DELIVERY_FAILURE_REASONS",
    "KNOWN_TERMINAL_DELIVERY_STATUSES",
    "RECEIPT_FIELD_SOURCE_ABSENT",
    "RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR",
    "RECEIPT_FIELD_SOURCE_TERMINAL_DELIVERY_STATUS",
    "REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY",
    "REDISPATCH_DISPOSITION_NOT_NEEDED",
    "REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION",
    "REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION",
    "TERMINAL_DELIVERY_FAILURE_REASON_AGENT_AWAITING_USER_DECISION_TIMEOUT",
    "TERMINAL_DELIVERY_FAILURE_REASON_CANCELLED_BEFORE_DELIVERY",
    "TERMINAL_DELIVERY_FAILURE_REASON_HUMAN_TERMINAL_CONTENTION_TIMEOUT",
    "TERMINAL_DELIVERY_FAILURE_REASON_NO_ACTIVE_TERMINAL_SESSION",
    "TERMINAL_DELIVERY_FAILURE_REASON_RUNTIME_RESTARTED_BEFORE_CONFIRMATION",
    "TERMINAL_DELIVERY_FAILURE_REASON_SESSION_ENDED_BEFORE_DELIVERY",
    "TERMINAL_DELIVERY_FAILURE_REASON_SUBMIT_CONFIRMATION_BUDGET_EXHAUSTED",
    "TERMINAL_DELIVERY_FAILURE_REASON_SUPERSEDED_BY_LATER_DELIVERY",
    "TERMINAL_DELIVERY_FAILURE_REASON_TERMINAL_PROMPT_READINESS_TIMEOUT",
    "TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION",
    "TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS",
    "TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV",
    "TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED",
    "TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN",
    "TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED",
    "TERMINAL_TERMINAL_DELIVERY_STATUSES",
    "ClineKanbanTerminalDeliveryReceipt",
    "awaiting_receipt_exceeded_contract_convergence_budget",
    "cancel_receipt_indicates_cancellation_rather_than_another_failure",
    "cancel_receipt_indicates_message_already_reached_agent",
    "classify_cline_kanban_terminal_delivery_receipt",
    "describe_terminal_delivery_receipt_for_operator",
    "redispatch_disposition_for_terminal_delivery_failure_reason",
    "resolve_terminal_delivery_status_convergence_timeout_seconds",
    "terminal_delivery_status_age_seconds",
]
