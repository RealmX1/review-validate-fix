"""独立解读展示格式，供回归断言检查完整路径身份；不复用生产建树逻辑。"""

import json
import re


def restored_tree_paths(tree: str) -> list[str]:
    directory_prefixes: dict[int, str] = {-1: ""}
    paths = []
    for line in tree.splitlines():
        if line == "(none)":
            continue
        branch_offset = max(line.find("├── "), line.find("└── "))
        depth = branch_offset // 4 if branch_offset >= 0 else 0
        label = line[branch_offset + 4:] if branch_offset >= 0 else line
        label = json.loads('"' + label + '"')
        if branch_offset < 0 and label.endswith("/"):
            directory_prefixes[-1] = label
            continue
        path = directory_prefixes[depth - 1] + label
        if label.endswith("/"):
            directory_prefixes[depth] = path
        else:
            paths.append(path)
    return paths


def paths_from_compact_path_tree_blocks(text: str) -> list[str]:
    return [
        path
        for tree in re.findall(r"^```text\n(.*?)\n```$", text, flags=re.MULTILINE | re.DOTALL)
        for path in restored_tree_paths(tree)
    ]
