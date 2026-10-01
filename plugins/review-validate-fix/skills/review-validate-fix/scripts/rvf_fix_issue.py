#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
import rvf_merge_dispositions  # noqa: E402
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402


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


def _write_mirror(run_dir: Path, issue_id: str, payload: dict[str, Any]) -> Path:
    """写给 fixer 读的 ``artifacts/fix-issues/<id>.json``：一律去掉来源字段（source-agnostic 隔离）。

    来源只留在 tracker 的 ``source_refs`` 列和 ``artifacts/merge/merge_dispositions.json`` 里。"""
    path = rvf_merge_dispositions.fix_issue_mirror_path(run_dir, issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    mirror = rvf_merge_dispositions.strip_source_attribution_keys_from_fix_issue_payload(payload)
    path.write_text(json.dumps(mirror, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def upsert_canonical_issue(
    *,
    run_dir: Path,
    repo: Path,
    issue_file: Path,
    issue_id_override: str | None = None,
    run_id_override: str | None = None,
    state: str = "open",
    log_root: Path | None = None,
) -> dict[str, Any]:
    """把一份 canonical issue JSON 镜像进 run 工件并写入全局 tracker ledger。

    强制点（CLI upsert 与 ``rvf fix prepare`` 共用）：``artifacts/merge/merge_dispositions.json`` 必须
    结构合法、未过期，且覆盖这个 ``issue_id``（adopted / merged_into 目标或非 reviewer 来源），否则拒绝。
    ``source_refs`` 只从处置工件派生，不信任 payload；payload 里的来源字段被剥掉，剩余正文不得提到
    本轮 reviewer 的来源标签。"""
    raw_payload = _read_json(issue_file)
    issue_id = _issue_id(raw_payload, issue_id_override)
    run_id = _run_id(raw_payload, run_id_override, run_dir)
    # 处置按 run_dir 校验放行，记录就必须落在同一个 run：payload / --run-id 指向别的 run 会把
    # issue 写进另一个 run 的 ledger，当前 run 的因果与最终状态随之缺失。
    run_directory_run_id = rvf_merge_dispositions.run_id_for_run_directory(run_dir)
    if run_id != run_directory_run_id:
        raise ValueError(
            f"run_id {run_id!r} (from issue payload or --run-id) does not match the run directory's own run_id "
            f"{run_directory_run_id!r}; merge dispositions are checked against {run_dir}, so the issue must be "
            "upserted into that run"
        )
    source_refs, source_markers = rvf_merge_dispositions.resolve_fix_issue_source_refs_for_upsert(run_dir, issue_id)
    payload = {
        **rvf_merge_dispositions.strip_source_attribution_keys_from_fix_issue_payload(raw_payload),
        "issue_id": issue_id,
        "run_id": run_id,
    }
    leaks = rvf_merge_dispositions.fix_issue_payload_source_attribution_errors(
        payload, source_markers=source_markers, label=f"canonical issue {issue_id}"
    )
    if leaks:
        raise rvf_merge_dispositions.MergeDispositionsCheckFailed(leaks)
    mirror_path = _write_mirror(run_dir, issue_id, payload)
    result = reviewable_unit_diff_tracker.rvf_issue_upsert(
        repo=repo,
        run_id=run_id,
        issue_id=issue_id,
        payload=payload,
        artifact_path=mirror_path,
        source_refs=source_refs,
        state=state,
        log_root_override=log_root,
    )
    rvf_merge_dispositions.record_fix_issue_source_refs_written_to_tracker_at_upsert(run_dir, issue_id, source_refs)
    return {**result, "issue_id": issue_id, "issue_artifact_path": str(mirror_path)}


def command_upsert(args: argparse.Namespace) -> int:
    run_dir_raw = args.run_dir or os.environ.get("RVF_RUN_DIR")
    if not run_dir_raw:
        raise ValueError("--run-dir or RVF_RUN_DIR is required")
    repo_raw = args.repo or os.environ.get("RVF_REPO")
    if not repo_raw:
        raise ValueError("--repo or RVF_REPO is required")
    result = upsert_canonical_issue(
        run_dir=Path(run_dir_raw).expanduser().resolve(),
        repo=Path(repo_raw).expanduser().resolve(),
        issue_file=Path(args.issue_file).expanduser().resolve(),
        issue_id_override=args.issue_id,
        run_id_override=args.run_id,
        state=args.state,
        log_root=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
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
