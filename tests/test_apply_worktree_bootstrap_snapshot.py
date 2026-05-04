#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf@example.com")
    _git(path, "config", "user.name", "RVF")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_apply_bootstrap_writes_before_snapshot(tmp_path: Path) -> None:
    apply_module = _load("apply_worktree_bootstrap")
    # source repo with uncommitted edits
    source = _init_repo(tmp_path / "source")
    (source / "README.md").write_text("base\nedit\n", encoding="utf-8")
    (source / "untracked.txt").write_text("only-in-source\n", encoding="utf-8")
    base_ref = _git(source, "rev-parse", "HEAD").stdout.strip()
    patch_text = _git(source, "diff", "HEAD").stdout

    # destination repo (kanban worktree clone): same HEAD, no edits yet
    dest = _init_repo(tmp_path / "dest")
    # ensure dest is at the same commit as source (re-init makes a different commit; replay README)
    (dest / "README.md").write_text("base\n", encoding="utf-8")
    _git(dest, "add", "README.md")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "align")
    # Reset dest to source HEAD so verify_base_ref passes
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    bundle = bundle_dir / "src.bundle"
    _git(source, "bundle", "create", str(bundle), "HEAD")
    _git(dest, "fetch", str(bundle), "HEAD")
    _git(dest, "reset", "--hard", "FETCH_HEAD")

    files_dir = tmp_path / "untracked-store"
    files_dir.mkdir()
    shutil.copyfile(source / "untracked.txt", files_dir / "untracked.txt")

    patch_path = tmp_path / "bootstrap.patch"
    patch_path.write_text(patch_text, encoding="utf-8")

    snapshot_target = tmp_path / "run" / "artifacts" / "before-workspace-snapshot.json"
    metadata = {
        "base_ref": base_ref,
        "patch_file": str(patch_path),
        "files_dir": str(files_dir),
        "untracked_files": [{"path": "untracked.txt", "stored_path": "untracked.txt"}],
        "before_snapshot_path": str(snapshot_target),
    }
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    payload = apply_module.apply_bootstrap(repo=dest, metadata_path=metadata_path)
    assert payload["patch_applied"] is True
    assert payload["copied_untracked_files"] == ["untracked.txt"]
    assert payload["before_snapshot"]["captured"] is True
    assert snapshot_target.exists()
    snapshot = json.loads(snapshot_target.read_text(encoding="utf-8"))
    fps = snapshot.get("path_fingerprints", {})
    # README.md was modified -> appears in dirty path fingerprints
    assert "README.md" in fps
    # untracked.txt was copied -> also dirty
    assert "untracked.txt" in fps
    # sha256 of untracked.txt should match the source contents
    assert fps["untracked.txt"]["sha256"]


def test_apply_bootstrap_skips_snapshot_without_target(tmp_path: Path) -> None:
    apply_module = _load("apply_worktree_bootstrap")
    repo = _init_repo(tmp_path / "repo")
    base_ref = _git(repo, "rev-parse", "HEAD").stdout.strip()
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps({"base_ref": base_ref}), encoding="utf-8")
    payload = apply_module.apply_bootstrap(repo=repo, metadata_path=metadata_path)
    assert payload["before_snapshot"]["captured"] is False
    assert payload["before_snapshot"]["reason"] == "no_target_path"
