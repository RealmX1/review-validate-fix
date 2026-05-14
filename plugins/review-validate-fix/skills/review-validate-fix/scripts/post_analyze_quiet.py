#!/usr/bin/env python3
"""一次性 "post-analyze quiet" 标记。

约束：
- 仅在 dispatcher / Stop hook 在 finalize handoff 后注入 ``$rvf-analyze``
  follow-up 的那个 Stop event 写入 marker；
- 由紧接着的下一次 Stop hook 读取并消费（read + delete = consume）；
- 消费时若 analyze artifacts (summary.md, causality.json) 已 ready 且 mtime
  在 ``armed_at`` 之后，则视为 RVF + analyze 工作流完整结束，跳过下一次自动
  RVF dispatch；否则一次性消费、原样回落到既有逻辑。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rvf_logging import log_root, safe_token


SUBDIR_NAME = "post-analyze-quiet"
MARKER_VERSION = 1


def _quiet_root(root: Path | None = None) -> Path:
    return (root.expanduser() if root is not None else log_root()) / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _quiet_root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _quiet_root(root) / f"sess-{safe_token(session_id)}.json"


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[Path]:
    """返回该上下文下可能持有 marker 的所有候选路径。

    task_id 优先；若两者都缺则返回空列表，调用方应据此跳过写/读。
    """
    paths: list[Path] = []
    if task_id:
        paths.append(_task_path(task_id, root))
    if session_id:
        paths.append(_session_path(session_id, root))
    return paths


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


def write_post_analyze_quiet_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    armed_run_id: str,
    armed_handoff_path: str | None,
    analyze_run_dir: str,
    analyze_summary_md: str,
    analyze_causality_json: str,
    kanban_attempt_id: str | None = None,
    armed_at: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """写入 marker。优先用 task_id；无 task_id 时退回 session_id；都无返回 None。

    set point 在 ``rvf_analyze_advisory.surface_rvf_analyze_advisory`` 注入
    follow-up（无论 kanban-injection 成功 / 失败 / manual fallback）之后，或
    manual ``$rvf-analyze`` deterministic scaffold 时调用。
    """
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    timestamp = armed_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    payload: dict[str, Any] = {
        "marker_version": MARKER_VERSION,
        "armed_at": timestamp,
        "armed_run_id": armed_run_id,
        "armed_handoff_path": armed_handoff_path,
        "analyze_run_dir": analyze_run_dir,
        "analyze_summary_md": analyze_summary_md,
        "analyze_causality_json": analyze_causality_json,
        "kanban_task_id": task_id,
        "kanban_attempt_id": kanban_attempt_id,
        "parent_session_id": session_id,
    }
    target = paths[0]
    _atomic_write(target, payload)
    return target


def read_post_analyze_quiet_marker(
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


def clear_post_analyze_quiet_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
    """删除该上下文下的所有 marker（task 与 session 各一）。

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


def _parse_armed_at(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # 接受 "...Z" 与 "+00:00" 两种 ISO 表示。
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def post_analyze_workflow_complete(marker: dict[str, Any] | None) -> bool:
    """判断 marker 对应的 RVF + $rvf-analyze 工作流是否已完整结束。

    判定标准：``analyze_summary_md`` 与 ``analyze_causality_json`` 两个文件都
    存在，mtime 严格大于 ``armed_at`` 时间戳，summary 已移除
    ``TODO(rvf-analyze)``，且 causality 是有效 JSON object。任一条件不满足 →
    False，调用方应按一次性消费语义丢弃 marker 并继续既有 RVF 逻辑
    （fail-open）。
    """
    if not isinstance(marker, dict):
        return False
    summary_path = marker.get("analyze_summary_md")
    causality_path = marker.get("analyze_causality_json")
    if not (isinstance(summary_path, str) and isinstance(causality_path, str)):
        return False
    armed_ts = _parse_armed_at(marker.get("armed_at"))
    if armed_ts is None:
        return False
    for raw in (summary_path, causality_path):
        try:
            stat = Path(raw).stat()
        except (FileNotFoundError, OSError):
            return False
        if stat.st_mtime <= armed_ts:
            return False
    try:
        summary_text = Path(summary_path).read_text(encoding="utf-8")
    except OSError:
        return False
    if "TODO(rvf-analyze)" in summary_text:
        return False
    try:
        causality = json.loads(Path(causality_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(causality, dict):
        return False
    return True
