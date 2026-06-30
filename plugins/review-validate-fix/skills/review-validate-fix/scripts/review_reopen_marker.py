#!/usr/bin/env python3
"""失败再入「重开评审范围」一次性标记（agent 写、Stop 消费）。

约束 / 生命周期（存储与原子写镜像 ``round_baseline_marker``，状态语义借
``kanban_followup_lock`` 的 active/stale/invalid）：

- 由主 agent 在「用户带『实现未达标 / 有问题』信号回到实现终点、准备开修」时，
  经 ``rvf_rescope.py arm`` 写入 marker，带 ``target_run_id``（最近一次刚经过
  RVF 的那次实现 run）；
- 由后续 Stop hook 在 tracker refresh 之后、``allocate_review_scope`` 之前读取：
  若 ``active`` → 调 ``reviewable_unit_diff_tracker.invalidate_reviewed_units_for_run`` 按
  ``target_run_id`` 把该 run 仍存在的 ``reviewed`` units 翻回 ``available``，
  使紧接着的 allocate 得到「该实现 units ∪ 本次 fix delta」全量 → 即时全量
  re-review dispatch；随后 **consume（删除）marker**；
- ``stale`` / ``invalid`` 也会被消费（清除），不触发重开——避免放弃的 marker
  长期残留再误触发。

刻意只承载 run-scoped 意图，不广播：``target_run_id`` 之外的 reviewed units 永不
被本机制波及。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import log_root, safe_token  # noqa: E402


SUBDIR_NAME = "review-reopen-pending"
MARKER_VERSION = 1
DEFAULT_TTL_SECONDS = 6 * 60 * 60
TTL_ENV = "RVF_REVIEW_REOPEN_TTL_SECONDS"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"


def _reopen_root(root: Path | None = None) -> Path:
    return (root.expanduser() if root is not None else log_root()) / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _reopen_root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _reopen_root(root) / f"sess-{safe_token(session_id)}.json"


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[Path]:
    """返回该上下文下可能持有 marker 的所有候选路径。

    task_id 优先；若两者都缺则返回空列表，调用方应据此跳过写 / 读。
    """
    paths: list[Path] = []
    if task_id:
        paths.append(_task_path(task_id, root))
    if session_id:
        paths.append(_session_path(session_id, root))
    return paths


def ttl_seconds() -> float:
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
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


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
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    target_run_id: str,
    repo: str | None,
    reason: str,
    source: str,
    armed_at: str | None = None,
) -> dict[str, Any]:
    timestamp = armed_at or _iso_now()
    ttl = ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": "pending_reopen",
        "armed_at": timestamp,
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "target_run_id": target_run_id,
        "repo": repo,
        "reason": reason,
        "source": source,
        "kanban_task_id": task_id,
        "parent_session_id": session_id,
    }


def write_review_reopen_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    target_run_id: str,
    repo: str | None,
    reason: str = "failed_impl_reentry",
    source: str = "rvf_rescope",
    armed_at: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """写入 rescope marker。优先用 task_id；无 task_id 时退回 session_id；都无返回 None。

    set point 在主 agent 经 ``rvf_rescope.py arm`` 检测到失败再入、准备开修之时。
    """
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        target_run_id=target_run_id,
        repo=repo,
        reason=reason,
        source=source,
        armed_at=armed_at,
    )
    target = paths[0]
    _atomic_write(target, payload)
    return target


def read_review_reopen_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """读取 marker；找到第一个匹配就返回，不做副作用。"""
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        data = _read_json(path)
        if data is not None:
            data.setdefault("_marker_path", str(path))
            return data
    return None


def clear_review_reopen_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
    """删除（consume）该上下文下的所有 marker（task 与 session 各一）。

    返回被实际删除的文件路径列表（用于 ledger 记录）。
    """
    removed: list[str] = []
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(str(path))
    return removed


def review_reopen_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    """返回 marker 状态：active / stale / invalid。

    优先按 ``expires_at`` 判定；缺失时退回 ``armed_at`` + TTL。
    """
    if not isinstance(marker, dict):
        return STATUS_INVALID
    current_ts = (
        datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    )
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is not None:
        return STATUS_ACTIVE if current_ts <= expires_ts else STATUS_STALE
    armed_ts = _parse_iso_ts(marker.get("armed_at"))
    if armed_ts is None:
        return STATUS_INVALID
    return STATUS_ACTIVE if current_ts - armed_ts <= ttl_seconds() else STATUS_STALE
