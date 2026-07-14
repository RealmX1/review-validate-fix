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


def synthesize_apply_patch_text_from_patch_apply_end(
    payload: dict[str, Any],
) -> str | None:
    """把一条 **成功的** ``patch_apply_end`` event payload 合成为 ``apply_patch``
    自定义补丁文本，使既有 ``parse_apply_patch_details`` /
    ``parse_apply_patch_operations_without_repo`` 能对新旧两代 Codex schema 复用同一套
    解析，而非各写一份、再次一起过时（去-codex risk #7 的结构性隐患）。

    ``patch_apply_end`` 是 gpt-5.6-sol 世代 rollout 里唯一跨版本稳定、结构化、带
    ``success`` 标志的编辑真相源：其 ``changes`` 为 ``{abs_path: {type, unified_diff |
    content, move_path}}``。``update`` 用 ``unified_diff``（已含 ``@@`` 与 ``+``/``-``
    行）；``add`` / ``delete`` 只带整文件 ``content``，此处逐行加 ``+`` / ``-`` 前缀合成
    hunk 变更，使其与旧 apply_patch 自定义格式的 mutation 行等价。

    仅处理 ``payload.success is True``；``success`` 非真、``changes`` 缺失/非 dict/空
    → 返回 ``None``（调用方据此判定该记录无归属贡献）。
    """
    if payload.get("success") is not True:
        return None
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        return None
    lines: list[str] = ["*** Begin Patch"]
    emitted = False
    for path, change in changes.items():
        if not isinstance(path, str) or not path or not isinstance(change, dict):
            continue
        change_type = change.get("type")
        if change_type == "update":
            lines.append(f"*** Update File: {path}")
            # `*** Move to: <target>` 必须紧挨 `*** Update File: <source>`、写在 hunk 之前：
            # parse_apply_patch_details 是按行状态机，遇到 `*** Move to:` 才把后续 mutation
            # hunk 的归属路径切到 target。若把它放在 unified_diff 之后（旧写法），mutation
            # hunk 会先被归到 source；对未暂存的 rename+edit（change 同时带 move_path 与
            # unified_diff），source 已被 rename 走、不在 dirty set → live-unit 整段跳过，
            # source 删除与 target 文件都落入 unattributed_dirty，Stop hook 误跳过（RVF-001 F2）。
            move_path = change.get("move_path")
            if isinstance(move_path, str) and move_path:
                lines.append(f"*** Move to: {move_path}")
            unified_diff = change.get("unified_diff")
            if isinstance(unified_diff, str) and unified_diff:
                lines.extend(unified_diff.splitlines())
            emitted = True
        elif change_type == "add":
            lines.append(f"*** Add File: {path}")
            lines.append("@@")
            content = change.get("content")
            if isinstance(content, str) and content:
                lines.extend("+" + content_line for content_line in content.splitlines())
            emitted = True
        elif change_type == "delete":
            lines.append(f"*** Delete File: {path}")
            lines.append("@@")
            content = change.get("content")
            if isinstance(content, str) and content:
                lines.extend("-" + content_line for content_line in content.splitlines())
            emitted = True
    if not emitted:
        return None
    lines.append("*** End Patch")
    return "\n".join(lines)


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
