"""in-progress 锁 marker 的文件名助手（测试专用）。

锁的 key 自 P2 起含仓身份（``task-<id>--repo-<identity>.json`` / ``sess-…``，见
``kanban_followup_lock.marker_paths``）。测试若继续硬编码 ``task-<id>.json``，就会写出一份
生产代码永远读不到的 marker——断言仍然「通过」，但测的是一个不存在的世界。

所以名字一律**从生产模块现算**：本模块只是把 ``marker_paths()`` 包成一个取文件名的糖，
不复制任何命名规则。仓身份要真解析得出来，故 ``repo`` 必须指向一个真实 git 仓/工作树
（测试里通常就是 ``init_repo(...)`` 的返回值）。

``lock_module`` 由调用方传入**它自己那一份**已加载的 ``kanban_followup_lock``（两个 aggregator
各有自己的 ``load_kanban_followup_lock_module()``），避免本模块另 import 一次造出第二个实例。
"""

from __future__ import annotations

from pathlib import Path


def in_progress_lock_marker_name(
    lock_module: object,
    *,
    repo: str | Path,
    task_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """这把锁的**首选**（=写侧目标）marker 文件名。"""
    paths = lock_module.marker_paths(  # type: ignore[attr-defined]
        task_id=task_id, session_id=session_id, repo=repo
    )
    if not paths:
        raise AssertionError(
            f"仓身份解析不出来，marker_paths 返回空：repo={repo!r}（必须是真实 git 仓/工作树）"
        )
    return paths[0].name
