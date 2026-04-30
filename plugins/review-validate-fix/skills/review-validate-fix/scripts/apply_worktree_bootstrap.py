#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


# Contract surface: tracked bootstrap changes are replayed through `git apply`.
def fail(message: str, code: int = 2) -> int:
    print(message)
    return code


def run_git(repo: Path, args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
    return completed


def git_root(repo: Path) -> Path:
    return Path(run_git(repo, ["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()


def git_commit(repo: Path, ref: str) -> str:
    return run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).stdout.strip()


def read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"bootstrap metadata is not an object: {path}")
    return payload


def verify_base_ref(root: Path, metadata: dict[str, Any]) -> None:
    base_ref = metadata.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError("bootstrap metadata is missing base_ref")
    expected = git_commit(root, base_ref.strip())
    actual = git_commit(root, "HEAD")
    if actual != expected:
        raise RuntimeError(f"bootstrap base_ref mismatch: expected {expected}, current HEAD is {actual}")


def copy_untracked_files(root: Path, metadata: dict[str, Any]) -> list[str]:
    copied: list[str] = []
    files_root_value = metadata.get("files_dir")
    if not isinstance(files_root_value, str) or not files_root_value:
        return copied
    files_root = Path(files_root_value).expanduser().resolve()
    for item in metadata.get("untracked_files", []):
        if not isinstance(item, dict):
            continue
        rel = item.get("path")
        stored = item.get("stored_path")
        if not isinstance(rel, str) or not isinstance(stored, str):
            continue
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"bootstrap path escapes worktree: {rel}")
        source = Path(stored)
        if not source.is_absolute():
            source = files_root / stored
        if not source.exists():
            raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(rel)
    return copied


def apply_bootstrap(*, repo: Path, metadata_path: Path) -> dict[str, Any]:
    root = git_root(repo)
    metadata = read_manifest(metadata_path)
    verify_base_ref(root, metadata)
    patch_path_value = metadata.get("patch_file")
    patch_applied = False
    if isinstance(patch_path_value, str) and patch_path_value:
        patch_path = Path(patch_path_value).expanduser().resolve()
        patch_text = patch_path.read_text(encoding="utf-8")
        if patch_text.strip():
            check = run_git(root, ["apply", "--check", "-"], input_text=patch_text, check=False)
            if check.returncode != 0:
                raise RuntimeError(check.stderr.strip() or check.stdout.strip() or "bootstrap patch does not apply")
            run_git(root, ["apply", "-"], input_text=patch_text)
            patch_applied = True
    copied = copy_untracked_files(root, metadata)
    return {
        "ok": True,
        "repo": str(root),
        "metadata": str(metadata_path),
        "patch_applied": patch_applied,
        "copied_untracked_files": copied,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply RVF session-owned bootstrap changes inside a Cline Kanban worktree.")
    parser.add_argument("--metadata", "--bootstrap", dest="metadata", required=True)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    try:
        payload = apply_bootstrap(
            repo=Path(args.repo).expanduser().resolve(),
            metadata_path=Path(args.metadata).expanduser().resolve(),
        )
    except Exception as exc:
        return fail(f"worktree bootstrap failed: {type(exc).__name__}: {exc}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
