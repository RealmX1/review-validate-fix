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
import os
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


def _scaffold_analysis(run_dir: Path) -> dict[str, Any]:
    """在 finalize 末尾生成 ``$rvf-analyze`` 的确定性分析骨架。"""
    # 延迟导入，保持 finalize 启动轻量。
    from analysis_artifacts import scaffold_run  # noqa: WPS433

    scaffold = scaffold_run(run_dir)
    return {
        "summary_md_path": str(scaffold["summary_md_path"]),
        "causality_json_path": str(scaffold["causality_json_path"]),
        "stats": scaffold["stats_dict"],
    }


TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _rollout_token_usage(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict):
                continue
            records.append(
                {
                    "timestamp": record.get("timestamp"),
                    "total": {
                        key: int(total.get(key) or 0)
                        for key in TOKEN_USAGE_KEYS
                    },
                }
            )
    return records


def _duration_seconds(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((end_dt - start_dt).total_seconds(), 3))


def _usage_summary(run_dir: Path) -> dict[str, Any]:
    pre_rollout = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.jsonl"
    rvf_rollout = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.jsonl"
    pre_records = _rollout_token_usage(pre_rollout)
    rvf_records = _rollout_token_usage(rvf_rollout)
    baseline = pre_records[-1]["total"] if pre_records else {key: 0 for key in TOKEN_USAGE_KEYS}
    final = rvf_records[-1]["total"] if rvf_records else baseline
    delta = {
        key: max(0, int(final.get(key, 0)) - int(baseline.get(key, 0)))
        for key in TOKEN_USAGE_KEYS
    }
    delta["noncached_input_tokens"] = max(
        0,
        delta["input_tokens"] - delta["cached_input_tokens"],
    )
    return {
        "schema_version": 1,
        "source": "artifacts/trajectory/rvf/rollout.jsonl",
        "baseline_source": (
            "artifacts/trajectory/pre-rvf/rollout.jsonl"
            if pre_records
            else None
        ),
        "started_at": rvf_records[0].get("timestamp") if rvf_records else None,
        "ended_at": rvf_records[-1].get("timestamp") if rvf_records else None,
        "wall_seconds": _duration_seconds(
            rvf_records[0].get("timestamp") if rvf_records else None,
            rvf_records[-1].get("timestamp") if rvf_records else None,
        ),
        "token_count_event_count": len(rvf_records),
        "baseline_total_token_usage": baseline,
        "final_total_token_usage": final,
        **delta,
    }


def _write_usage_summary(run_dir: Path) -> dict[str, Any]:
    summary = _usage_summary(run_dir)
    path = run_dir / "artifacts" / "usage" / "usage-summary.json"
    _atomic_write_json(path, summary)
    return {"summary_path": str(path), **summary}


def _release_tracker_lease(
    run_dir: Path,
    repo: Path | None,
    *,
    decision_kind: str,
) -> dict[str, Any] | None:
    if repo is None:
        return None
    contract_path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(contract, dict):
        return None
    lease_id = contract.get("tracker_lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return None
    primary_units_raw = contract.get("primary_units")
    primary_units = [
        item.strip()
        for item in primary_units_raw
        if isinstance(item, str) and item.strip()
    ] if isinstance(primary_units_raw, list) else []
    scope_hash = contract.get("tracker_scope_hash")
    if not isinstance(scope_hash, str) or not scope_hash:
        scope_hash = None
    run_id = contract.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = None

    import diff_tracker  # noqa: WPS433

    log_root_raw = os.environ.get("CODEX_RVF_LOG_ROOT", "").strip()
    log_root_override = Path(log_root_raw).expanduser().resolve() if log_root_raw else None
    release_reason = "failed" if decision_kind in {"cancelled", "cancel", "interrupted"} else "completed"
    if release_reason == "completed":
        result = diff_tracker.complete_review_scope(
            repo=repo,
            lease_id=lease_id,
            unit_ids=primary_units,
            scope_hash=scope_hash,
            run_id=run_id,
            reason=release_reason,
            log_root_override=log_root_override,
        )
    else:
        result = diff_tracker.lease_release(
            repo=repo,
            lease_id=lease_id,
            reason=release_reason,
            log_root_override=log_root_override,
        )
    return {
        "scope_contract_path": str(contract_path),
        "lease_id": lease_id,
        "release_reason": release_reason,
        "primary_unit_count": len(primary_units),
        **result,
    }


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
        "usage": None,
        "workspace_diff": None,
        "tracker_lease_release": None,
        "analysis": None,
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

    try:
        finalize_record["usage"] = _write_usage_summary(run_dir)
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "usage_summary",
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

    try:
        finalize_record["tracker_lease_release"] = _release_tracker_lease(
            run_dir,
            repo,
            decision_kind=decision_kind,
        )
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "tracker_lease_release",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    finalize_record["completed_at"] = _utc_now()

    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        merged = _read_summary(summary_path)
        merged["finalize"] = finalize_record
        try:
            _atomic_write_json(summary_path, merged)
        except OSError as exc:
            finalize_record["errors"].append(
                {"stage": "summary_merge", "error": f"{type(exc).__name__}: {exc}"}
            )

    try:
        finalize_record["analysis"] = _scaffold_analysis(run_dir)
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "analysis_scaffold",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

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


def public_finalize_errors(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """返回适合写入 hook ledger/summary 的 finalize 错误摘要。"""
    if not isinstance(record, dict):
        return []
    errors = record.get("errors")
    if not isinstance(errors, list):
        return []

    public_errors: list[dict[str, Any]] = []
    for item in errors:
        if not isinstance(item, dict):
            public_errors.append({"stage": "unknown", "error": str(item)})
            continue
        stage = item.get("stage")
        error = item.get("error")
        public_errors.append(
            {
                "stage": stage if isinstance(stage, str) and stage else "unknown",
                "error": error if isinstance(error, str) else str(error),
            }
        )
    return public_errors


def surface_finalize_record_errors(
    ledger: Any,
    record: dict[str, Any] | None,
    *,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把 finalize 返回记录中的非抛出错误暴露到当前 hook 的 ledger/summary。

    finalize_run 仍保留 trajectory/workspace_diff/analysis 的非致命错误模型；
    该 helper 只负责让 handoff completion caller 不再把这些错误隐藏在 actual run
    summary 内部。
    """
    errors = public_finalize_errors(record)
    if not errors:
        return []

    run_dir = record.get("run_dir") if isinstance(record, dict) else None
    paths = {"run_dir": run_dir} if isinstance(run_dir, str) and run_dir else {}
    try:
        ledger.event(
            phase="handoff",
            event="finalize_completed_with_errors",
            status="warning",
            reason_code="finalize_error",
            level="warn",
            paths=paths,
            finalize_error_count=len(errors),
            finalize_errors=errors,
        )
    except Exception:
        pass

    summary_path = getattr(ledger, "summary_path", None)
    if summary_path is not None:
        try:
            path = Path(summary_path)
            summary = _read_summary(path)
            if summary:
                summary["finalize_status"] = "warning"
                summary["finalize_error_count"] = len(errors)
                summary["finalize_errors"] = errors
                if isinstance(run_dir, str) and run_dir:
                    summary["finalized_run_dir"] = run_dir
                _atomic_write_json(path, summary)
        except OSError:
            pass

    if payload is not None:
        message = payload.get("systemMessage")
        if isinstance(message, str) and "finalize_errors=" not in message:
            payload["systemMessage"] = f"{message}; finalize_errors={len(errors)}"

    return errors


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
