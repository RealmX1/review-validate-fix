"""RVF import-root bootstrap（单一收口）。

自底向上从本文件所在目录寻找**同时含 ``.rvf-pyroot`` 哨兵与 ``core/`` 目录**的
最近祖先目录，并把它插入 ``sys.path[0]``，使 ``import core.*`` / ``import
adapters.*`` 在以下四种上下文下走同一条码路：

1. **repo**：哨兵 + ``core/`` 在仓库根（本文件在 ``plugins/<plugin>/skills/.../scripts/``，
   需向上跨过 plugin/skill 多层才到根）。
2. **已部署 payload**：``install_to_codex.py`` 的 ``vendor_pyroot`` 把 ``core/`` +
   ``adapters/`` + ``.rvf-pyroot`` 一并 vendor 进 payload 根（``…/review-validate-fix/``），
   此时哨兵就在 scripts 上方第 3 层。
3. **kanban worktree**：同 repo 形态（worktree 是仓库的工作树副本）。
4. **测试**：经 ``_rvf_test_support.loader`` 加载 facade 时仍由仓库根命中。

部署与 repo 两种形态下「哨兵到 scripts」的层数不同（部署近、repo 远），因此
**不能用固定 ``parents[N]`` 数层**——只能靠哨兵自底向上搜。找不到则 fail-loud，
明确指向 ``vendor_pyroot`` 未把 core/adapters/哨兵随 payload 部署。
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_pyroot(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".rvf-pyroot").is_file() and (candidate / "core").is_dir():
            return candidate
    return None


def ensure_pyroot_on_path() -> Path:
    start = Path(__file__).resolve().parent
    root = find_pyroot(start)
    if root is None:
        raise ImportError(
            "RVF pyroot 未找到：自 "
            f"{start} 起向上未发现同时含 '.rvf-pyroot' 哨兵与 'core/' 的目录。"
            "请确认 install_to_codex.py 的 vendor_pyroot 已把 core/ + adapters/ + "
            ".rvf-pyroot 随 payload 一并部署（参见 _rvf_pyroot 模块 docstring）。"
        )
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


PYROOT = ensure_pyroot_on_path()
