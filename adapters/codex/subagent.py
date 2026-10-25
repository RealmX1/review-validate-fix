"""Codex host 子代理 adapter（观测侧发现 + 调用侧 argv 构造）。

**观测侧**：Codex 主会话通过内置工具 ``spawn_agent`` 派生子代理（reviewer /
validate-fix 等），每个子代理拥有独立 session_id 和
``~/.codex/sessions/.../rollout-*-<id>.jsonl``。本模块封装「如何在 Codex 主
rollout 里发现 spawn、如何在 Codex sessions 目录里定位子代理 rollout」这两件
host 耦合的事；通用的 copy/distill/manifest 骨架在 ``subagent_capture`` facade，
host 中性。

**调用侧**：``build_analyze_command`` 给出 Codex headless analyze 调用的 argv
形态（``codex … exec -``）。按 host 选哪个 adapter 的分派留在 skill facade
（``rvf_analyze_thread.build_analyze_command``）。
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import

from core.subagents.models import InvokeCommand, SpawnRecord, iter_jsonl_dicts  # noqa: E402


def build_analyze_command(*, codex_bin: str, formal_analysis_directory: Path) -> InvokeCommand:
    """Codex headless analyze 调用向量：``codex --ask-for-approval never
    --sandbox workspace-write exec --add-dir <analysis> -``，prompt 走 stdin。

    ``codex_bin`` 由调用方（skill facade）解析后传入，使本 adapter 不耦合 RVF 的
    ``CODEX_RVF_CODEX_BIN`` env 约定。``--sandbox workspace-write`` 允许 analyze
    agent Edit ``summary.md`` / ``causality.json``；正式归档在业务仓库之外，须显式
    用 ``--add-dir`` 授权该 run 的 analysis 目录。末元素 ``-`` 表示从 stdin 读 prompt。
    """
    return InvokeCommand(
        argv=[
            codex_bin,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "exec",
            "--add-dir",
            str(formal_analysis_directory.expanduser().resolve()),
            "-",
        ],
        uses_stdin=True,
    )


def codex_sessions_root() -> Path:
    """Codex rollout 的根目录。

    优先级：``$CODEX_HOME/sessions`` > ``~/.codex/sessions``。这是 Codex CLI
    自身查 ``~/.codex/`` 时使用的同一约定。
    """
    home = os.environ.get("CODEX_HOME")
    if home:
        candidate = Path(home).expanduser()
    else:
        candidate = Path.home() / ".codex"
    return candidate / "sessions"


def discover_spawned_agents(rollout_path: Path, *, parent_session_id: str | None = None) -> list[SpawnRecord]:
    """关联实际派发调用、原生 SubAgentActivity 与返回值；不用 task_name 猜线程身份。"""
    calls: dict[str, dict[str, Any]] = {}
    call_timestamps: dict[str, str] = {}
    out: dict[str, SpawnRecord] = {}
    recorded_parent_session_id = codex_rollout_session_id(rollout_path)
    if parent_session_id and recorded_parent_session_id and parent_session_id != recorded_parent_session_id:
        return []
    parent_session_id = parent_session_id or recorded_parent_session_id

    def decoded_object(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    for line_index, record in iter_jsonl_dicts(rollout_path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        subtype = payload.get("type")
        call_id = payload.get("call_id")
        if record.get("type") == "response_item" and subtype in ("function_call", "custom_tool_call"):
            if str(payload.get("name", "")).split(".")[-1] == "spawn_agent" and isinstance(call_id, str):
                calls[call_id] = decoded_object(payload.get("arguments") or payload.get("input"))
                if isinstance(record.get("timestamp"), str):
                    call_timestamps.setdefault(call_id, record["timestamp"])
            continue
        agent_id = None
        details: dict[str, Any] = payload
        if record.get("type") == "event_msg" and subtype == "collab_agent_spawn_end":
            if parent_session_id and payload.get("sender_thread_id") and payload["sender_thread_id"] != parent_session_id:
                continue
            agent_id = payload.get("new_thread_id")
        elif record.get("type") == "event_msg" and subtype == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "SubAgentActivity" or item.get("kind") != "started":
                continue
            if parent_session_id and payload.get("thread_id") != parent_session_id:
                continue
            call_id = item.get("id")
            if not isinstance(call_id, str) or call_id not in calls:
                continue
            agent_id, details = item.get("agent_thread_id"), item
        elif record.get("type") == "response_item" and subtype in ("function_call_output", "custom_tool_call_output"):
            if not isinstance(call_id, str) or call_id not in calls:
                continue
            details = decoded_object(payload.get("output"))
            agent_id = details.get("agent_id") or details.get("new_thread_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        arguments = calls.get(call_id, {})
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        spawn = SpawnRecord(
            call_id=call_id if isinstance(call_id, str) else None, agent_id=agent_id,
            role=details.get("new_agent_role") or arguments.get("agent_type"),
            nickname=details.get("new_agent_nickname") or details.get("nickname") or details.get("agent_path"),
            prompt=details.get("prompt") or arguments.get("message") or arguments.get("prompt"),
            ts=call_timestamps.get(call_id) or ts, line_index=line_index,
        )
        # 同一次派发可能同时有返回值和原生活动事件，只归档一份子会话。
        out.setdefault(agent_id, spawn)
    return list(out.values())


def codex_rollout_session_id(rollout_path: Path) -> str | None:
    try:
        for _line_index, record in iter_jsonl_dicts(rollout_path):
            if record.get("type") == "session_meta":
                payload = record.get("payload")
                session_id = payload.get("id") if isinstance(payload, dict) else None
                return session_id if isinstance(session_id, str) and session_id else None
    except OSError:
        return None
    return None


def find_subagent_rollout(
    agent_id: str,
    *,
    sessions_root: Path | None = None,
) -> Path | None:
    """在 Codex sessions 目录里 glob ``rollout-*-<agent_id>.jsonl``。

    匹配多个时返回 mtime 最新的一份（理论上只有一份，多份说明用户 / Codex
    出过 anomaly，新的更可信）。找不到返回 None。
    """
    root = sessions_root if sessions_root is not None else codex_sessions_root()
    if not root.is_dir():
        return None
    if not agent_id or any(character in agent_id for character in "/\\*?[]"):
        return None
    matches = [
        path for path in root.glob(f"**/rollout-*-{agent_id}.jsonl")
        if codex_rollout_session_id(path) == agent_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def resolve_subagents(
    *,
    main_rollout_path: Path,
    original_transcript: Path | None = None,
    sessions_root: Path | None = None,
) -> list[tuple[SpawnRecord, Path | None]]:
    """facade 统一契约：返回 ``(spawn, 源 rollout 路径 | None)`` 列表。

    Codex 路径：从主 rollout 发现 spawn，再在 sessions 目录 glob 定位每个子代理
    独立 rollout（找不到则源为 None，facade 写 pointer-only manifest）。
    """
    if not main_rollout_path.exists():
        return []
    spawns = discover_spawned_agents(main_rollout_path, parent_session_id=(
        codex_rollout_session_id(original_transcript) if original_transcript is not None else None
    ))
    if sessions_root is None and original_transcript is not None:
        sessions_root = next((parent for parent in original_transcript.parents if parent.name == "sessions"), None)
    return [
        (spawn, find_subagent_rollout(spawn.agent_id, sessions_root=sessions_root))
        for spawn in spawns
    ]
