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

key 里**还含仓身份与父 harness 身份**（``scope_marker_identity_key``）。两者各修一个撞键：
前者修「不同 project 的 cline-kanban task id 短哈希撞车 ⇒ 一个仓的 baseline 盖到另一个仓」；
后者修「同一个 worktree 被 Claude 与 Codex 同时打开 ⇒ 两个会话的 UPS 互相覆盖，A 的 Stop
读到 B 上一条 prompt 的 HEAD、窗口下界被抬高 ⇒ 漏审」。后者历史上一直被双状态根偶然掩盖，
统一根之后会立刻开始流血。``review_highwater_marker`` **刻意只含前者**，理由见
``scope_marker_identity_key`` 模块头。

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

# 本模块迁入 core/ 后既被当作 ``core.session_scope_allocation.round_baseline_marker`` import、
# 也作为 CLI(``__main__``) 与 ``spec_from_file_location`` 直接加载。后两种上下文里含 ``core/``
# 的 PYROOT 尚不在 ``sys.path``，且 ``_rvf_pyroot.py`` 不在本目录（它在 ``scripts/``），无法沿用
# scripts 侧「同目录 ``import _rvf_pyroot``」自举。故内联哨兵自举：自底向上找同时含 ``.rvf-pyroot``
# 哨兵与 ``core/`` 的最近祖先（与 ``_rvf_pyroot`` 同口径、depth-robust），插入 ``sys.path``。被当作
# ``core.*`` import 时 PYROOT 已在 path，幂等无副作用。
_pyroot_search_start = Path(__file__).resolve()
for _pyroot_candidate in (_pyroot_search_start, *_pyroot_search_start.parents):
    if (_pyroot_candidate / ".rvf-pyroot").is_file() and (_pyroot_candidate / "core").is_dir():
        if str(_pyroot_candidate) not in sys.path:
            sys.path.insert(0, str(_pyroot_candidate))
        break
from core.run_ledger.run_ledger import log_root, safe_token  # noqa: E402
from core.session_scope_allocation.scope_marker_identity_key import (  # noqa: E402
    resolve_parent_harness_identity_for_scope_marker_key,
    resolve_repository_identity_for_scope_marker_key,
    scope_marker_candidate_paths,
)


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


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    parent_harness_identity: str | None = None,
    root: Path | None = None,
) -> list[Path]:
    """返回该上下文下可能持有 marker 的所有候选路径。写者取 ``[0]``，读者取第一个存在的。

    task_id 优先；两者都缺、或 ``repo`` 的仓身份解析不出来，都返回空列表，调用方应据此
    跳过写 / 读（fail-closed 的方向是窗口偏宽 ⇒ 多审，不是 marker 混住 ⇒ 漏审）。

    ``parent_harness_identity=None`` 表示「就地判定」（``resolve_parent_harness_identity``）。
    每个 key 后面还跟一条 ``--harness-unspecified`` 回退候选，接住迁移写出的历史 marker
    （旧格式没记过 harness）与「写入点比读取点少一层可用信号」时留下的那份。
    """
    return scope_marker_candidate_paths(
        marker_directory=_baseline_root(root),
        task_id_token=safe_token(task_id) if task_id else None,
        session_id_token=safe_token(session_id) if session_id else None,
        repository_identity=resolve_repository_identity_for_scope_marker_key(repo),
        parent_harness_identity=resolve_parent_harness_identity_for_scope_marker_key(
            parent_harness_identity
        ),
    )


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
    parent_harness_identity: str | None = None,
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
        # 落盘 key 里那两段身份的明文来源。marker 文件名里仓身份只剩一个哈希短码，取证时
        # 无从反查是哪个仓；迁移工具也靠 ``repository_identity`` 把旧文件名重映射到新 key。
        "repository_identity": resolve_repository_identity_for_scope_marker_key(repo),
        "parent_harness_identity": resolve_parent_harness_identity_for_scope_marker_key(
            parent_harness_identity
        ),
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
    parent_harness_identity: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """写入本轮 baseline marker。优先用 task_id；无 task_id 时退回 session_id；
    都无（或仓身份解析不出）返回 None。每条新 prompt 覆盖（``_atomic_write`` overwrites）。

    set point 在 ``rvf_user_prompt_submit.inspect_user_prompt_submit`` 解析出
    repo 与 session 之后；best-effort，绝不阻断 prompt。
    """
    if not isinstance(baseline_head, str) or not baseline_head.strip():
        return None
    # harness 身份**只判定一次**，路径与 payload 共用同一个取值：分两次判定的话，一旦
    # 中间有信号变化，文件名说 A、正文说 B，取证时无从分辨哪个才是真的写入方。
    resolved_harness_identity = resolve_parent_harness_identity_for_scope_marker_key(
        parent_harness_identity
    )
    paths = marker_paths(
        task_id=task_id,
        session_id=session_id,
        repo=repo,
        parent_harness_identity=resolved_harness_identity,
        root=root,
    )
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        baseline_head=baseline_head.strip(),
        repo=repo,
        prompt_excerpt=prompt_excerpt,
        captured_at=captured_at,
        parent_harness_identity=resolved_harness_identity,
    )
    target = paths[0]
    _atomic_write(target, payload)
    return target


def read_round_baseline_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    parent_harness_identity: str | None = None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """读取 marker；找到第一个匹配就返回，不做副作用。"""
    for path in marker_paths(
        task_id=task_id,
        session_id=session_id,
        repo=repo,
        parent_harness_identity=parent_harness_identity,
        root=root,
    ):
        data = _read_json(path)
        if data is not None:
            data.setdefault("_marker_path", str(path))
            return data
    return None


def clear_round_baseline_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    parent_harness_identity: str | None = None,
    root: Path | None = None,
) -> list[str]:
    """删除该上下文下的所有 marker（task 与 session 各一）。

    Stop hook 默认 **不** 主动清除（下条 prompt 覆盖即可），此函数供显式清理 /
    测试使用。返回被实际删除的文件路径列表。

    注意它会连 ``--harness-unspecified`` 那条回退候选一起删——那份可能是**别的** harness
    在判不出身份时留下的。当前只有显式清理 / 测试会调它，故按「清干净这个 (task,session)
    在本仓下的全部 marker」处理；哪天有业务路径要调，先想清楚要不要收窄到自己那一份。
    """
    removed: list[str] = []
    for path in marker_paths(
        task_id=task_id,
        session_id=session_id,
        repo=repo,
        parent_harness_identity=parent_harness_identity,
        root=root,
    ):
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
    repo: str | Path | None,
    parent_harness_identity: str | None = None,
    root: Path | None = None,
    now_ts: float | None = None,
) -> str | None:
    """便捷读取：仅当存在且 ``active`` 时返回 ``baseline_head``，否则 None。

    Stop hook 用它一步拿到可用下界；stale/invalid/缺失都降级为 None ⇒ 行为与
    今日（无 committed 观测）完全一致。
    """
    marker = read_round_baseline_marker(
        task_id=task_id,
        session_id=session_id,
        repo=repo,
        parent_harness_identity=parent_harness_identity,
        root=root,
    )
    if round_baseline_status(marker, now_ts=now_ts) != STATUS_ACTIVE:
        return None
    assert marker is not None
    baseline = marker.get("baseline_head")
    return baseline.strip() if isinstance(baseline, str) and baseline.strip() else None
