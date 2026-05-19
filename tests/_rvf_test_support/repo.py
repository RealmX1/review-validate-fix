"""Repo-builder template primitive.

The suite has ~8 distinct git-repo shapes; the expensive ones run the
full ``git init`` + config + add + commit chain (5 subprocesses) once
per test. ``templated_repo`` wraps any such builder so the git work runs
exactly once per process; every later call is a filesystem ``copytree``
of that template. The wrapped builder keeps its exact original recipe,
so repo state (history, identity, dirty/untracked files) is unchanged.

A plain ``git init`` repo stores no absolute worktree path in
``.git/config``, so a copied template is relocatable.
"""

from __future__ import annotations

import atexit
import functools
import shutil
import tempfile
from pathlib import Path
from typing import Callable


def templated_repo(builder: Callable[[Path], Path]) -> Callable[[Path], Path]:
    cache: dict[str, Path] = {}

    @functools.wraps(builder)
    def wrapper(path: Path) -> Path:
        template = cache.get("path")
        if template is None:
            base = Path(tempfile.mkdtemp(prefix="rvf-repo-tmpl-"))
            atexit.register(shutil.rmtree, base, ignore_errors=True)
            template = base / "repo"
            builder(template)  # original git sequence, exactly once
            cache["path"] = template
        # copytree runs os.makedirs(path) (creates parents, errors if
        # path exists) — same contract as the builders' path.mkdir.
        shutil.copytree(template, path, symlinks=True)
        return path

    return wrapper
