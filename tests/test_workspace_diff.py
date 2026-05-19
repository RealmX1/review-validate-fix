#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
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


from _rvf_test_support.loader import load_script_module as _load


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _init_repo_with_commit(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf-test@example.com")
    _git(path, "config", "user.name", "RVF Tester")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_workspace_diff_records_added_and_modified(tmp_path: Path) -> None:
    snapshot = _load("workspace_snapshot")
    workspace_diff = _load("workspace_diff")
    repo = _init_repo_with_commit(tmp_path / "repo")
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    before_path = artifacts / "before-workspace-snapshot.json"
    before_path.write_text(
        json.dumps(snapshot.capture(repo), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # mutate the workspace
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    (repo / "new_file.txt").write_text("brand new\n", encoding="utf-8")
    after_path = artifacts / "after-workspace-snapshot.json"
    workspace_diff.capture_after(repo, after_path)
    payload = workspace_diff.compute(
        run_dir=run_dir,
        repo=repo,
        before_path=before_path,
        after_path=after_path,
    )
    assert payload["status"] == "complete"
    paths = {item["path"]: item["op"] for item in payload["changed_paths"]}
    assert paths.get("README.md") == "modified"
    assert paths.get("new_file.txt") == "added"
    # patch should be written for modified path (HEAD vs HEAD diff is empty so will only show working tree changes via HEAD diff)
    # head_before == head_after, so git diff against working tree might be omitted; only assert json consistency
    assert payload["head_before"] == payload["head_after"]


def test_workspace_diff_handles_missing_snapshot(tmp_path: Path) -> None:
    workspace_diff = _load("workspace_diff")
    repo = _init_repo_with_commit(tmp_path / "repo")
    run_dir = tmp_path / "run"
    (run_dir / "artifacts").mkdir(parents=True)
    payload = workspace_diff.compute(
        run_dir=run_dir,
        repo=repo,
        before_path=run_dir / "artifacts" / "missing-before.json",
        after_path=run_dir / "artifacts" / "missing-after.json",
    )
    assert payload["status"] == "incomplete"
    assert payload["diagnostics"]
