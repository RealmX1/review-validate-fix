"""temporary_disable_window — 声明式「临时禁用时间盒」通用组件(R21,S7 抽取)。

把「某能力在某 UTC 时刻前临时禁用」的一次性策略泛化成可声明、可单测、可自动
过期的通用组件。既有唯一用例(claude-code external reviewer 时间盒,原硬编码在
``dispatch_reviewers.py``)改写为本组件的声明式实例;后续任何同类临时策略
(禁用某 harness/某通道/某功能到某截止点)都应声明一个
:class:`TemporaryDisableWindow` 实例而非再写一次 ad-hoc 判断。

设计要点:

- **纯声明**:窗口 = (被禁对象, 理由 slug, UTC 截止时刻, 附注 metadata)。
- **自动过期**::meth:`active_override` 在 ``now >= expires_at_utc`` 后恒返回
  ``None``,策略失效无需改码——过期声明保留在原地即是「已过期时间盒」的考古记录。
- **零 IO**:时钟由调用方注入(默认取当前 UTC),便于单测冻结时间。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


def _coerce_to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class TemporaryDisableWindow:
    """一条声明式临时禁用策略。

    ``disabled_subject``:被禁对象标识(如 harness id ``claude_code``)。
    ``reason``:自解释理由 slug(进 warning/遥测)。
    ``expires_at_utc``:UTC 截止时刻,达到即自动失效。
    ``metadata``:附注(如原始需求日期、时区覆盖策略),原样并入 override 载荷。
    """

    disabled_subject: str
    reason: str
    expires_at_utc: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_at_utc", _coerce_to_utc(self.expires_at_utc))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def is_active(self, now_utc: datetime | None = None) -> bool:
        current = _coerce_to_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
        return current < self.expires_at_utc

    def active_override(self, now_utc: datetime | None = None) -> dict[str, str] | None:
        """窗口生效中返回 override 载荷(供 warning/遥测),已过期返回 ``None``。"""
        if not self.is_active(now_utc):
            return None
        return {
            "reason": self.reason,
            "disabled_subject": self.disabled_subject,
            "expires_at_utc": self.expires_at_utc.isoformat().replace("+00:00", "Z"),
            **dict(self.metadata),
        }
