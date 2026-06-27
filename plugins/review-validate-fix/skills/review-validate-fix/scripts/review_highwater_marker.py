#!/usr/bin/env python3
"""最近一次完成的 review 覆盖到的 HEAD（last-reviewed 高水位标记）。

Stop-finalize（``rvf_run_finalize.finalize_run`` 在 ``did_review`` 完成时）与
rvf-land 封窗（``seal_round_baseline_to_head``）**写**，committed-round 漏审检测
（``codex_stop_review_validate_fix.maybe_route_committed_round_scope``）**读**。

为什么需要它（committed-round 第三面盲区 = 落到 baseline 之下的孤儿提交）：
``round_baseline_marker`` 记录「上一条 user prompt 提交时的 HEAD」，UserPromptSubmit
**每条 prompt** 都把它无条件顶到当前 HEAD（"since the last user prompt" 语义）。于是
「在某轮提交、却没在该轮自己的 Stop 里被审」的工作，一旦后续任意一条 prompt（哪怕是
一句只为触发审查的『现在停下让 stophook 接管』）把 round-baseline 推过它，就永久落到
``baseline..HEAD`` 窗口之下、再不被 committed-round 捕获 → 漏审。

本高水位改记「最近一次 **真正完成** 的 review 覆盖到的 HEAD」——只在 review 真正完成
（finalize 检出 reviewer 产物 ``did_review``）或 rvf-land 封窗时推进，而非每条 prompt。
committed-round 以它（而非 round-baseline）作窗口下界，于是孤儿提交一直留在
``reviewed_head..HEAD`` 内直到真被审。语义从『自上一条 prompt』收紧为『自上一次审查』。

与 ``round_baseline_marker`` 的分工：committed-round 优先用本高水位（仅当其为当前 HEAD
的祖先时采用——分支被 reset/rebase 到高水位之前则视为失效）；仅当某 task/session **从未
完成过 review**（无高水位）时，才回退到 round-baseline 作 bootstrap 下界。

durable（**无 TTL 过期**）：高水位是「这个 commit 已被审过」的持久事实；其有效性由消费端
的『是否为当前 HEAD 祖先』判定，而非时间过期——时间过期会让久未活动的 task 重新落入漏审。
键与 ``round_baseline_marker`` 对齐：``task_id`` 优先、``session_id`` 回退、同一 ``log_root()``，
确保 finalize/seal 写到的 ``(task,session)`` 路径正是 committed-round 读的那一个。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rvf_logging import log_root, safe_token


SUBDIR_NAME = "review-highwater"
MARKER_VERSION = 1


def _highwater_root(root: Path | None = None) -> Path:
    return (root.expanduser() if root is not None else log_root()) / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _highwater_root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _highwater_root(root) / f"sess-{safe_token(session_id)}.json"


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[Path]:
    """该上下文下可能持有高水位的候选路径，task_id 优先（与 round_baseline 对齐）。

    两者都缺则返回空列表，调用方据此跳过写 / 读。
    """
    paths: list[Path] = []
    if task_id:
        paths.append(_task_path(task_id, root))
    if session_id:
        paths.append(_session_path(session_id, root))
    return paths


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    reviewed_head: str,
    repo: str | None,
    source: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    return {
        "marker_version": MARKER_VERSION,
        "state": "review_highwater",
        "captured_at": captured_at or _iso_now(),
        "reviewed_head": reviewed_head,
        "repo": repo,
        "kanban_task_id": task_id,
        "session_id": session_id,
        "source": source,
    }


def write_review_highwater(
    *,
    task_id: str | None,
    session_id: str | None,
    reviewed_head: str,
    repo: str | None,
    source: str | None = None,
    captured_at: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """写入 / 推进高水位。优先 task_id；无 task_id 时退回 session_id；都无返回 None。

    幂等覆盖（``_atomic_write``）。**只前移不应回退**的不变量由调用方保证：finalize 仅在
    真正完成 review 时调用、seal 仅在 rvf-land HEAD 调用——二者都是单调向前的 HEAD。本函数
    刻意不做 ancestry 比较（拿不到 repo 上下文且应保持 IO-only，与 ``round_baseline_marker``
    一致）；非单调的极端情形由消费端的 ancestry 守卫兜底。
    """
    if not isinstance(reviewed_head, str) or not reviewed_head.strip():
        return None
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        reviewed_head=reviewed_head.strip(),
        repo=repo,
        source=source,
        captured_at=captured_at,
    )
    target = paths[0]
    _atomic_write(target, payload)
    return target


def read_review_highwater(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """读取高水位；找到第一个匹配（task 优先）就返回，不做副作用。"""
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        data = _read_json(path)
        if data is not None:
            data.setdefault("_marker_path", str(path))
            return data
    return None


def resolve_review_highwater_head(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> str | None:
    """便捷读取：存在则返回 ``reviewed_head``，否则 None（不做 ancestry 判定——交消费端）。"""
    marker = read_review_highwater(task_id=task_id, session_id=session_id, root=root)
    if not isinstance(marker, dict):
        return None
    head = marker.get("reviewed_head")
    return head.strip() if isinstance(head, str) and head.strip() else None


def clear_review_highwater(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
    """删除该上下文下的所有高水位标记（task 与 session 各一）。供显式清理 / 测试使用。"""
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
