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
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import

from core.subagents.models import InvokeCommand, SpawnRecord, iter_jsonl_dicts  # noqa: E402


def build_analyze_command(*, codex_bin: str) -> InvokeCommand:
    """Codex headless analyze 调用向量：``codex --ask-for-approval never
    --sandbox workspace-write exec -``，prompt 走 stdin。

    ``codex_bin`` 由调用方（skill facade）解析后传入，使本 adapter 不耦合 RVF 的
    ``CODEX_RVF_CODEX_BIN`` env 约定。``--sandbox workspace-write`` 允许 analyze
    agent Edit ``summary.md`` / ``causality.json``；末元素 ``-`` 表示从 stdin 读
    prompt。
    """
    return InvokeCommand(
        argv=[
            codex_bin,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "exec",
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


def discover_spawned_agents(rollout_path: Path) -> list[SpawnRecord]:
    """扫主 rollout，挑出所有 ``event_msg.collab_agent_spawn_end`` events。

    按出现顺序返回；不去重（同 agent_id 理论上只 spawn 一次，但保留多份以防
    schema 变化）。
    """
    out: list[SpawnRecord] = []
    for line_index, record in iter_jsonl_dicts(rollout_path):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "collab_agent_spawn_end":
            continue
        agent_id = payload.get("new_thread_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None
        role = payload.get("new_agent_role") if isinstance(payload.get("new_agent_role"), str) else None
        nickname = (
            payload.get("new_agent_nickname")
            if isinstance(payload.get("new_agent_nickname"), str)
            else None
        )
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        out.append(
            SpawnRecord(
                call_id=call_id,
                agent_id=agent_id,
                role=role,
                nickname=nickname,
                prompt=prompt,
                ts=ts,
                line_index=line_index,
            )
        )
    return out


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
    matches = list(root.glob(f"**/rollout-*-{agent_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def resolve_subagents(
    *,
    main_rollout_path: Path,
    original_transcript: Path | None = None,  # noqa: ARG001 — Codex 不需要原始 transcript
    sessions_root: Path | None = None,
) -> list[tuple[SpawnRecord, Path | None]]:
    """facade 统一契约：返回 ``(spawn, 源 rollout 路径 | None)`` 列表。

    Codex 路径：从主 rollout 发现 spawn，再在 sessions 目录 glob 定位每个子代理
    独立 rollout（找不到则源为 None，facade 写 pointer-only manifest）。
    """
    if not main_rollout_path.exists():
        return []
    spawns = discover_spawned_agents(main_rollout_path)
    return [
        (spawn, find_subagent_rollout(spawn.agent_id, sessions_root=sessions_root))
        for spawn in spawns
    ]
