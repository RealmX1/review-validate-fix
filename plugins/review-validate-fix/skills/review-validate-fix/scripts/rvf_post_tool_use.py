#!/usr/bin/env python3
"""RVF PostToolUse hook 核心：在「主 agent 本回合首次文件编辑」时 park 父 Kanban 卡片。

为什么是 PostToolUse-on-edit、而不是 Stop hook 里 park：Claude Code 把同一次 Stop 上
matching 的多个 hook **并行执行、顺序不确定**，且各源 hook 合并——一次 Stop 上 RVF 自己的
``stop.py``（Python，重）与 cline-kanban 烤进 settings 的 ``kanban hooks … to_review``（Node，
轻）同时在场。RVF 的 park 路径结构上更长（冷启 + 评估 + 再 spawn ``kanban task park`` 走第二
趟 tRPC），几乎必然晚于 to_review；且卡片 ``turnOwner`` 一旦翻成 user，park 还会被
``turnOwner==="agent"`` 闸直接拒绝。→ Stop-hook park = 双重死路。

唯一 race-free 落点 = 「在该回合的 Stop **之前**、回合仍活跃（turnOwner==='agent'）时 park」。
本回合（Turn 1 实现回合）唯一的 during-turn 信号就是它自己的工具调用——故采用 PostToolUse：
本回合首次写型工具（Edit/Write/MultiEdit/NotebookEdit）落地时 park。它发生在 Turn 1 的 Stop
之前 → 对并行的 to_review snapshot 确定性早到（race-free）；且「发生了编辑」比「任意非 token
回合」更紧地预示「确有 resume 在路上」。

资格判定（哪一轮该 park）由目标 session 的 UserPromptSubmit hook 在回合开头写下
（``mark_park_eligibility``）：非 token、非 kanban-followup marker、非 manual、且在 kanban task
的回合（=Turn 1）才 eligible；Turn 2（被注入 followup 唤回、带 token+marker）eligible=false，
绝不 park（它是终态「待人审」回合，必须让 kanban to_review 正常发）。

park 泄漏的清标（dispatch 失败/不派发/stranded）由 Stop hook 侧的幂等 unpark 安全阀兜底
（见 ``codex_stop_review_validate_fix`` 的 self-park 安全阀 + stranded-sweep）；本文件只负责置标。

刻意 stdlib-only、全程 best-effort 永不抛：hook 是最该 fail-open 的安全面。park 失败 = 无抑制
（与今日同），但会记一行 ledger，绝不静默假装成功。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 本回合首次编辑才 park：只认写型工具，与 ``session_change_manifest.CLAUDE_WRITE_TOOL_NAMES`` 同集。
# 内联而非 import，保 hook 热路径 stdlib-only、零重依赖。
WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

STATE_ROOT_ENV = "CODEX_RVF_KANBAN_PARK_STATE_ROOT"
STATE_SUBDIR = "kanban-park-turn-eligibility"
DEFAULT_PARK_TIMEOUT_SECONDS = 12.0
PARK_TIMEOUT_ENV = "CODEX_RVF_KANBAN_PARK_TIMEOUT_SECONDS"

DEFAULT_CLINE_KANBAN_CLIENT = Path(__file__).resolve().parent / "cline_kanban_client.py"
DEFAULT_TASK_CMD = "kanban task"


# ---------------------------------------------------------------------------
# 状态文件（每会话一文件，nonce=每回合键 → park 每回合至多一次、下回合自动重置）。
# ---------------------------------------------------------------------------


def _state_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    raw = os.environ.get(STATE_ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".rvf" / STATE_SUBDIR


def _safe_key(session_id: str) -> str:
    # 文件系统安全 + 稳定：sha1 截断；不可读但每会话唯一，state 文件无需人读。
    return hashlib.sha1(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _eligibility_path(session_id: str, root: str | Path | None = None) -> Path:
    return _state_root(root) / f"sess-{_safe_key(session_id)}.json"


def _parked_path(session_id: str, root: str | Path | None = None) -> Path:
    return _state_root(root) / f"sess-{_safe_key(session_id)}.parked.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def mark_park_eligibility(
    session_id: str | None,
    *,
    eligible: bool,
    nonce: str | None = None,
    root: str | Path | None = None,
) -> str | None:
    """目标 session 的 UPS 在回合开头写本回合 park 资格（每回合一个 fresh nonce）。

    best-effort：缺 session_id 或写失败都返回 None、绝不抛（绝不阻断 prompt）。返回写下的 nonce。
    """
    if not (isinstance(session_id, str) and session_id.strip()):
        return None
    session_id = session_id.strip()
    nonce = nonce or secrets.token_hex(8)
    try:
        _atomic_write(
            _eligibility_path(session_id, root),
            {"eligible": bool(eligible), "nonce": nonce, "updated_at": _iso_now()},
        )
    except OSError:
        return None
    return nonce


def read_park_eligibility(session_id: str, *, root: str | Path | None = None) -> dict[str, Any] | None:
    return _read_json(_eligibility_path(session_id, root))


def read_self_park_state(session_id: str | None, *, root: str | Path | None = None) -> dict[str, Any] | None:
    """Stop hook 安全阀读：本会话是否被本插件 self-park 过（含 task_id/project_path 供 unpark）。"""
    if not (isinstance(session_id, str) and session_id.strip()):
        return None
    return _read_json(_parked_path(session_id.strip(), root))


def clear_self_park_state(session_id: str | None, *, root: str | Path | None = None) -> bool:
    if not (isinstance(session_id, str) and session_id.strip()):
        return False
    try:
        _parked_path(session_id.strip(), root).unlink()
    except (FileNotFoundError, OSError):
        return False
    return True


def _append_ledger(session_id: str, record: dict[str, Any], root: str | Path | None = None) -> None:
    # park 成败都记一行 jsonl（含失败 stderr），保「park 失败被观测到、不静默假装成功」。
    try:
        path = _state_root(root) / f"sess-{_safe_key(session_id)}.parklog.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _iso_now(), **record}, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# event → kanban 标识（内联，避免热路径 import 重模块）。
# ---------------------------------------------------------------------------


def _event_or_env_text(
    event: dict[str, Any],
    env_names: tuple[str, ...],
    event_keys: tuple[str, ...],
) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in event_keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def current_kanban_task_id(event: dict[str, Any]) -> str | None:
    return _event_or_env_text(
        event,
        ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"),
        ("kanban_task_id", "kanbanTaskId", "task_id", "taskId"),
    )


def current_kanban_project_path(event: dict[str, Any], fallback: str) -> str:
    value = _event_or_env_text(
        event,
        ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH"),
        ("kanban_project_path", "kanbanProjectPath", "project_path", "projectPath"),
    )
    return value or fallback


def _session_id_from_event(event: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "session_hook_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_name_from_event(event: dict[str, Any]) -> str | None:
    for key in ("tool_name", "toolName", "tool"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _park_timeout_seconds() -> float:
    raw = os.environ.get(PARK_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PARK_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PARK_TIMEOUT_SECONDS


def _park_via_client(*, task_id: str, project_path: str, label: str) -> tuple[bool, str]:
    """spawn ``cline_kanban_client.py park``。返回 (ok, detail)。best-effort、永不抛。"""
    client = os.environ.get("CODEX_RVF_CLINE_KANBAN_CLIENT") or str(DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_TASK_CMD)
    command = [
        sys.executable,
        client,
        "park",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
        "--label",
        label,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_park_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort，park 失败=无抑制（与今日同）
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, detail[:500] or f"park exited {completed.returncode}"
    return True, (completed.stdout or "").strip()[:500]


def inspect_post_tool_use(event: dict[str, Any], *, root: str | Path | None = None) -> dict[str, Any]:
    """PostToolUse 核心决策 + 副作用。返回结果 dict（供测试/诊断），永不抛。

    早退顺序（任一不满足即 noop）：不在 kanban task → noop；非写型工具 → noop；本回合
    eligibility 非 true → noop；本回合 nonce 已 park 过 → noop。否则 spawn park 并写 parked 标记。
    """
    if not isinstance(event, dict):
        return {"status": "skipped", "reason": "not_a_dict"}
    task_id = current_kanban_task_id(event)
    if not task_id:
        return {"status": "skipped", "reason": "not_kanban_task"}
    tool_name = _tool_name_from_event(event)
    if tool_name not in WRITE_TOOL_NAMES:
        return {"status": "skipped", "reason": "not_write_tool", "tool_name": tool_name}
    session_id = _session_id_from_event(event)
    if not session_id:
        return {"status": "skipped", "reason": "no_session_id"}

    eligibility = read_park_eligibility(session_id, root=root)
    if not (isinstance(eligibility, dict) and eligibility.get("eligible") is True):
        return {"status": "skipped", "reason": "not_eligible"}
    nonce = eligibility.get("nonce")
    if not (isinstance(nonce, str) and nonce):
        return {"status": "skipped", "reason": "no_nonce"}

    parked = read_self_park_state(session_id, root=root)
    if isinstance(parked, dict) and parked.get("nonce") == nonce:
        return {"status": "noop", "reason": "already_parked_this_turn", "task_id": task_id}

    project_path = current_kanban_project_path(event, str(event.get("cwd") or os.getcwd()))
    label = f"rvf-self-rising:{_safe_key(session_id)}"
    ok, detail = _park_via_client(task_id=task_id, project_path=project_path, label=label)
    _append_ledger(
        session_id,
        {
            "event": "park" if ok else "park_failed",
            "status": "ok" if ok else "degraded",
            "task_id": task_id,
            "project_path": project_path,
            "tool_name": tool_name,
            "detail": detail,
        },
        root=root,
    )
    if not ok:
        # park 失败 = 无抑制（与今日同），但已记 ledger；不写 parked 标记（无 park 需 unpark）。
        return {"status": "park_failed", "reason": detail, "task_id": task_id}
    try:
        _atomic_write(
            _parked_path(session_id, root),
            {
                "nonce": nonce,
                "task_id": task_id,
                "project_path": project_path,
                "parked_at": _iso_now(),
            },
        )
    except OSError:
        pass
    return {"status": "parked", "task_id": task_id, "project_path": project_path}


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        inspect_post_tool_use(event)
    except Exception:  # noqa: BLE001 - hook 必须 fail-open
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
