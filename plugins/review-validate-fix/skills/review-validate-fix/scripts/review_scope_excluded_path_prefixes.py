#!/usr/bin/env python3
"""「审查范围排除前缀」的唯一事实源：Stop 候选集与 review packet 共用同一份前缀清单。

排除前缀 = 内置的开发工具托管前缀 ∪ 仓库根的 ``.review-validate-fix-ignore`` ∪ 调用方额外传入的前缀。

以前 ``.review-validate-fix-ignore`` 只在 start 之后构建 packet 时读，Stop 发起闸铸决策点用的候选
集完全不看它；于是只有工具自动生成文件变脏时，照样会铸一枚决策点、让主 agent 花一个回合去 decline。
现在两处读同一个函数，「这个路径不该审」的结论在发起前和发起后保持一致。

匹配语义是字符串前缀（任意深度），与 ``build_review_packet.exclude_pathspecs`` 为每个前缀生成的
``:(exclude,top,glob)<prefix>*`` + ``:(exclude,top,glob)<prefix>*/**`` 两条 pathspec 之并一致：
以 ``/`` 结尾的前缀命中该目录下任意深度的路径，不以 ``/`` 结尾的前缀会连同名文件、同前缀的兄弟
路径以及这些目录下的嵌套路径一起命中。（单条 ``<prefix>*`` 不够：glob magic 下 ``*`` 不跨 ``/``。）
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME = ".review-validate-fix-ignore"

# 开发工具在会话启动时自己写进工作树、不属于任何人「本轮工作」的目录。
#
# ``.serena/``：全局 MCP 配置以 ``--project-from-cwd`` + ``alwaysLoad: true`` 启动 Serena，每个会话都会
# 在 cwd 激活 Serena 项目，于是出现两种形态的脏文件：
#   * 形态 A（``??`` 未跟踪）：新 worktree 不继承未跟踪的 ``.serena/``，首次启动生成
#     ``.serena/.gitignore`` 与 ``.serena/project.yml``；
#   * 形态 B（`` M`` 已修改）：仓库跟踪的 ``project.yml`` schema 落后于当前 Serena 版本，启动时被就地重写。
# 形态 B 靠 gitignore 类方案治不了，所以只能在 RVF 这一侧排除。取整个目录而非逐个文件：Serena 生成的
# 文件集合一直在变（``project.local.yml``、``cache/``、字段改名），``memories/`` 也只是 onboarding 笔记。
# EXPERIMENT_NODE: 其他工具目录（如 .gitnexus/）是否也并入内置前缀，待其误报频次持续出现后再定。
BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE: tuple[str, ...] = (".serena/",)


def normalize_review_scope_excluded_path_prefix(prefix: str) -> str | None:
    normalized = prefix.strip().replace("\\", "/")
    if not normalized or normalized.startswith("#"):
        return None
    is_directory_prefix = normalized.endswith("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    return f"{normalized}/" if is_directory_prefix else normalized


def load_review_scope_excluded_path_prefixes(repo: Path, extra_prefixes: Iterable[str], *, repo_local_ignore_file_text: str | None = None) -> list[str]:
    prefixes: list[str] = []
    for prefix in BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE:
        normalized = normalize_review_scope_excluded_path_prefix(prefix)
        if normalized is not None:
            prefixes.append(normalized)
    ignore_file = repo / REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME
    if repo_local_ignore_file_text is None and ignore_file.is_file():
        repo_local_ignore_file_text = ignore_file.read_text(encoding="utf-8")
    if repo_local_ignore_file_text is not None:
        for line in repo_local_ignore_file_text.splitlines():
            normalized = normalize_review_scope_excluded_path_prefix(line)
            if normalized is not None:
                prefixes.append(normalized)
    for prefix in extra_prefixes:
        normalized = normalize_review_scope_excluded_path_prefix(prefix)
        if normalized is not None:
            prefixes.append(normalized)
    return sorted(set(prefixes))


def path_matches_review_scope_excluded_path_prefix(path: str, prefixes: Iterable[str]) -> bool:
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return any(normalized_path.startswith(prefix) for prefix in prefixes)
