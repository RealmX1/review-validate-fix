"""Canonical script-module loader.

Replaces the byte-identical ``_load`` helper duplicated across the
main-less / pytest test files: import a plugin runtime script by module
name from the skill ``scripts/`` directory, with ``sys.modules`` caching.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def load_script_module(name: str):
    if name in sys.modules:
        return sys.modules[name]
    script_path = SCRIPT_DIR / f"{name}.py"
    if not script_path.exists():
        # 已迁入 ``core/`` 的模块（去-codex 重构 S10+）：按 canonical 包路径 import，
        # 与运行时共用同一 ``sys.modules`` 实例，避免 spec-load 造双实例 / 双 SQLite 连接。
        # ``core/`` 整目录随 payload vendored，dotted 名稳定；rglob 自底向上免固定层数。
        core_matches = sorted((ROOT / "core").rglob(f"{name}.py"))
        if core_matches:
            dotted = ".".join(core_matches[0].relative_to(ROOT).with_suffix("").parts)
            if dotted in sys.modules:
                return sys.modules[dotted]
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            return importlib.import_module(dotted)
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
