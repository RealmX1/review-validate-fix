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
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
