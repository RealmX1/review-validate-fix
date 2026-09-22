"""Kimi Code CLI host 子代理 adapter（观测侧发现）。

## 发现路径与另两家的差异

Codex 从主 rollout 里扫 spawn 事件、Claude 从 `<uuid>/subagents/` glob 文件；**Kimi 两者
都不是**——它把整份 agent 名册写在会话的 `state.json` 里：

```json
{"agents": {
   "main":    {"homedir": "…/agents/main",    "type": "main", "parentAgentId": null},
   "agent-0": {"homedir": "…/agents/agent-0", "type": "sub",  "parentAgentId": "main",
               "swarmItem": "分析 skill `grill-me`…"}
}}
```

实测（本机唯一一个多 agent 会话，20 个 agent）：`homedir` **恒等于**
`<session_dir>/agents/<agent_id>`，且 20/20 都存在 `wire.jsonl`。所以定位子代理 transcript
不需要 glob、不需要猜命名，直接读名册即可。

## 三处刻意留 `None` 而不硬凑

`SpawnRecord` 的字段是按 Codex 的 spawn 事件设计的，Kimi 的名册里没有对应物：

- **`call_id`** —— Kimi 的 `state.json` 不记「哪一次 `Agent` 工具调用派生了它」。wire 里的
  `tool.call.toolCallId` 理论上能对上，但那需要跨文件按时间对齐，且**没有任何字段保证这条
  对应关系**。硬编一个会造出看似精确实则无依据的关联。
- **`role`** —— Kimi 的 `type` 只有 `main` / `sub` 两值，是**层级**不是**角色**；Codex 的
  `new_agent_role` 是语义角色。把 `sub` 塞进 `role` 会让跨 host 的 role 统计变成垃圾。
- **`ts`** —— 名册不带 per-agent 时间戳。

**宁可 None，不可假精确**：这些字段的消费者无法分辨「真的没有」与「编出来的」。
"""

from __future__ import annotations

import json
from pathlib import Path

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import

from adapters.kimi.session_layout import (  # noqa: E402
    AGENT_WIRE_TRANSCRIPT_FILE_NAME,
    MAIN_AGENT_IDENTIFIER,
    SESSION_STATE_FILE_NAME,
)
from core.subagents.models import SpawnRecord  # noqa: E402

MAIN_AGENT_TYPE = "main"
SUBAGENT_TYPE = "sub"
SWARM_ITEM_PROMPT_FIELD = "swarmItem"
"""AgentSwarm 派生的子代理带这个字段（内容即派生 prompt）；普通 ``Agent`` 派生的不带。"""


def read_kimi_originator(rollout_path: Path) -> str | None:  # noqa: ARG001
    """Kimi wire transcript 无 Codex 式 ``originator`` 概念，恒返回 ``None``。

    与 ``read_claude_originator`` 同形，让 facade 能按 host 统一选取 originator 提取函数。
    """
    return None


def _session_directory_of(main_rollout_path: Path) -> Path | None:
    """由主 agent 的 ``wire.jsonl`` 反推会话目录。

    布局 ``<session_dir>/agents/<agent_id>/wire.jsonl`` ⇒ ``parents[2]`` 即会话目录。
    形状不符（层级不够、或不是 ``agents/`` 下）时返回 ``None`` 而不是猜。
    """
    path = Path(main_rollout_path)
    parents = path.parents
    if len(parents) < 3:
        return None
    if parents[1].name != "agents":
        return None
    return parents[2]


def _build_spawn(agent_id: str, metadata: dict, line_index: int) -> SpawnRecord:
    prompt = metadata.get(SWARM_ITEM_PROMPT_FIELD)
    return SpawnRecord(
        call_id=None,
        agent_id=agent_id,
        role=None,
        nickname=agent_id,
        prompt=prompt if isinstance(prompt, str) and prompt.strip() else None,
        ts=None,
        line_index=line_index,
    )


def resolve_subagents(
    *,
    main_rollout_path: Path | None = None,
    original_transcript: Path | None = None,
    sessions_root: Path | None = None,  # noqa: ARG001 — Kimi 从会话名册发现，不用 sessions 根
) -> list[tuple[SpawnRecord, Path | None]]:
    """facade 统一契约：返回 ``(spawn, 子代理 transcript 路径)`` 列表。

    Kimi 路径：由主 agent 的 ``wire.jsonl`` 反推会话目录 → 读 ``state.json`` 的 ``agents``
    名册 → 跳过主 agent → 每个子 agent 取 ``homedir/wire.jsonl``。

    **跳过主 agent 用双判据**（``type == "main"`` **或** ``parentAgentId`` 为空）：两者实测
    一致，但任一字段缺失时另一个仍能挡住——把主 agent 当成自己的子代理会造成蒸馏自嵌套。

    拿不到路径 / 名册不可读 / 形状不符时返回空列表，**不抛**：子代理发现是尽力而为的观测，
    失败不该让整条 capture 链路断掉。
    """
    anchor = main_rollout_path or original_transcript
    if anchor is None:
        return []
    session_directory = _session_directory_of(Path(anchor))
    if session_directory is None:
        return []
    try:
        state = json.loads(
            (session_directory / SESSION_STATE_FILE_NAME).read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except (OSError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    agents = state.get("agents")
    if not isinstance(agents, dict):
        return []

    out: list[tuple[SpawnRecord, Path | None]] = []
    for line_index, agent_id in enumerate(sorted(agents)):
        metadata = agents[agent_id]
        if not isinstance(metadata, dict):
            continue
        if agent_id == MAIN_AGENT_IDENTIFIER:
            continue
        if metadata.get("type") == MAIN_AGENT_TYPE:
            continue
        if not metadata.get("parentAgentId"):
            continue
        home = metadata.get("homedir")
        transcript: Path | None = None
        if isinstance(home, str) and home:
            candidate = Path(home) / AGENT_WIRE_TRANSCRIPT_FILE_NAME
            if candidate.is_file():
                transcript = candidate
        out.append((_build_spawn(agent_id, metadata, line_index), transcript))
    return out
