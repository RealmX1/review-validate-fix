#!/usr/bin/env python3
"""为一次 RVF run 计算 before/after 工作区 diff。

调用 `workspace_snapshot.compare(before, after)` 得到结构化变更，
再用 `git diff` 产出可读 patch。两个产物一起写入 run_dir 的 artifact 目录，
为后续 `/rvf-analyze` 复盘提供"这次 RVF 实际改了什么"的权威来源。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_snapshot import capture as snapshot_capture, compare as snapshot_compare

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _classify_change(
    before_fp: dict[str, Any] | None,
    after_fp: dict[str, Any] | None,
    *,
    in_head_before: bool,
) -> str:
    before_exists = bool(before_fp and before_fp.get("exists")) or in_head_before
    after_exists = bool(after_fp and after_fp.get("exists"))
    if not before_exists and after_exists:
        return "added"
    if before_exists and not after_exists:
        return "deleted"
    return "modified"


def _path_in_head(repo: Path, head: str, path: str) -> bool:
    if not head or not path:
        return False
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{head}:{path}"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _git_diff(repo: Path, head_before: str, head_after: str, paths: list[str]) -> str | None:
    if head_before == head_after and not paths:
        return None
    base_args = ["git", "-c", "core.quotepath=false", "diff", "--binary"]
    if head_before and head_after and head_before != head_after:
        range_args = [f"{head_before}..{head_after}"]
    else:
        range_args = ["HEAD"]
    cmd = [*base_args, *range_args]
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    completed = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def compute(
    *,
    run_dir: Path,
    repo: Path,
    before_path: Path,
    after_path: Path,
) -> dict[str, Any]:
    """Compute and persist workspace-diff artifacts inside <run_dir>/artifacts/.

    返回写入 workspace-diff.json 的内容（含失败时的诊断字段）。
    """
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    diff_json_path = artifacts_dir / "workspace-diff.json"
    diff_patch_path = artifacts_dir / "workspace-diff.patch"

    diagnostics: list[str] = []
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    try:
        before_snapshot = _read_snapshot(before_path)
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(f"before_snapshot_read_failed: {type(exc).__name__}: {exc}")
    try:
        after_snapshot = _read_snapshot(after_path)
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(f"after_snapshot_read_failed: {type(exc).__name__}: {exc}")

    if before_snapshot is None or after_snapshot is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "status": "incomplete",
            "diagnostics": diagnostics,
            "before_path": str(before_path),
            "after_path": str(after_path),
            "git_diff_path": None,
            "head_before": None,
            "head_after": None,
            "changed_paths": [],
        }
        diff_json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    comparison = snapshot_compare(before_snapshot, after_snapshot)
    before_fps = before_snapshot.get("path_fingerprints", {}) or {}
    after_fps = after_snapshot.get("path_fingerprints", {}) or {}
    head_before = before_snapshot.get("head") or ""
    changed_paths = []
    for path in comparison.get("changed_paths", []):
        before_fp = before_fps.get(path)
        after_fp = after_fps.get(path)
        op = _classify_change(
            before_fp,
            after_fp,
            in_head_before=_path_in_head(repo, head_before, path),
        )
        changed_paths.append(
            {
                "path": path,
                "op": op,
                "before_sha256": (before_fp or {}).get("sha256"),
                "after_sha256": (after_fp or {}).get("sha256"),
            }
        )
    head_after = after_snapshot.get("head") or ""
    # head_before defined above
    git_diff_text: str | None = None
    git_diff_path_value: str | None = None
    try:
        git_diff_text = _git_diff(
            repo,
            head_before,
            head_after,
            [item["path"] for item in changed_paths],
        )
    except OSError as exc:
        diagnostics.append(f"git_diff_failed: {type(exc).__name__}: {exc}")
    if git_diff_text:
        diff_patch_path.write_text(git_diff_text, encoding="utf-8")
        git_diff_path_value = str(diff_patch_path)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "complete",
        "diagnostics": diagnostics,
        "before_path": str(before_path),
        "after_path": str(after_path),
        "head_before": head_before,
        "head_after": head_after,
        "unchanged": comparison.get("unchanged", False),
        "status_changed": comparison.get("status_changed", False),
        "head_changed": comparison.get("head_changed", False),
        "changed_paths": changed_paths,
        "git_diff_path": git_diff_path_value,
    }
    diff_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def capture_after(repo: Path, output: Path) -> dict[str, Any]:
    snapshot = snapshot_capture(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute RVF before/after workspace diff.")
    parser.add_argument("--run-dir", required=True, help="RVF run directory containing artifacts/.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument(
        "--before",
        required=True,
        help="Path to before-workspace-snapshot.json captured at RVF start.",
    )
    parser.add_argument(
        "--after",
        help=(
            "Path to after-workspace-snapshot.json. If omitted, captures one now into "
            "<run_dir>/artifacts/after-workspace-snapshot.json."
        ),
    )
    args = parser.parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    before_path = Path(args.before).expanduser().resolve()
    if args.after:
        after_path = Path(args.after).expanduser().resolve()
    else:
        after_path = run_dir / "artifacts" / "after-workspace-snapshot.json"
        capture_after(repo, after_path)
    payload = compute(run_dir=run_dir, repo=repo, before_path=before_path, after_path=after_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
