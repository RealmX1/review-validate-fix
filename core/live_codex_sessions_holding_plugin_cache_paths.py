"""找出「正在运行、且其 hook/skill 注册指向某条即将被删的 plugin cache 路径」的 Codex 会话。

## 为什么需要它，以及为什么 ``lsof`` 判据是结构性失明的

Codex 在**会话启动时**把插件注册快照进内存。此后 ``codex plugin remove`` 把
``~/.codex/plugins/cache/<marketplace>/<plugin>/`` 整棵 payload 树删掉（这是该命令的隐藏
副作用），已在运行的会话仍会按快照去 spawn 那条绝对路径。

2026-08-18 的事故里，动手前用的门禁是「``lsof`` 对目标路径零命中 +
``~/.codex/hooks.json`` 不引用该路径」。**两条都查错了地方**：

- Codex 的 Stop hook 是**每次 Stop 事件才 fork/exec 一个短命子进程**，跑完即退。会话在两次
  Stop 之间的空闲期没有任何打开的文件句柄，``lsof +D`` 必然零命中——它测的是「此刻有无打开
  的 fd」，而危险持有的是「启动时快照进内存的一条路径字符串」，在 VFS 层不可见。
- legacy hook 走的是 **plugin bundled ``hooks/hooks.json`` + ``config.toml`` 的
  ``[hooks.state]``**，根本不经过 ``~/.codex/hooks.json``（实测引用计数 = 0）。

当晚 ``lsof`` 报 0/0 放行的**同一屏输出**里，rollout 扫描已经报「活会话引用 legacy ×2」。
本模块就是把那条当时没被采信的判据固化下来。

## 判据与它的已知边界

会话起始的 prelude 记录里有一张 Codex 自己写的 **Skill roots 表**
（``- `r3` = `/Users/…/.codex/plugins/cache/<marketplace>/<plugin>/…/skills` ``）。
只扫 prelude 而不是整份 rollout 是刻意的：rollout 后半段常常包含 agent 自己 ``cat`` /
``grep`` 出来的同名路径，**关键词命中 ≠ 该会话真的注册了它**。prelude 由 Codex 在任何
agent 输出之前写入，是结构性证据。

两条必须明说的边界，也是本检测只配当**告警**而非硬闸的原因：

1. **liveness 用 rollout 文件 mtime 近似。** 空闲超过窗口的活会话会被漏报（假阴性）。
   进程侧没有 rollout ↔ pid 的映射可用。
2. **只对「带 skills 的插件」有效。** 一个只注册 hook、不带 skill 的插件不会出现在 Skill
   roots 表里，因而不可见。RVF 自带 skills，故对 RVF 有效。

此外硬闸本身也不可行：Kanban 会在 codex 会话进程死亡后**立刻重启**新会话，
「等所有 codex 退出」这个条件不可达，硬闸会变成永远过不去的门。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_RECENT_ACTIVITY_WINDOW_SECONDS = 3600
"""rollout 文件 mtime 在此窗口内即视为「可能仍在运行」。"""

DEFAULT_PRELUDE_RECORD_LIMIT = 12
"""只扫每份 rollout 的前若干条记录——Skill roots 表落在 prelude 里。"""

_SKILL_ROOT_LINE_PATTERN = re.compile(r"`r\d+`\s*=\s*`(/[^`]+)`")


@dataclass(frozen=True)
class LiveCodexSessionHoldingPluginCachePaths:
    """一个可能仍在运行、且注册了某条即将失效 plugin cache 路径的 Codex 会话。"""

    rollout_path: Path
    session_id: str
    last_activity_epoch_seconds: float
    held_plugin_cache_paths: tuple[str, ...]


def codex_plugin_cache_path_prefix(
    *, codex_home: Path, marketplace_name: str, plugin_name: str
) -> str:
    """``codex plugin remove <plugin>@<marketplace>`` 会删掉的那棵树的路径前缀。"""
    return f"{codex_home}/plugins/cache/{marketplace_name}/{plugin_name}/"


def plugin_cache_path_prefixes_for_selectors(
    *, codex_home: Path, plugin_selectors: Iterable[str]
) -> tuple[str, ...]:
    """把 ``<plugin>@<marketplace>`` selector 列表翻成 plugin cache 路径前缀列表。"""
    prefixes: list[str] = []
    for selector in plugin_selectors:
        if "@" not in selector:
            continue
        plugin_name, _, marketplace_name = selector.partition("@")
        if not plugin_name or not marketplace_name:
            continue
        prefixes.append(
            codex_plugin_cache_path_prefix(
                codex_home=codex_home,
                marketplace_name=marketplace_name,
                plugin_name=plugin_name,
            )
        )
    return tuple(dict.fromkeys(prefixes))


def _skill_root_is_inside_plugin_cache_prefix(
    skill_root_path: str, plugin_cache_path_prefix_text: str
) -> bool:
    """按**路径分量边界**判断 skill root 是否落在某条即将被删的 plugin cache 树里。

    不能直接对 ``os.path.normpath`` 的结果做 ``startswith``：``normpath`` 会把前缀末尾的
    分隔符去掉，于是同一 marketplace 下 ``review-validate-fix-dashboard/…`` 这类**相似但
    不同**的插件路径会被判成 ``review-validate-fix/`` 的子路径，产生假的 live-session 告警。
    这条告警是给开发者看的判据，误报会直接稀释它的可信度，所以宁可显式补回分隔符再比。

    前缀带不带末尾分隔符都接受（``codex_plugin_cache_path_prefix`` 生成的形态是带的，
    但调用方也可能手写不带的），且 skill root 恰好等于前缀目录本身时算命中——那棵树整个
    会被 ``codex plugin remove`` 删掉。
    """
    normalized_skill_root = os.path.normpath(skill_root_path)
    normalized_prefix = os.path.normpath(plugin_cache_path_prefix_text)
    if normalized_skill_root == normalized_prefix:
        return True
    prefix_with_component_boundary = normalized_prefix.rstrip(os.sep) + os.sep
    return normalized_skill_root.startswith(prefix_with_component_boundary)


def _session_identifier_from_rollout_path(rollout_path: Path) -> str:
    stem = rollout_path.stem
    prefix = "rollout-"
    return stem[len(prefix) :] if stem.startswith(prefix) else stem


def _skill_root_paths_in_prelude(
    rollout_path: Path, *, prelude_record_limit: int
) -> tuple[str, ...]:
    roots: list[str] = []
    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= prelude_record_limit:
                    break
                roots.extend(_SKILL_ROOT_LINE_PATTERN.findall(line))
    except OSError:  # 读不动的 rollout 按「无证据」处理，绝不阻断部署
        return ()
    return tuple(dict.fromkeys(roots))


def live_codex_sessions_holding_plugin_cache_paths(
    *,
    codex_home: Path,
    plugin_cache_path_prefixes: Sequence[str],
    recent_activity_window_seconds: int = DEFAULT_RECENT_ACTIVITY_WINDOW_SECONDS,
    now_epoch_seconds: float | None = None,
    prelude_record_limit: int = DEFAULT_PRELUDE_RECORD_LIMIT,
) -> tuple[LiveCodexSessionHoldingPluginCachePaths, ...]:
    """列出可能仍在运行、且持有 ``plugin_cache_path_prefixes`` 下路径的 Codex 会话。

    返回空元组既可能是「确实没有」，也可能是「本判据看不见」（见模块 docstring 的两条
    边界）。**调用方必须把空结果当作「未发现」而不是「已确认安全」。**
    """
    if not plugin_cache_path_prefixes:
        return ()
    sessions_root = Path(codex_home) / "sessions"
    if not sessions_root.is_dir():
        return ()
    now = time.time() if now_epoch_seconds is None else now_epoch_seconds
    oldest_accepted = now - recent_activity_window_seconds

    findings: list[LiveCodexSessionHoldingPluginCachePaths] = []
    for rollout_path in sessions_root.rglob("rollout-*.jsonl"):
        try:
            modified_at = rollout_path.stat().st_mtime
        except OSError:
            continue
        if modified_at < oldest_accepted:
            continue
        held = tuple(
            root
            for root in _skill_root_paths_in_prelude(
                rollout_path, prelude_record_limit=prelude_record_limit
            )
            if any(
                _skill_root_is_inside_plugin_cache_prefix(root, prefix)
                for prefix in plugin_cache_path_prefixes
            )
        )
        if not held:
            continue
        findings.append(
            LiveCodexSessionHoldingPluginCachePaths(
                rollout_path=rollout_path,
                session_id=_session_identifier_from_rollout_path(rollout_path),
                last_activity_epoch_seconds=modified_at,
                held_plugin_cache_paths=held,
            )
        )
    return tuple(sorted(findings, key=lambda finding: str(finding.rollout_path)))


def live_codex_session_warning_lines(
    findings: Sequence[LiveCodexSessionHoldingPluginCachePaths],
) -> tuple[str, ...]:
    """把检测结果渲染成部署日志里的 developer-only 告警行。"""
    if not findings:
        return ()
    lines = [
        "⚠️ 检测到可能仍在运行、且注册了即将被删除的 plugin cache 路径的 Codex 会话：",
        "   它们持有的是**启动时的注册快照**；删除后这些会话每次 Stop 都会 spawn 一个不存在的",
        "   脚本。本次部署已给 hook 命令串装上 spawn 守卫，**但守卫只对本次部署之后新起的会话生效**",
        "   ——下面这些会话仍持有旧的、无守卫的命令串。建议先结束它们再部署。",
    ]
    for finding in findings:
        lines.append(f"   - session {finding.session_id}  ({finding.rollout_path})")
        for held in finding.held_plugin_cache_paths:
            lines.append(f"       持有: {held}")
    return tuple(lines)
