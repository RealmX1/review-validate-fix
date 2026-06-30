#!/usr/bin/env python3
"""``$rvf-analyze`` 主入口（确定性后端）。

职责：
  1. 解析目标 run（``--run-id`` / ``--run-dir`` / ``--latest`` / 位置参数）。
  2. 调 ``orphan_detect.classify_run`` 判断生命周期状态。
  3. 按用户决策（CLI flag）选择处理路径：
     - ``finalized`` → 直接进 scaffold。
     - ``running`` → 拒绝分析，退出码 2。让上层 agent 询问是否要继续等待。
     - ``orphan_candidate`` / ``cancel_without_lock`` → 需要决策：
       - 未传决策 flag → 退出码 3 + classification JSON，让上层 agent 询问用户后重入。
       - ``--auto-finalize-orphan`` → 调 ``rvf_run_finalize.finalize_run``
         (decision_kind=``lazy_orphan_finalize``)，写 ``.interrupted`` (lazy_finalized)，
         继续 scaffold。
       - ``--decline-finalize`` → 写 ``.interrupted`` (declined_finalize)，
         继续 scaffold（降级分析，artifact 残缺也照写出来）。
     - ``half_broken`` → 写 ``.interrupted`` (auto_classified_only)，尽力 scaffold。
  4. 调 ``analysis_artifacts.scaffold_run`` 写 ``analysis/summary.md`` 与
     ``analysis/causality.json``。
  5. 输出最终 JSON（classification + scaffold paths）到 stdout。

设计意图：CLI 自身永远非交互。需要"问用户"的环节由 skill 层 agent
（``references/rvf-analyze.md`` 提示模板）驱动——agent 看到退出码 3 时
向用户提问，得到答复后用对应 flag 重入。这样 CLI 既能在 hook / pipe /
非 TTY 上下文下安全运行，又不丢失 plan 要求的"用户确认"语义。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_artifacts import scaffold_run  # noqa: E402
from orphan_detect import (  # noqa: E402
    Classification,
    classify_run,
    write_interrupted_marker,
)
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import log_root  # noqa: E402

EXIT_OK = 0
EXIT_RUNNING = 2
EXIT_NEEDS_DECISION = 3
EXIT_RESOLVE_FAILED = 4
EXIT_LAZY_FINALIZE_FAILED = 5


def _read_latest_run_dir() -> Path | None:
    """``state/latest.json::summary_path`` 的父目录就是 run_dir。"""
    pointer_path = log_root() / "latest.json"
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    summary_path = payload.get("summary_path")
    if not isinstance(summary_path, str) or not summary_path:
        return None
    candidate = Path(summary_path).expanduser()
    if not candidate.is_file():
        return None
    return candidate.parent


def resolve_run_dir(args: argparse.Namespace) -> Path | None:
    """按 mutex 优先级解析目标 run_dir。返回 None 表示无法定位。"""
    if args.run_dir:
        candidate = Path(args.run_dir).expanduser().resolve()
        return candidate if (candidate / "summary.json").is_file() else None
    if args.run_id:
        candidate = log_root() / "runs" / args.run_id
        return candidate if candidate.is_dir() else None
    if args.target:
        if args.target == "latest":
            return _read_latest_run_dir()
        target_path = Path(args.target).expanduser()
        if not target_path.is_absolute():
            candidate = log_root() / "runs" / args.target
            if candidate.is_dir() and (candidate / "summary.json").is_file():
                return candidate
        candidate = target_path.resolve()
        if (candidate / "summary.json").is_file():
            return candidate
        return None
    if args.latest:
        return _read_latest_run_dir()
    return None


def _classification_payload(classification: Classification) -> dict[str, Any]:
    return {
        "kind": classification.kind,
        "run_dir": classification.run_dir,
        "run_id": classification.run_id,
        "prior_status": classification.prior_status,
        "prior_timestamp": classification.prior_timestamp,
        "age_seconds": classification.age_seconds,
        "has_finalize_lock": classification.has_finalize_lock,
        "has_interrupted_marker": classification.has_interrupted_marker,
        "detected_at": classification.detected_at,
    }


def _lazy_finalize(run_dir: Path) -> dict[str, Any]:
    """惰性 import，避免 CLI 在不需要 finalize 时拉起 trajectory_capture / workspace_diff。"""
    from rvf_run_finalize import finalize_run  # noqa: WPS433 — local import on purpose

    return finalize_run(
        run_dir=run_dir,
        event=None,
        decision_kind="lazy_orphan_finalize",
    )


def analyze(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    run_dir = resolve_run_dir(args)
    if run_dir is None:
        return EXIT_RESOLVE_FAILED, {
            "status": "resolve_failed",
            "message": "could not resolve run_dir; pass --run-dir, --run-id, or --latest",
        }

    classification = classify_run(
        run_dir,
        orphan_age_seconds=max(0.0, args.orphan_age_hours) * 3600,
    )

    if classification.kind == "running" and not args.force:
        return EXIT_RUNNING, {
            "status": "running",
            "classification": _classification_payload(classification),
            "message": (
                "run still appears to be in flight; pass --force to scaffold anyway "
                "or wait for finalize"
            ),
        }

    needs_decision = classification.kind in {"orphan_candidate", "cancel_without_lock"}
    decision_chosen: str | None = None
    finalize_record: dict[str, Any] | None = None

    if needs_decision:
        if args.auto_finalize_orphan and args.decline_finalize:
            return EXIT_NEEDS_DECISION, {
                "status": "conflicting_flags",
                "message": "pass exactly one of --auto-finalize-orphan / --decline-finalize",
            }
        if args.auto_finalize_orphan:
            pre_classification = classification
            try:
                finalize_record = _lazy_finalize(run_dir)
            except Exception as exc:  # noqa: BLE001 — CLI must not raise
                return EXIT_LAZY_FINALIZE_FAILED, {
                    "status": "lazy_finalize_failed",
                    "classification": _classification_payload(classification),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            classification = classify_run(
                run_dir,
                orphan_age_seconds=max(0.0, args.orphan_age_hours) * 3600,
            )
            decision_chosen = "lazy_finalized"
            write_interrupted_marker(
                run_dir,
                classification=classification,
                user_decision=decision_chosen,
                lazy_finalize_decision_kind="lazy_orphan_finalize",
                pre_finalize_classification=pre_classification,
            )
        elif args.decline_finalize:
            decision_chosen = "declined_finalize"
            write_interrupted_marker(
                run_dir,
                classification=classification,
                user_decision=decision_chosen,
            )
        else:
            return EXIT_NEEDS_DECISION, {
                "status": "needs_decision",
                "classification": _classification_payload(classification),
                "message": (
                    "run is not finalized; ask the user, then re-invoke with "
                    "--auto-finalize-orphan or --decline-finalize"
                ),
            }
    elif classification.kind == "half_broken":
        decision_chosen = "auto_classified_only"
        write_interrupted_marker(
            run_dir,
            classification=classification,
            user_decision=decision_chosen,
        )
    elif classification.kind == "running" and args.force:
        decision_chosen = "auto_classified_only"
        write_interrupted_marker(
            run_dir,
            classification=classification,
            user_decision=decision_chosen,
            extra={"forced_through_running": True},
        )

    scaffold = scaffold_run(run_dir)

    payload: dict[str, Any] = {
        "status": "ok",
        "classification": _classification_payload(classification),
        "user_decision": decision_chosen,
        "summary_md_path": str(scaffold["summary_md_path"]),
        "causality_json_path": str(scaffold["causality_json_path"]),
        "stats": scaffold["stats_dict"],
    }
    if finalize_record is not None:
        payload["lazy_finalize"] = {
            "decision_kind": finalize_record.get("decision_kind"),
            "completed_at": finalize_record.get("completed_at"),
            "errors": finalize_record.get("errors") or [],
        }
    return EXIT_OK, payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run /rvf-analyze deterministic backend on a finalized RVF run.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="run_id, run_dir path, or the literal 'latest' (matches the user-facing "
        "$rvf-analyze [<run_id>|latest] form).",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--run-id", help="Resolve to <log_root>/runs/<run-id>.")
    selector.add_argument("--run-dir", help="Use this directory directly.")
    selector.add_argument("--latest", action="store_true", help="Read state/latest.json.")
    parser.add_argument(
        "--orphan-age-hours",
        type=float,
        default=6.0,
        help="Threshold for marking inflight runs as orphan_candidate (default: 6h).",
    )
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument(
        "--auto-finalize-orphan",
        action="store_true",
        help="If classification is orphan_candidate/cancel_without_lock, run "
        "finalize_run(decision_kind=lazy_orphan_finalize) before scaffolding.",
    )
    decision.add_argument(
        "--decline-finalize",
        action="store_true",
        help="If classification is orphan_candidate/cancel_without_lock, write a "
        "declined .interrupted marker and scaffold from whatever artifacts exist.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Scaffold even if classification is 'running'. Use sparingly — rollout "
        "may still be growing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit only the structured JSON payload on stdout (default behavior).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    exit_code, payload = analyze(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
