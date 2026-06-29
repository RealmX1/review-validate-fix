#!/usr/bin/env python3
"""Task/session scoped in-progress marker for Cline Kanban RVF follow-up runs."""

from __future__ import annotations

import json
import os
import secrets
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import safe_token  # noqa: E402


SUBDIR_NAME = "kanban-followup-in-progress"
MARKER_VERSION = 1
# 锁的合法寿命 = agent 处理「一轮 follow-up review」的时间（分钟级，实测最长约 47min）。
# 历史默认 6h 是「兜底」，但它同时也是「一把因故未 handoff 的锁把 agent Stop 静默挡住」的
# 最坏冻结时长。砍到 1h：既覆盖正常长 follow-up，又把卡死锁最坏自释放窗口从 6h 压到 ≤1h
# （锁过期→STALE→读侧惰性清）。需要更长可经 TTL_ENV 覆盖。
DEFAULT_TTL_SECONDS = 60 * 60
TTL_ENV = "RVF_KANBAN_FOLLOWUP_IN_PROGRESS_TTL_SECONDS"
LOCK_ROOT_ENV = "RVF_KANBAN_FOLLOWUP_LOCK_ROOT"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"


@dataclass(frozen=True)
class AcquireResult:
    acquired: bool
    path: Path | None
    marker: dict[str, Any] | None
    status: str


def _root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".rvf" / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"sess-{safe_token(session_id)}.json"


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[Path]:
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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _open_marker_exclusive(path: Path, encoded: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _takeover_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.takeover.lock")


@contextmanager
def _with_takeover_lock(path: Path):
    lock_path = _takeover_lock_path(path)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
) -> dict[str, Any]:
    ttl = ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": "in_progress",
        "armed_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": attempt_id,
        "session_id": session_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "repo": repo,
        "cwd": cwd,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_path": prompt_path,
    }


def write_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    root: Path | None = None,
) -> Path | None:
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
    )
    target = paths[0]
    # Option A（兑现 nudge 预算的 loop-break 契约）：本函数是 in-progress 锁唯一的 arm 入口
    # （arm_kanban_followup_lock_on_delivery 投递确认时调用），整份覆盖 marker。重派发的
    # delivery re-arm 若不保留既有 reengage_nudge_count，会把『预算用尽再退回静默 skip 防
    # review↔fix 死循环』的计数每轮清回 0（见 codex_stop_review_validate_fix.py 的 nudge 分支）。
    # 故在覆盖前读出同 task 既有 marker 的计数并带入新 payload：首次 arm（无既有 marker）自然从
    # 0 起，re-arm 则让计数跨投递存活。best-effort：read_marker 吞读错、reengage_nudge_count
    # 坏值按 0（保守：宁可多给一次 nudge，也不因坏值误判预算耗尽而静默挡停）。
    carried_nudge_count = reengage_nudge_count(
        read_marker(task_id=task_id, session_id=session_id, root=root)
    )
    if carried_nudge_count:
        payload["reengage_nudge_count"] = carried_nudge_count
    _atomic_write(target, payload)
    return target


def acquire_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    root: Path | None = None,
) -> AcquireResult:
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return AcquireResult(acquired=True, path=None, marker=None, status=STATUS_INVALID)
    target = paths[0]
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for _ in range(2):
        try:
            _open_marker_exclusive(target, encoded)
        except FileExistsError:
            marker = read_marker(task_id=task_id, session_id=session_id, root=root)
            status = marker_status(marker)
            if status == STATUS_ACTIVE:
                return AcquireResult(acquired=False, path=target, marker=marker, status=status)
            with _with_takeover_lock(target):
                marker = read_marker(task_id=task_id, session_id=session_id, root=root)
                status = marker_status(marker)
                if status == STATUS_ACTIVE:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    _open_marker_exclusive(target, encoded)
                except FileExistsError:
                    continue
                return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
            continue
        return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
    marker = read_marker(task_id=task_id, session_id=session_id, root=root)
    return AcquireResult(
        acquired=False,
        path=target,
        marker=marker,
        status=marker_status(marker),
    )


def read_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            return payload
    return None


def clear_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
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


def marker_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    if not isinstance(marker, dict):
        return STATUS_INVALID
    current_ts = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is not None:
        return STATUS_ACTIVE if current_ts <= expires_ts else STATUS_STALE
    armed_ts = _parse_iso_ts(marker.get("armed_at"))
    if armed_ts is None:
        return STATUS_INVALID
    return STATUS_ACTIVE if current_ts - armed_ts <= ttl_seconds() else STATUS_STALE


def reengage_nudge_count(marker: dict[str, Any] | None) -> int:
    """读出 in-progress 锁 marker 上已累计的 re-engage nudge 次数（缺省 0）。

    Stop 读侧据此判断「这把 active 锁还剩多少次 re-engage 预算」：在预算内时不再静默放行
    agent 干停，而是放回常规 RVF gate 重新唤起；预算用尽再退回静默 skip（防 review↔fix 死循环）。
    任何结构异常一律按 0 处理（保守：宁可多给一次 nudge，也不因坏值误判为预算耗尽而静默挡停）。
    """
    if not isinstance(marker, dict):
        return 0
    try:
        return max(0, int(marker.get("reengage_nudge_count") or 0))
    except (TypeError, ValueError):
        return 0


def bump_reengage_nudge_count(marker: dict[str, Any]) -> int:
    """读-改-写：把 in-progress 锁 marker 的 ``reengage_nudge_count`` +1，原子写回其原文件。

    直接在 ``marker['_marker_path']``（``read_marker`` 注入的实际命中文件，task-path 或
    session-path）上原地自增，避免重新解析 marker_paths 写到另一条路径上。返回新计数。
    best-effort：无 ``_marker_path`` 或写失败时返回当前计数、绝不抛——nudge 记账永不阻断 Stop。
    """
    current = reengage_nudge_count(marker)
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return current
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["reengage_nudge_count"] = current + 1
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return current
    return current + 1


# ---------------------------------------------------------------------------
# dispatched-unconfirmed (pending) marker family
#
# 与 in-progress 锁并列、物理隔离的一族 marker：在 Stop hook **dispatch 一条 follow-up**
# 时写入（state=``dispatched_unconfirmed``），表示「已交给 Cline Kanban、但尚未确认成为真实
# turn」。投递落地的权威信号是目标 session 的 UserPromptSubmit hook（arm in-progress 锁），
# 它会按 token 清掉对应 pending；若投递静默丢失（如经 terminal fallback 注入到一个已停止的
# session），pending 永不被清，下一次 Stop 据其判定「上次静默丢投」并放行重投。pending 还
# 顺带恢复了 dispatch→delivery 在途窗口的去重保护（active pending 期间短暂跳过重复 dispatch）。
# ---------------------------------------------------------------------------

PENDING_SUBDIR_NAME = "kanban-followup-dispatched"
PENDING_STATE = "dispatched_unconfirmed"
DEFAULT_PENDING_TTL_SECONDS = 15 * 60
PENDING_TTL_ENV = "RVF_KANBAN_FOLLOWUP_PENDING_TTL_SECONDS"


def _pending_root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / PENDING_SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        # in-progress 锁把该 env 视为「直接就是 in-progress 目录」；pending 在其下另起子目录，
        # 与 in-progress marker 物理隔离，避免同名 task-*.json 互相覆盖。
        return Path(raw).expanduser() / PENDING_SUBDIR_NAME
    return Path.home() / ".rvf" / PENDING_SUBDIR_NAME


def _pending_task_path(task_id: str, root: Path | None = None) -> Path:
    return _pending_root(root) / f"task-{safe_token(task_id)}.json"


def pending_ttl_seconds() -> float:
    raw = os.environ.get(PENDING_TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_PENDING_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_PENDING_TTL_SECONDS)
    return max(0.0, value)


def pending_marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    token: str | None,
    delivery_channel: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    kanban_project_path: str | None = None,
    kanban_task_title: str | None = None,
    kanban_task_title_source: str | None = None,
    origin_transcript_path: str | None = None,
) -> dict[str, Any]:
    ttl = pending_ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": PENDING_STATE,
        "dispatched_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": attempt_id,
        "session_id": session_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "repo": repo,
        "cwd": cwd,
        "token": token,
        "delivery_channel": delivery_channel,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_path": prompt_path,
        # 跨 task stranded-sweep（S1b）与 OS 通知 deep-link（S1a）所需的快照字段：
        # 任意会话的 Stop 都能据此对别的 task 的 stale pending 推出 taskUrl 并通知用户。
        "kanban_project_path": kanban_project_path,
        "kanban_task_title": kanban_task_title,
        "kanban_task_title_source": kanban_task_title_source,
        "origin_transcript_path": origin_transcript_path,
    }


def write_pending_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    token: str | None,
    delivery_channel: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    kanban_project_path: str | None = None,
    kanban_task_title: str | None = None,
    kanban_task_title_source: str | None = None,
    origin_transcript_path: str | None = None,
    root: Path | None = None,
) -> Path | None:
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    payload = pending_marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        token=token,
        delivery_channel=delivery_channel,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
        kanban_project_path=kanban_project_path,
        kanban_task_title=kanban_task_title,
        kanban_task_title_source=kanban_task_title_source,
        origin_transcript_path=origin_transcript_path,
    )
    target = _pending_task_path(task_id, root)
    _atomic_write(target, payload)
    return target


def read_pending_marker(
    *,
    task_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    path = _pending_task_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        payload.setdefault("_marker_path", str(path))
        return payload
    return None


def clear_pending_marker(
    *,
    task_id: str | None,
    token: str | None = None,
    root: Path | None = None,
) -> list[str]:
    if not (isinstance(task_id, str) and task_id.strip()):
        return []
    # token 防误清：若磁盘上的 pending 已被一条更新的 dispatch（不同 token）覆盖，
    # 一条迟到的旧投递确认不应清掉这把仍未确认的新 pending。
    if token is not None:
        existing = read_pending_marker(task_id=task_id, root=root)
        if isinstance(existing, dict):
            stored = existing.get("token")
            if isinstance(stored, str) and stored and stored != token:
                return []
    path = _pending_task_path(task_id, root)
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return []
    return [str(path)]


def pending_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    # pending payload 始终带 ``expires_at``，故可直接复用 ``marker_status`` 的过期判定。
    return marker_status(marker, now_ts=now_ts)


def iter_pending_markers(
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """枚举 pending 目录下所有 ``task-*.json`` 的 payload（跨 task）。

    供 S1b 跨 task stranded-sweep 使用：任意会话的 Stop 都能据此发现**别的** task
    遗留的 stale pending。形状仿 ``rvf_prep_file.sweep_stale``——glob 整目录、逐文件
    吞 OSError/JSON 错误，绝不因单个坏文件中断整轮扫荡。每个 payload 注入 ``_marker_path``
    以便调用方定位文件（与 ``read_pending_marker`` 一致）。
    """
    base = _pending_root(root)
    if not base.is_dir():
        return []
    markers: list[dict[str, Any]] = []
    try:
        paths = sorted(base.glob("task-*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            markers.append(payload)
    return markers


def stamp_pending_notified(
    *,
    task_id: str | None,
    token: str | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """给某条 pending marker 盖 ``last_notified_at`` 戳（防 stranded-sweep 刷屏）。

    **read-merge-write，且 ``token`` 必须原样保留**——否则会破坏
    ``clear_pending_marker`` 的 token 防误清 guard（一条迟到的旧投递确认会因 token
    不再匹配而无法清掉，导致 marker 永久误锁）。这里直接读原始文件（不经
    ``read_pending_marker`` 的 ``_marker_path`` 注入），合并戳记后整体写回，
    payload 里既有的 ``token`` 字段保持不动。

    ``token`` 传入时作为「确认在盖正确的 marker」的 guard：若磁盘上 marker 的 token
    与传入不一致（已被更新的 dispatch 覆盖），返回 False、不盖戳。
    """
    if not (isinstance(task_id, str) and task_id.strip()):
        return False
    path = _pending_task_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if token is not None:
        stored = payload.get("token")
        if isinstance(stored, str) and stored and stored != token:
            return False
    payload.pop("_marker_path", None)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload["last_notified_at"] = stamp.isoformat().replace("+00:00", "Z")
    try:
        payload["notify_count"] = int(payload.get("notify_count") or 0) + 1
    except (TypeError, ValueError):
        payload["notify_count"] = 1
    try:
        _atomic_write(path, payload)
    except OSError:
        return False
    return True
