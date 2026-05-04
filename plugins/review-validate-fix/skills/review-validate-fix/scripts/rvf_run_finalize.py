#!/usr/bin/env python3
"""RVF run 终态统一入口（幂等）。

stop hook 检测到 RVF run 终结（典型路径：handoff_completion_payload 返回非 None）后，
调用 finalize_run() 一次：
  1) trajectory_capture.capture_run -> 写 pre/post rollouts + 蒸馏 + reviewer 子轨迹
  2) 拍 after-workspace-snapshot.json + workspace_diff.compute -> workspace-diff.{json,patch}
  3) 在该 run 的 summary.json 上追加 finalize 字段（不动 status / latest pointer）

幂等：通过 <run_dir>/artifacts/.finalize.lock 文件保护，重复调用直接返回缓存的结果。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trajectory_capture import capture_run  # noqa: E402
from workspace_diff import capture_after, compute as compute_workspace_diff  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and (path / "summary.json").is_file()


def resolve_run_dir(*, handoff_path: Path | None, event: dict[str, Any] | None) -> Path | None:
    """从 handoff path / event 中反查 actual RVF run_dir。

    optimistic 顺序:
      1. handoff_path 的 ../.. (即 <run_dir>/artifacts/handoff.md → <run_dir>)
      2. event['rvf_run_dir'] / event['CODEX_RVF_RUN_DIR'] (若 caller 显式传入)
    返回 None 表示无法定位。

    历史上这里还有一个 ``os.environ.get('CODEX_RVF_RUN_DIR')`` fallback，但
    reviewer 子进程及任何继承父 RVF run 环境的 process 都会带着这条 env，
    一旦它指向某个旧 run_dir，finalize 会把 trajectory / lock / workspace-diff
    写到错误的 run。Caller 若真的想用 env 驱动 targeting，应在调用前把值塞进
    ``event['rvf_run_dir']`` 自行表达意图，而不是依赖隐式继承。
    """
    if handoff_path is not None:
        candidate = handoff_path.expanduser().resolve().parent.parent
        if _is_run_dir(candidate):
            return candidate
    if event:
        for key in ("rvf_run_dir", "CODEX_RVF_RUN_DIR"):
            value = event.get(key)
            if isinstance(value, str) and value:
                candidate = Path(value).expanduser().resolve()
                if _is_run_dir(candidate):
                    return candidate
    return None


def _read_summary(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _resolve_repo(run_dir: Path, summary: dict[str, Any], event: dict[str, Any] | None) -> Path | None:
    for source in (
        summary.get("repo"),
        (event or {}).get("cwd") if event else None,
    ):
        if isinstance(source, str) and source:
            candidate = Path(source).expanduser().resolve()
            if (candidate / ".git").exists() or (candidate.parent / ".git").exists():
                return candidate
            if candidate.exists():
                return candidate
    return None


def finalize_run(
    *,
    run_dir: Path,
    event: dict[str, Any] | None = None,
    decision_kind: str = "handoff",
) -> dict[str, Any]:
    """对指定 run_dir 执行一次 finalize。已经 finalize 过则直接返回 lock 内的缓存结果。"""
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    lock_path = artifacts_dir / ".finalize.lock"

    if lock_path.exists():
        try:
            cached = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                cached.setdefault("already_finalized", True)
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    summary = _read_summary(run_dir / "summary.json")
    repo = _resolve_repo(run_dir, summary, event)

    finalize_record: dict[str, Any] = {
        "schema_version": 1,
        "decision_kind": decision_kind,
        "started_at": _utc_now(),
        "run_id": summary.get("run_id"),
        "run_dir": str(run_dir),
        "repo": str(repo) if repo else None,
        "trajectory": None,
        "workspace_diff": None,
        "errors": [],
    }

    try:
        traj_summary = capture_run(run_dir=run_dir, event=event or {}, repo=repo)
        finalize_record["trajectory"] = {
            "host": traj_summary.get("host"),
            "host_originator": traj_summary.get("host_originator"),
            "trajectory_dir": traj_summary.get("trajectory_dir"),
            "pre_rvf_source_kind": traj_summary.get("pre_rvf_source_kind"),
            "post_rvf_source_kind": traj_summary.get("post_rvf_source_kind"),
            "distill_index": traj_summary.get("distill_index"),
            "reviewers": [item.get("reviewer_id") for item in traj_summary.get("reviewers", [])],
        }
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "trajectory_capture",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    before_path = artifacts_dir / "before-workspace-snapshot.json"
    after_path = artifacts_dir / "after-workspace-snapshot.json"
    if repo is not None and before_path.exists():
        try:
            capture_after(repo, after_path)
            diff_payload = compute_workspace_diff(
                run_dir=run_dir,
                repo=repo,
                before_path=before_path,
                after_path=after_path,
            )
            finalize_record["workspace_diff"] = {
                "status": diff_payload.get("status"),
                "head_before": diff_payload.get("head_before"),
                "head_after": diff_payload.get("head_after"),
                "changed_path_count": len(diff_payload.get("changed_paths", [])),
                "git_diff_path": diff_payload.get("git_diff_path"),
            }
        except Exception as exc:
            finalize_record["errors"].append(
                {
                    "stage": "workspace_diff",
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(),
                }
            )
    else:
        finalize_record["workspace_diff"] = {
            "status": "skipped",
            "reason": (
                "missing_before_snapshot" if not before_path.exists() else "missing_repo"
            ),
        }

    finalize_record["completed_at"] = _utc_now()

    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        merged = _read_summary(summary_path)
        merged["finalize"] = finalize_record
        merged["finalize_completed_at"] = finalize_record["completed_at"]
        try:
            _atomic_write_json(summary_path, merged)
        except OSError as exc:
            finalize_record["errors"].append(
                {"stage": "summary_merge", "error": f"{type(exc).__name__}: {exc}"}
            )

    try:
        lock_path.write_text(
            json.dumps(finalize_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        finalize_record["errors"].append(
            {"stage": "lock_write", "error": f"{type(exc).__name__}: {exc}"}
        )

    return finalize_record


def finalize_for_handoff(
    *,
    handoff_path: Path | None,
    event: dict[str, Any] | None,
    decision_kind: str = "handoff",
) -> dict[str, Any] | None:
    """便捷入口：从 handoff_path / event 反查 run_dir 后 finalize。
    返回 None 表示无法定位 run_dir（finalize 跳过）。
    """
    run_dir = resolve_run_dir(handoff_path=handoff_path, event=event)
    if run_dir is None:
        return None
    return finalize_run(run_dir=run_dir, event=event, decision_kind=decision_kind)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run RVF finalize hook for a given run.")
    parser.add_argument("--run-dir", help="Explicit RVF run directory.")
    parser.add_argument("--handoff", help="Path to handoff.md (used to derive --run-dir if missing).")
    parser.add_argument("--event-json", help="Path to JSON event payload from stop hook.")
    parser.add_argument("--decision-kind", default="manual", help="Decision kind tag for the finalize record.")
    args = parser.parse_args()
    event: dict[str, Any] = {}
    if args.event_json:
        try:
            event = json.loads(Path(args.event_json).expanduser().read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                event = {}
        except (OSError, json.JSONDecodeError):
            event = {}
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        record = finalize_run(run_dir=run_dir, event=event, decision_kind=args.decision_kind)
    elif args.handoff:
        record = finalize_for_handoff(
            handoff_path=Path(args.handoff).expanduser(),
            event=event,
            decision_kind=args.decision_kind,
        )
        if record is None:
            print("could not resolve run_dir from --handoff", file=sys.stderr)
            return 2
    else:
        parser.error("one of --run-dir or --handoff is required")
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
