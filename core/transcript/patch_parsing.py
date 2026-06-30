"""host-中性的 ``apply_patch`` patch-text 解析原语。

``apply_patch`` 自定义补丁格式（``*** Begin Patch`` / ``*** Add File:`` /
``*** Update File:`` / ``*** Delete File:`` / ``@@ -a,b +c,d @@``）并非某一个
host 专属：Codex rollout 把 ``apply_patch`` 走 ``custom_tool_call`` 投递，
Claude Code 则可能在 ``Bash`` 工具里以 heredoc / 内联 stdin 形式调用同名 CLI。
两端 transcript adapter 都需要把这段补丁文本解析成 ``artifact_refs``，故这些
纯文本解析 helper 上提到 ``core.transcript``，不依赖任何 host SDK / 子进程，
只用 stdlib（``re``）。
"""

from __future__ import annotations

import re
from typing import Any


def parse_apply_patch_operations_without_repo(
    patch_text: str, line_number: int
) -> tuple[list[dict[str, Any]], set[str]]:
    """Repo-less apply_patch parser；与 ``session_change_manifest.parse_apply_patch``
    返回 shape 一致，但跳过 path 归一化（无 repo 上下文时的 fallback）。
    识别 ``*** Add File: ...`` / ``*** Delete File: ...`` / ``*** Update File: ...``。
    """
    operations: list[dict[str, Any]] = []
    paths: set[str] = set()
    for raw_line in patch_text.splitlines():
        for prefix, op in (
            ("*** Add File: ", "add"),
            ("*** Delete File: ", "delete"),
            ("*** Update File: ", "update"),
        ):
            if raw_line.startswith(prefix):
                rel = raw_line.removeprefix(prefix).strip()
                if rel:
                    operations.append({"operation": op, "path": rel, "line_number": line_number})
                    paths.add(rel)
                break
    return operations, paths


def apply_patch_hunk_line_range_for_path(patch_text: str, path: str | None) -> list[int]:
    """从 apply_patch 文本中提取该 path 下首段 hunk 的 @@ -X,Y +A,B @@ 中的 A 与 A+B-1。"""
    if not path:
        return []
    in_path = False
    for raw_line in patch_text.splitlines():
        if raw_line.startswith("*** ") and "File:" in raw_line:
            in_path = raw_line.endswith(": " + path) or raw_line.endswith(":" + path) or raw_line.endswith(path)
            continue
        if not in_path:
            continue
        if raw_line.startswith("@@"):
            try:
                # @@ -a,b +c,d @@
                parts = raw_line.split(" ")
                plus = next(p for p in parts if p.startswith("+"))
                plus = plus.lstrip("+")
                if "," in plus:
                    start_str, length_str = plus.split(",", 1)
                    start = int(start_str)
                    length = int(length_str)
                    return [start, start + max(length, 1) - 1]
                return [int(plus), int(plus)]
            except (StopIteration, ValueError):
                continue
    return []


def apply_patch_operation_to_artifact_verb(operation: str | None) -> str:
    if operation == "add":
        return "create"
    if operation == "delete":
        return "delete"
    return "edit"


_BASH_HEREDOC_RE = re.compile(
    r"<<\s*['\"]?(?P<token>[A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\n(?P<body>.*?)\n(?P=token)\s*$",
    re.DOTALL | re.MULTILINE,
)


def extract_apply_patch_text_from_bash_command(command: str) -> str | None:
    """从 Bash ``apply_patch`` 调用中抽出 patch 文本。

    支持两种形式：
    - heredoc: ``apply_patch <<'EOF'\n*** Begin Patch...\nEOF``
    - 内联 stdin: ``apply_patch '*** Begin Patch...\n*** End Patch'``
    无 ``apply_patch`` 关键字 → 返回 None。
    """
    if "apply_patch" not in command:
        return None
    match = _BASH_HEREDOC_RE.search(command)
    if match:
        body = match.group("body")
        if "*** Begin Patch" in body or "*** Add File:" in body or "*** Update File:" in body or "*** Delete File:" in body:
            return body
    if "*** Begin Patch" in command:
        start = command.find("*** Begin Patch")
        end = command.rfind("*** End Patch")
        if end > start:
            return command[start:end + len("*** End Patch")]
    return None
