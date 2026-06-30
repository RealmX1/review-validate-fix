#!/usr/bin/env python3
"""本轮起点基线标记（UserPromptSubmit 写、Stop 读）。

用途：记录「上一条 user prompt 提交时」的 ``HEAD``，作为 RVF 下一次 Stop
判定「本轮 agent 已提交但未审改动」的下界 baseline。Stop hook 以
``baseline_head..HEAD`` 的 first-parent 净 diff 派生 committed 观测单元，与
dirty 观测合流后再交集 transcript 归属，得到「本轮已提交、属于本会话、尚未审」
的范围（见 ``reviewable_unit_diff_tracker._list_committed_round_changed_paths`` /
``session_change_manifest.build_manifest(committed_baseline=...)``）。

存储与原子写镜像 ``review_reopen_marker``：env-root、
task+session 双键、原子 rename 写、TTL 状态（active/stale/invalid）。

多 prompt 语义（严格对齐「自上一条 user prompt」）：**每条新 prompt 覆盖标记**。
同一任务内若用户连发两条 prompt、中间未 Stop，第一条提交会随 baseline 前移而
落出窗口——这正是 "since the *last* user prompt" 的字面语义。

刻意只承载 diagnostic 之外的最小事实：``baseline_head`` 是唯一会被 Stop 消费的
字段；``prompt_excerpt`` 仅供排错，绝不参与 scope 计算。
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


SUBDIR_NAME = "round-baseline-pending"
MARKER_VERSION = 1
DEFAULT_TTL_SECONDS = 6 * 60 * 60
TTL_ENV = "RVF_ROUND_BASELINE_TTL_SECONDS"
PROMPT_EXCERPT_MAX_CHARS = 200
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"


def _baseline_root(root: Path | None = None) -> Path:
    return (root.expanduser() if root is not None else log_root()) / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _baseline_root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _baseline_root(root) / f"sess-{safe_token(session_id)}.json"


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
    baseline_head: str,
    repo: str | None,
    prompt_excerpt: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    timestamp = captured_at or _iso_now()
    ttl = ttl_seconds()
    excerpt = (prompt_excerpt or "")[:PROMPT_EXCERPT_MAX_CHARS]
    return {
        "marker_version": MARKER_VERSION,
        "state": "round_baseline",
        "captured_at": timestamp,
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "baseline_head": baseline_head,
        "repo": repo,
        "kanban_task_id": task_id,
        "session_id": session_id,
        "prompt_excerpt": excerpt,
    }


def write_round_baseline_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    baseline_head: str,
    repo: str | None,
    prompt_excerpt: str | None = None,
    captured_at: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """写入本轮 baseline marker。优先用 task_id；无 task_id 时退回 session_id；
    都无返回 None。每条新 prompt 覆盖（``_atomic_write`` overwrites）。

    set point 在 ``rvf_user_prompt_submit.inspect_user_prompt_submit`` 解析出
    repo 与 session 之后；best-effort，绝不阻断 prompt。
    """
    if not isinstance(baseline_head, str) or not baseline_head.strip():
        return None
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        baseline_head=baseline_head.strip(),
        repo=repo,
        prompt_excerpt=prompt_excerpt,
        captured_at=captured_at,
    )
    target = paths[0]
    _atomic_write(target, payload)
    return target


def read_round_baseline_marker(
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


def clear_round_baseline_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
    """删除该上下文下的所有 marker（task 与 session 各一）。

    Stop hook 默认 **不** 主动清除（下条 prompt 覆盖即可），此函数供显式清理 /
    测试使用。返回被实际删除的文件路径列表。
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


def round_baseline_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    """返回 marker 状态：active / stale / invalid。

    优先按 ``expires_at`` 判定；缺失时退回 ``captured_at`` + TTL。``invalid`` 还
    覆盖「缺 baseline_head」——没有可用下界时与无标记等价。
    """
    if not isinstance(marker, dict):
        return STATUS_INVALID
    baseline = marker.get("baseline_head")
    if not isinstance(baseline, str) or not baseline.strip():
        return STATUS_INVALID
    current_ts = (
        datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    )
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is not None:
        return STATUS_ACTIVE if current_ts <= expires_ts else STATUS_STALE
    captured_ts = _parse_iso_ts(marker.get("captured_at"))
    if captured_ts is None:
        return STATUS_INVALID
    return STATUS_ACTIVE if current_ts - captured_ts <= ttl_seconds() else STATUS_STALE


def resolve_round_baseline_head(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
    now_ts: float | None = None,
) -> str | None:
    """便捷读取：仅当存在且 ``active`` 时返回 ``baseline_head``，否则 None。

    Stop hook 用它一步拿到可用下界；stale/invalid/缺失都降级为 None ⇒ 行为与
    今日（无 committed 观测）完全一致。
    """
    marker = read_round_baseline_marker(task_id=task_id, session_id=session_id, root=root)
    if round_baseline_status(marker, now_ts=now_ts) != STATUS_ACTIVE:
        return None
    assert marker is not None
    baseline = marker.get("baseline_head")
    return baseline.strip() if isinstance(baseline, str) and baseline.strip() else None
