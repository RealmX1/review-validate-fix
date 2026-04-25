#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run_git(repo: Path, args: list[str], *, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        raise RuntimeError(stderr.strip() or stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(str(run_git(repo, ["rev-parse", "--show-toplevel"])).strip()).resolve()


def parse_status_z(data: bytes) -> tuple[list[str], set[str]]:
    parts = [part for part in data.split(b"\0") if part]
    entries: list[str] = []
    paths: set[str] = set()
    index = 0
    while index < len(parts):
        record = parts[index].decode("utf-8", "surrogateescape")
        entries.append(record)
        if len(record) >= 4:
            xy = record[:2]
            path = record[3:]
            paths.add(path)
            if "R" in xy or "C" in xy:
                index += 1
                if index < len(parts):
                    old_path = parts[index].decode("utf-8", "surrogateescape")
                    entries.append(old_path)
                    paths.add(old_path)
        index += 1
    return entries, paths


def file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False}
    if path.is_symlink():
        return {"exists": True, "type": "symlink", "target": os.readlink(path)}
    if not path.is_file():
        return {"exists": True, "type": "non-file"}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"exists": True, "type": "file", "size": stat.st_size, "sha256": digest.hexdigest()}


def capture(repo: Path) -> dict[str, Any]:
    root = git_root(repo)
    head = str(run_git(root, ["rev-parse", "HEAD"])).strip()
    status_data = run_git(root, ["status", "--porcelain=v1", "-z", "-uall"], text=False)
    assert isinstance(status_data, bytes)
    entries, paths = parse_status_z(status_data)
    return {
        "repo": str(root),
        "head": head,
        "status_entries": entries,
        "path_fingerprints": {path: file_fingerprint(root / path) for path in sorted(paths)},
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changed_paths = sorted(
        path
        for path in set(before.get("path_fingerprints", {})) | set(after.get("path_fingerprints", {}))
        if before.get("path_fingerprints", {}).get(path) != after.get("path_fingerprints", {}).get(path)
    )
    status_changed = before.get("status_entries") != after.get("status_entries")
    head_changed = before.get("head") != after.get("head")
    return {
        "unchanged": not status_changed and not head_changed and not changed_paths,
        "status_changed": status_changed,
        "head_changed": head_changed,
        "changed_paths": changed_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture or compare a review-time workspace snapshot.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repo", required=True)
    capture_parser.add_argument("--output", required=True)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--repo", required=True)
    compare_parser.add_argument("--before", required=True)
    compare_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    try:
        if args.command == "capture":
            snapshot = capture(Path(args.repo).expanduser().resolve())
            Path(args.output).expanduser().resolve().write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0

        before = json.loads(Path(args.before).expanduser().resolve().read_text(encoding="utf-8"))
        result = compare(before, capture(Path(args.repo).expanduser().resolve()))
    except Exception as exc:
        return fail(str(exc), 2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["unchanged"]:
        print("UNCHANGED")
    else:
        print("WORKSPACE_CHANGED")
        for path in result["changed_paths"]:
            print(path)
    return 0 if result["unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
