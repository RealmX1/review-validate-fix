"""Claude Code host 子代理 adapter（观测侧发现 + 调用侧 argv 构造）。

**观测侧**：Claude Code 主会话通过 ``Task``/``Agent`` 工具派生子代理；每个子代理
的 transcript 被持久化为父会话同名目录下的独立文件：

    ~/.claude/projects/<proj>/<session-uuid>.jsonl          # 父会话 transcript
    ~/.claude/projects/<proj>/<session-uuid>/subagents/agent-<id>.jsonl  # 子代理

子代理文件是标准 Claude transcript record（带 ``isSidechain=true`` / ``agentId`` /
``slug``），可直接用 ``distill_claude_jsonl`` 蒸馏。本模块封装「如何由父会话
transcript 路径定位这些子代理文件、并从其首条 record 抽 spawn 元数据」这件
host 耦合的事；通用 copy/distill/manifest 骨架在 ``subagent_capture`` facade。

与 Codex 不同：Claude 子代理不在主 rollout 里以 ``spawn_agent`` 事件出现，而是
独立落盘文件——因此发现入口是**原始父 transcript 路径**（``original_transcript``）
而非捕获到 run 目录的副本；缺失时优雅返回空（例如 forked-target 场景拿不到原始
会话目录）。

**调用侧**：``build_analyze_command`` 给出 Claude headless analyze 调用的 argv
形态（``claude -p --output-format stream-json …``）。按 host 选哪个 adapter 的
分派留在 skill facade（``rvf_analyze_thread.build_analyze_command``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import

from core.subagents.models import InvokeCommand, SpawnRecord, iter_jsonl_dicts  # noqa: E402


def build_analyze_command(*, claude_bin: str) -> InvokeCommand:
    """Claude headless analyze 调用向量：``claude -p --output-format stream-json
    --verbose --permission-mode acceptEdits``，prompt 走 stdin。

    ``claude_bin`` 由调用方（skill facade）解析后传入，使本 adapter 不耦合 RVF 的
    ``CODEX_RVF_CLAUDE_BIN`` env 约定。**关键约束**：绝不追加
    ``--disable-slash-commands``——analyze agent 需解析 ``$rvf-analyze`` slash
    command 并 Edit ``summary.md`` / ``causality.json``；``acceptEdits`` 放行这些
    写入。
    """
    return InvokeCommand(
        argv=[
            claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
        ],
        uses_stdin=True,
    )


def read_claude_originator(rollout_path: Path) -> str | None:  # noqa: ARG001
    """Claude transcript 无 Codex 式 ``originator`` 概念，恒返回 None。

    保留此 helper 是为了与 ``read_codex_originator`` 对称，让 facade 能按 host
    统一选取 originator 提取函数。host 区分由 manifest 的 ``host`` 字段表达。
    """
    return None


def _subagents_dir(original_transcript: Path) -> Path:
    """由父会话 transcript 路径推出 ``<session-uuid>/subagents`` 目录。

    ``.../<proj>/<uuid>.jsonl`` → ``.../<proj>/<uuid>/subagents``。
    """
    return original_transcript.parent / original_transcript.stem / "subagents"


def _extract_prompt(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = [
            block.get("text")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    return None


def _build_spawn(path: Path, line_index: int) -> SpawnRecord:
    """从子代理文件首条 record 抽 spawn 元数据。

    ``slug`` 是 Claude 给子代理的随机名（语义上近 Codex ``nickname`` 而非
    ``role``），故映射到 ``nickname``；Claude 暂无显式 agent-type/role 字段，
    ``role`` 留 None。``call_id`` 暂为 None——父会话 ``Task`` tool_use 的 id 不在
    子代理文件内，B 因果归因依赖的是蒸馏后子代理 trajectory 内的 write-op
    call_id，而非此 spawn call_id。
    """
    agent_id = path.stem
    if agent_id.startswith("agent-"):
        agent_id = agent_id[len("agent-"):]
    slug: str | None = None
    ts: str | None = None
    prompt: str | None = None
    for _, record in iter_jsonl_dicts(path):
        agent_id = record.get("agentId") if isinstance(record.get("agentId"), str) else agent_id
        slug = record.get("slug") if isinstance(record.get("slug"), str) else slug
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else ts
        prompt = _extract_prompt(record.get("message"))
        break  # 首条 record 即子代理初始 user prompt，足以提取元数据
    return SpawnRecord(
        call_id=None,
        agent_id=agent_id,
        role=None,
        nickname=slug,
        prompt=prompt,
        ts=ts,
        line_index=line_index,
    )


def resolve_subagents(
    *,
    main_rollout_path: Path | None = None,  # noqa: ARG001 — Claude 不从主 rollout 发现
    original_transcript: Path | None = None,
    sessions_root: Path | None = None,  # noqa: ARG001 — Claude 不用 Codex sessions 布局
) -> list[tuple[SpawnRecord, Path | None]]:
    """facade 统一契约：返回 ``(spawn, 子代理 transcript 路径)`` 列表。

    Claude 路径：由 ``original_transcript`` 推出 ``<uuid>/subagents`` 目录，glob
    ``agent-*.jsonl``。拿不到原始 transcript 或目录不存在时返回空（不抛异常）。
    """
    if original_transcript is None:
        return []
    subdir = _subagents_dir(Path(original_transcript))
    if not subdir.is_dir():
        return []
    out: list[tuple[SpawnRecord, Path | None]] = []
    for index, path in enumerate(sorted(subdir.glob("agent-*.jsonl"))):
        out.append((_build_spawn(path, index), path))
    return out
