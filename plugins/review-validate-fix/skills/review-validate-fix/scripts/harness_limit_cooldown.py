#!/usr/bin/env python3
"""Reviewer harness「额度/配额耗尽」跨轮冷却（cooldown）标记。

当某个 reviewer harness（及其背后的 agentic / coding 订阅）在一轮 RVF review 中真跑撞上
usage limit / quota exhausted 时，dispatch_reviewers 会在 ``~/.rvf/harness-limit-cooldown/``
下记一条带 TTL 的冷却标记。后续轮 ``probe_available`` 在真实 probe 阶段会跳过仍处冷却期的
harness（额度耗尽时 ``codex login status`` 之类的 auth probe 仍返回 0，单看 auth 无法发现，
故需独立的额度信号）。

设计取舍（与 ``kanban_followup_lock.py`` 的差异）：
- 这是 **best-effort hint**，不是互斥锁：并发写用「原子 tmp + replace」last-writer-wins 即可，
  **不**抄 in-progress 锁的 ``O_EXCL`` / takeover flock 重型机制。
- TTL 默认 1h；能从错误文本解析到 provider 给出的重置提示（"try again in 4h" /
  "retry after 120" / "resets at <ISO>"）则以它为准；重复命中取更晚的 ``expires_at``（延长不缩短）。
- 读前 lazy ``sweep_expired``：过期标记在被读到时顺手清掉，避免目录无界增长。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rvf_logging import safe_token


SUBDIR_NAME = "harness-limit-cooldown"
MARKER_VERSION = 1
DEFAULT_TTL_SECONDS = 60 * 60  # 1h
TTL_ENV = "RVF_HARNESS_LIMIT_COOLDOWN_TTL_SECONDS"
ROOT_ENV = "RVF_HARNESS_LIMIT_COOLDOWN_ROOT"

# 解析 provider 重置提示的上下界：解析出的秒数会被夹到 [60s, 7d]，
# 防止把异常巨大/为零的数值写成永久或瞬时冷却。
MIN_RESET_HINT_SECONDS = 60.0
MAX_RESET_HINT_SECONDS = 7 * 24 * 60 * 60.0


def _root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / SUBDIR_NAME
    raw = os.environ.get(ROOT_ENV)
    if raw and raw.strip():
        # env 覆盖被视为「直接就是 cooldown 目录」（与测试 tmp root 约定一致）。
        return Path(raw).expanduser()
    return Path.home() / ".rvf" / SUBDIR_NAME


def _marker_path(harness_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"harness-{safe_token(harness_id)}.json"


def default_ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, value)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _parse_iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# reset hint 解析
# ---------------------------------------------------------------------------

_UNIT_SECONDS = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}

# "try again in 4h" / "retry in 30 minutes" / "resets in 90s" / "available again in 2 hours"
_DURATION_RE = re.compile(
    r"(?:try again|retry|reset|resets|available again|again|wait)\s+(?:in|after)\s+"
    r"(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|s|m|h|d)\b",
    re.IGNORECASE,
)
# "retry-after: 120" / "retry after 120 seconds"（HTTP Retry-After 风格，单位秒）
_RETRY_AFTER_RE = re.compile(
    r"retry[-_ ]after[:=]?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
# "resets at 2026-06-22T15:00:00Z"（绝对 ISO 时间戳；time-of-day 无日期者过于歧义，不解析）
_RESETS_AT_ISO_RE = re.compile(
    r"reset[s]?\s+at\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:]+(?:\.\d+)?(?:Z|[+-][0-9:]+)?)",
    re.IGNORECASE,
)


def parse_reset_hint(text: str | None) -> float | None:
    """从 provider 错误文本里解析「多少秒后重置」。

    支持三类：① "try again in 4h" / "retry in 30 minutes" 之类的相对时长；
    ② "retry-after: 120" 的 HTTP Retry-After（秒）；③ "resets at <ISO 时间戳>" 的绝对时间。
    解析不出返回 None（调用方回落默认 TTL）。结果夹到 [MIN, MAX] 防极端值。
    """
    if not text:
        return None

    match = _DURATION_RE.search(text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        mult = _UNIT_SECONDS.get(unit)
        if mult is not None:
            return _clamp_reset_hint(amount * mult)

    match = _RETRY_AFTER_RE.search(text)
    if match:
        return _clamp_reset_hint(float(match.group(1)))

    match = _RESETS_AT_ISO_RE.search(text)
    if match:
        ts = _parse_iso_ts(match.group(1))
        if ts is not None:
            delta = ts - datetime.now(timezone.utc).timestamp()
            if delta > 0:
                return _clamp_reset_hint(delta)

    return None


def _clamp_reset_hint(seconds: float) -> float:
    return max(MIN_RESET_HINT_SECONDS, min(MAX_RESET_HINT_SECONDS, seconds))


# ---------------------------------------------------------------------------
# record / query / sweep
# ---------------------------------------------------------------------------


def record(
    harness_id: str,
    *,
    ttl_seconds: float | None = None,
    reset_hint: float | None = None,
    reason: str | None = None,
    error_message: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """为 ``harness_id`` 记一条冷却标记，返回标记文件路径（harness_id 空则 None）。

    ``expires_at = now + (reset_hint or ttl_seconds or 默认TTL)``。重复命中：取
    现有 ``expires_at`` 与新值中更晚的一个（延长冷却、绝不缩短）。
    """
    if not (isinstance(harness_id, str) and harness_id.strip()):
        return None
    effective = reset_hint if reset_hint is not None else (
        ttl_seconds if ttl_seconds is not None else default_ttl_seconds()
    )
    effective = max(0.0, float(effective))
    now_ts = datetime.now(timezone.utc).timestamp()
    new_expires_ts = now_ts + effective

    path = _marker_path(harness_id, root)
    existing = _read_marker(path)
    if isinstance(existing, dict):
        existing_expires = _parse_iso_ts(existing.get("expires_at"))
        if existing_expires is not None and existing_expires > new_expires_ts:
            new_expires_ts = existing_expires

    expires_at = (
        datetime.fromtimestamp(new_expires_ts, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload = {
        "marker_version": MARKER_VERSION,
        "harness_id": harness_id,
        "recorded_at": _iso_now(),
        "expires_at": expires_at,
        "reason": reason,
        "error_message": (error_message[:2000] if isinstance(error_message, str) else None),
        "reset_hint_seconds": reset_hint,
    }
    _atomic_write(path, payload)
    return path


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _is_active(marker: dict[str, Any] | None, *, now_ts: float | None = None) -> bool:
    if not isinstance(marker, dict):
        return False
    current = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is None:
        return False
    return current <= expires_ts


def sweep_expired(root: Path | None = None) -> list[str]:
    """清理已过期 / 损坏的冷却标记，返回被删路径列表。"""
    base = _root(root)
    removed: list[str] = []
    try:
        entries = list(base.glob("harness-*.json"))
    except OSError:
        return removed
    now_ts = datetime.now(timezone.utc).timestamp()
    for path in entries:
        marker = _read_marker(path)
        if _is_active(marker, now_ts=now_ts):
            continue
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            continue
        removed.append(str(path))
    return removed


def active(harness_id: str, *, root: Path | None = None) -> bool:
    """``harness_id`` 当前是否处于冷却期（读前 lazy sweep）。"""
    if not (isinstance(harness_id, str) and harness_id.strip()):
        return False
    sweep_expired(root)
    marker = _read_marker(_marker_path(harness_id, root))
    return _is_active(marker)


def active_harnesses(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """返回当前仍在冷却期的 ``{harness_id: marker}``（读前 lazy sweep）。"""
    sweep_expired(root)
    base = _root(root)
    result: dict[str, dict[str, Any]] = {}
    try:
        entries = list(base.glob("harness-*.json"))
    except OSError:
        return result
    now_ts = datetime.now(timezone.utc).timestamp()
    for path in entries:
        marker = _read_marker(path)
        if not _is_active(marker, now_ts=now_ts):
            continue
        hid = marker.get("harness_id")
        if isinstance(hid, str) and hid:
            result[hid] = marker
    return result


def clear(harness_id: str, *, root: Path | None = None) -> bool:
    """显式清掉某 harness 的冷却标记（测试 / 手动恢复用）。"""
    if not (isinstance(harness_id, str) and harness_id.strip()):
        return False
    try:
        _marker_path(harness_id, root).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False
