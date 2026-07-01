"""父会话来源冻结的 host-中性核（S10e，从引擎 PARENT_CONTEXT 簇迁入）。

``freeze_parent_conversation_origin`` 是核心：给定父 transcript 路径 + 引擎 assembly
层**预解析**的 host 元数据（``session_id`` / ``thread_name_from_index`` /
``session_ref_prefix``）+ 注入的 ``HostAdapter``，产出来源标签 + 元数据 dict。标签走
四级兜底链（app-server 名 → session-index 名 → 首条-user-message 摘录 → session-ref），
其中「首条-user-message 摘录」与「会话深链（codex_url）」两处 host 行为经 ``host_adapter``
取得，其余是中性 policy。

**R-1 边界**：本模块**不**出现任何 host 身份常量或 host 归属键 token——host 身份短前缀
由调用方（引擎 wrapper）以 ``session_ref_prefix`` 字符串传入，host 归属键由 wrapper 在
返回后注入。``codex_url`` 键/值经 ``host_adapter.session_deep_link`` 取得（不触 R-1）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from core.host_adapter import HostAdapter


def short_identifier(value: str | None, fallback: str = "unknown") -> str:
    if not value:
        return fallback
    stripped = value.strip()
    if not stripped:
        return fallback
    first_segment = stripped.split("-", 1)[0]
    if re.match(r"^[A-Fa-f0-9]{8,}(?:-|$)", stripped):
        return first_segment[:12]
    return stripped[:32]


def short_run_ref(run_id: str) -> str:
    match = re.search(r"-([A-Fa-f0-9]{8,})$", run_id)
    if match:
        return match.group(1)[:12]
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]


def transcript_origin_label(path: Path | None, session_id: str | None) -> str | None:
    if path is None:
        return None
    stem = path.stem
    if stem.startswith("rollout-"):
        stem = stem.removeprefix("rollout-")
    match = re.match(
        r"(?P<started>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<session>[A-Za-z0-9]{8,12})",
        stem,
    )
    if match:
        return f"{match.group('started')} {match.group('session')}"
    if session_id:
        return short_identifier(session_id)
    return path.name


def freeze_parent_conversation_origin(
    *,
    parent_thread_path: Path | None,
    run_id: str,
    host_adapter: HostAdapter,
    session_id: str | None,
    thread_name_from_index: str | None,
    session_ref_prefix: str,
    parent_thread_name: str | None = None,
    name_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """冻结父会话来源为标签 + 元数据 dict（**不含** host 归属键，由 wrapper 返回后注入）。

    ``session_id`` / ``thread_name_from_index`` / ``session_ref_prefix`` 由引擎 assembly
    层预解析后传入（分别源自 parent_session_id 兜底 session_id_from_path、Codex
    session-index 查名、host 身份短前缀 Codex/Claude）。首条-user-message 摘录与
    ``codex_url`` 经 ``host_adapter`` 取得。
    """
    transcript_path = str(parent_thread_path) if parent_thread_path is not None else None
    name_source = "app_server_name"
    label = parent_thread_name.strip() if isinstance(parent_thread_name, str) else ""
    if not label:
        label = thread_name_from_index or ""
        name_source = "session_index_thread_name" if label else "session_ref_fallback"
    if not label:
        label = host_adapter.parent_transcript_quoted_session_name(parent_thread_path) or ""
        name_source = "first_user_prompt_fallback" if label else "session_ref_fallback"
    if not label:
        # 前三级 lookup（app-server 名 / session-index / 首条-user-message）在无名/非本 host
        # schema 时落空。这里按 host 身份短前缀（Codex/Claude，wrapper 预解析传入）给出
        # 稳定 ref。
        label = (
            f"{session_ref_prefix} "
            f"{transcript_origin_label(parent_thread_path, session_id) or short_identifier(session_id)}"
        )
        name_source = "session_ref_fallback"
    run_ref = short_run_ref(run_id)
    return {
        "label": label,
        "task_title": f"RVF from {label} run {run_ref}",
        "name_source": name_source,
        "name_lookup": name_lookup,
        "session_id": session_id,
        "session_short_id": short_identifier(session_id),
        # 会话深链只对有深链 scheme 的 host（Codex，含 host 未知兜底）成立；Claude Code
        # 会话经 session_deep_link 返回 None（prompt block 渲染为 <unavailable>），其「打开」
        # 入口由 RVF_PARENT_TRANSCRIPT_PATH 承担。
        "codex_url": host_adapter.session_deep_link(session_id),
        "transcript_path": transcript_path,
        "transcript_file": parent_thread_path.name if parent_thread_path is not None else None,
        "run_id": run_id,
        "run_ref": run_ref,
    }


def source_origin_for_kanban_task(
    *,
    task_id: str,
    attempt_id: str | None,
    task_title: str | None,
    task_title_source: str | None,
    fallback_origin: dict[str, Any],
) -> dict[str, Any]:
    title = task_title.strip() if isinstance(task_title, str) else ""
    origin = dict(fallback_origin)
    if title:
        label = title
        name_source = task_title_source or "cline_kanban_task_title"
    else:
        label = f"Cline Kanban task {task_id}"
        name_source = "cline_kanban_task_id_fallback"
    origin.update(
        {
            "label": label,
            "name_source": name_source,
            "source_kind": "cline-kanban-task",
            "kanban_task_id": task_id,
            "kanban_attempt_id": attempt_id,
            "kanban_task_title": title or None,
            "kanban_task_title_source": task_title_source,
            "source_session_label": fallback_origin.get("label"),
            "source_session_name_source": fallback_origin.get("name_source"),
        }
    )
    return origin


def parent_origin_summary_fields(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    parent_name_lookup: dict[str, Any],
    origin_path: str | None,
) -> dict[str, Any]:
    return {
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "parent_conversation_ref": parent_origin.get("label"),
        "parent_conversation_name": parent_origin.get("label"),
        "parent_conversation_name_source": parent_origin.get("name_source"),
        "parent_thread_name_lookup": parent_name_lookup,
        "parent_codex_url": parent_origin.get("codex_url"),
        "parent_origin_path": origin_path,
        "parent_transcript_file": parent_origin.get("transcript_file"),
        "parent_source_kind": parent_origin.get("source_kind"),
        "parent_kanban_task_id": parent_origin.get("kanban_task_id"),
        "parent_kanban_attempt_id": parent_origin.get("kanban_attempt_id"),
        "parent_kanban_task_title": parent_origin.get("kanban_task_title"),
        "parent_kanban_task_title_source": parent_origin.get("kanban_task_title_source"),
        "parent_source_session_ref": parent_origin.get("source_session_label"),
        "parent_source_session_name_source": parent_origin.get("source_session_name_source"),
    }
