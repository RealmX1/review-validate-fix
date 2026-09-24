"""将给定路径清单渲染为压缩目录树；不读取文件系统、不改变范围数据。

参考 Repomix 的路径分段建树方式；仅合并单链目录，文件始终单列。
标签中的反斜线、引号、反引号、美元符、冒号、树连接线和不可打印字符采用 JSON 字符串转义（不含外层引号）。
标签开头或空白后的斜线同样转义，避免路径被识别为手动 skill 调用。
绝对路径保留前导 /，相对路径保持调用方指定的基准，不做 resolve 或规范化。
"""

from __future__ import annotations

import json
from collections.abc import Sequence


def _escape_path_tree_label(label: str) -> str:
    return "".join(
        # 标签提升到独立行或压缩目录后，UPS 的三种手动触发前缀都须保持为路径。
        f"\\u{ord(character):04x}" if (
            character in "`$:├└─│"
            or label == "(none)" and index == 0  # 真实文件名不能与空集标记相同。
            or character == "/" and (index == 0 or label[index - 1].isspace())
        ) else json.dumps(character, ensure_ascii=not character.isprintable())[1:-1]
        for index, character in enumerate(label)
    )


def render_compact_path_tree(paths: Sequence[str]) -> str:
    """输出确定性的纯文本树；空清单输出 (none)，重复路径只展示一次。"""
    directory_tree: dict = {}
    for path in paths:
        if not isinstance(path, str) or not path:
            raise ValueError("路径清单必须包含非空字符串")
        components = path.split("/")
        current_directory = directory_tree
        for index, component in enumerate(components):
            label = component + "/" if index < len(components) - 1 else component
            current_directory = current_directory.setdefault(label, {})

    if not directory_tree:
        return "(none)"

    lines: list[str] = []
    # EXPERIMENT_NODE: 用真实 agent 的路径与属性还原准确率比较连接线树和纯缩进树。
    pending_render_items = [(directory_tree, "", True)]
    while pending_render_items:
        children, indentation, is_root = pending_render_items.pop()
        if children is None:
            lines.append(indentation)
            continue
        entries = sorted(children.items(), key=lambda item: (not item[0].endswith("/"), item[0]))
        # 用栈展开，避免深目录依赖 Python 递归深度上限。
        for index in range(len(entries) - 1, -1, -1):
            label, descendants = entries[index]
            while label.endswith("/") and len(descendants) == 1:
                child_label, child_descendants = next(iter(descendants.items()))
                if not child_label.endswith("/"):
                    break
                label += child_label
                descendants = child_descendants
            last_child = index == len(entries) - 1
            connector = "" if is_root and len(entries) == 1 else ("└── " if last_child else "├── ")
            child_indentation = indentation + ("    " if last_child else "│   ")
            # 单根目录没有连接线，其子项也无需多缩进一层。
            if is_root and len(entries) == 1:
                child_indentation = ""
            if descendants:
                pending_render_items.append((descendants, child_indentation, False))
            pending_render_items.append((None, indentation + connector + _escape_path_tree_label(label), False))
    return "\n".join(lines)


def render_compact_path_tree_block(paths: Sequence[str]) -> str:
    """代码块保持连接线与缩进；文件名反引号已转义，不能闭合代码块。"""
    tree = render_compact_path_tree(paths)
    escaping_note = "路径标签采用 JSON 字符串转义（不含外层引号）。\n" if "\\" in tree else ""
    return escaping_note + "```text\n" + tree + "\n```"
