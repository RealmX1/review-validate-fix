#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import diff_tracker  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("issue file root must be a JSON object")
    return payload


def _issue_id(payload: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    for key in ("issue_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    raise ValueError("issue file must contain issue_id/id or --issue-id")


def _run_id(payload: dict[str, Any], override: str | None, run_dir: Path) -> str:
    if override:
        return override
    value = payload.get("run_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        summary_run_id = summary.get("run_id")
        if isinstance(summary_run_id, str) and summary_run_id.strip():
            return summary_run_id.strip()
    raise ValueError("run_id missing; pass --run-id or use a run_dir with summary.json")


def _source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("source_refs")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    refs = []
    reviewer_id = payload.get("reviewer_id")
    if isinstance(reviewer_id, str) and reviewer_id:
        refs.append({"reviewer_id": reviewer_id})
    return refs


def _write_mirror(run_dir: Path, issue_id: str, payload: dict[str, Any]) -> Path:
    path = run_dir / "artifacts" / "fix-issues" / f"{diff_tracker.safe_token(issue_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def command_upsert(args: argparse.Namespace) -> int:
    run_dir_raw = args.run_dir or os.environ.get("RVF_RUN_DIR")
    if not run_dir_raw:
        raise ValueError("--run-dir or RVF_RUN_DIR is required")
    repo_raw = args.repo or os.environ.get("RVF_REPO")
    if not repo_raw:
        raise ValueError("--repo or RVF_REPO is required")
    run_dir = Path(run_dir_raw).expanduser().resolve()
    repo = Path(repo_raw).expanduser().resolve()
    issue_file = Path(args.issue_file).expanduser().resolve()
    payload = _read_json(issue_file)
    issue_id = _issue_id(payload, args.issue_id)
    run_id = _run_id(payload, args.run_id, run_dir)
    payload = {**payload, "issue_id": issue_id, "run_id": run_id}
    mirror_path = _write_mirror(run_dir, issue_id, payload)
    result = diff_tracker.rvf_issue_upsert(
        repo=repo,
        run_id=run_id,
        issue_id=issue_id,
        payload=payload,
        artifact_path=mirror_path,
        source_refs=_source_refs(payload),
        state=args.state,
        log_root_override=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    print(json.dumps({**result, "issue_artifact_path": str(mirror_path)}, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write RVF canonical issue records into the global tracker.")
    sub = parser.add_subparsers(dest="command", required=True)
    upsert = sub.add_parser("upsert")
    upsert.add_argument("--repo", default=None)
    upsert.add_argument("--run-dir", default=None)
    upsert.add_argument("--run-id", default=None)
    upsert.add_argument("--issue-id", default=None)
    upsert.add_argument("--issue-file", required=True)
    upsert.add_argument(
        "--state",
        choices=("open", "fixed", "false_positive", "elevated", "failed", "superseded"),
        default="open",
    )
    upsert.add_argument("--log-root", default=None)
    upsert.set_defaults(func=command_upsert)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
