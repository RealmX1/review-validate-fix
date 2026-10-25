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
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rvf_automatic_closeout_bridge import freeze_completion_notification, publish_completion  # noqa: E402
from trajectory_capture import capture_run  # noqa: E402
from workspace_diff import capture_after, compute as compute_workspace_diff  # noqa: E402
from core.run_ledger.review_run_lifecycle import (  # noqa: E402
    REVIEW_PHASE_SEQUENCE,
    atomic_write_json,
    merge_review_run_summary, record_review_run_terminal,
    read_review_run_lifecycle, review_run_start_evidence,
    parse_timezone_aware_review_run_timestamp_utc,
    review_run_timestamp_is_within_optional_bounds,
)


NON_REVIEW_COMPLETION_KINDS = frozenset({
    "cancelled", "cancel", "interrupted", "aborted_before_review", "superseded", "declined",
})
TERMINAL_ARCHIVE_FINALIZATION_PROGRESS_FILENAME = "review-run-terminal-archive-finalization-progress.json"
FINALIZATION_STAGE_RESULT_KEYS = {
    "trajectory_capture": "trajectory", "usage_summary": "usage", "workspace_diff": "workspace_diff",
    "tracker_lease_release": "tracker_lease_release", "review_highwater": "review_highwater",
    "analysis_scaffold": "analysis",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and (path / "summary.json").is_file()


def resolve_run_dir(*, handoff_path: Path | None, event: dict[str, Any] | None) -> Path | None:
    """从 handoff path / event 中反查 actual RVF run_dir。

    optimistic 顺序:
      1. handoff_path 的 ../.. (即 <run_dir>/artifacts/handoff.md → <run_dir>)
      2. event['rvf_run_dir'] / event['RVF_RUN_DIR'] (若 caller 显式传入)
    返回 None 表示无法定位。

    历史上这里还有一个 ``os.environ.get('RVF_RUN_DIR')`` fallback，但
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
        for key in ("rvf_run_dir", "RVF_RUN_DIR"):
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
            if not all(type(total.get(key)) is int and total[key] >= 0 for key in TOKEN_USAGE_KEYS):
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
    start_dt = parse_timezone_aware_review_run_timestamp_utc(start)
    end_dt = parse_timezone_aware_review_run_timestamp_utc(end)
    if start_dt is None or end_dt is None or end_dt < start_dt:
        return None
    whole_seconds = end_dt[0] - start_dt[0]
    duration_seconds = Decimal(whole_seconds.days * 86400 + whole_seconds.seconds) + end_dt[1] - start_dt[1]
    return float(round(duration_seconds, 3))


def _usage_summary(run_dir: Path) -> dict[str, Any]:
    pre_rollout = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.jsonl"
    rvf_rollout = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.jsonl"
    pre_records = _rollout_token_usage(pre_rollout)
    rvf_records = _rollout_token_usage(rvf_rollout)
    summary = _read_summary(run_dir / "summary.json")
    started_at, start_source = review_run_start_evidence(run_dir, summary)
    lifecycle = read_review_run_lifecycle(run_dir)
    terminal_at = summary.get("review_run_terminal_at") or lifecycle.get("review_run_terminal_at")
    start_moment = parse_timezone_aware_review_run_timestamp_utc(started_at)
    terminal_moment = parse_timezone_aware_review_run_timestamp_utc(terminal_at)
    window_is_known = start_moment is not None and terminal_moment is not None and start_moment <= terminal_moment
    before_start = [record for record in rvf_records if start_moment is not None
                    and review_run_timestamp_is_within_optional_bounds(record.get("timestamp"), window_end=started_at)]
    bounded_records = [record for record in rvf_records
                       if window_is_known and review_run_timestamp_is_within_optional_bounds(
                           record.get("timestamp"), window_start=started_at, window_end=terminal_at)]
    pre_identity, current_identity = _rollout_session_identity(pre_rollout), _rollout_session_identity(rvf_rollout)
    eligible_pre_records = [record for record in pre_records
                            if start_moment is not None and review_run_timestamp_is_within_optional_bounds(
                                record.get("timestamp"), window_end=started_at)]
    baseline_record = None
    if start_moment is not None and current_identity:
        if before_start:
            baseline_record = before_start[-1]
        elif pre_identity == current_identity and eligible_pre_records:
            baseline_record = eligible_pre_records[-1]
    baseline = baseline_record["total"] if baseline_record else None
    # 起点样本只能证明基线，不能同时冒充结束观测。
    final_records = [record for record in bounded_records
                     if parse_timezone_aware_review_run_timestamp_utc(record.get("timestamp")) > start_moment]
    final = final_records[-1]["total"] if final_records else None
    valid_delta = baseline is not None and final is not None and all(final[key] >= baseline[key] for key in TOKEN_USAGE_KEYS)
    delta = {
        key: final[key] - baseline[key] if valid_delta else None
        for key in TOKEN_USAGE_KEYS
    }
    delta["noncached_input_tokens"] = (
        max(0, delta["input_tokens"] - delta["cached_input_tokens"])
        if valid_delta else None
    )
    return {
        "schema_version": 1,
        "token_usage_coverage_status": "complete" if valid_delta else "unavailable",
        "review_run_start_evidence_source": start_source,
        "source": "artifacts/trajectory/rvf/rollout.jsonl",
        "baseline_source": (
            ("artifacts/trajectory/rvf/rollout.jsonl" if before_start else "artifacts/trajectory/pre-rvf/rollout.jsonl")
            if baseline is not None else None
        ),
        "started_at": started_at,
        "ended_at": terminal_at,
        "wall_seconds": _duration_seconds(
            started_at, terminal_at,
        ),
        "token_count_event_count": len(bounded_records),
        "baseline_total_token_usage": baseline,
        "final_total_token_usage": final,
        **delta,
        **_phase_and_component_resource_observations(run_dir, started_at, terminal_at, lifecycle),
    }


def _rollout_session_identity(path: Path) -> str | None:
    manifest_id = None
    for manifest_name in ("rollout.manifest.json", "manifest.json"):
        manifest = _read_summary(path.parent / manifest_name)
        value = manifest.get("source_session_id")
        if isinstance(value, str) and value:
            manifest_id = value
            break
    try:
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= 64:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("type") == "session_meta":
                        value = (record.get("payload") or {}).get("id")
                        if isinstance(value, str) and value:
                            return value if manifest_id is None or value == manifest_id else None
    except (OSError, UnicodeError):
        return None
    return manifest_id


def _phase_and_component_resource_observations(
    run_dir: Path, started_at: str | None, terminal_at: str | None, lifecycle: dict[str, Any],
) -> dict[str, Any]:
    phase_starts = lifecycle.get("phase_started_at", {})
    parsed_phases = [(parse_timezone_aware_review_run_timestamp_utc(timestamp), phase, timestamp)
                     for phase, timestamp in phase_starts.items()]
    phase_order = {phase: index for index, phase in enumerate(REVIEW_PHASE_SEQUENCE)}
    phases = sorted((item for item in parsed_phases if item[0] is not None),
                    key=lambda item: (item[0], phase_order.get(item[1], len(phase_order))))
    phase_times_are_known = len(phases) == len(parsed_phases)
    phase_wall_seconds = {phase: None for phase in phase_starts if phase != "complete"}
    if phase_times_are_known:
        phase_wall_seconds.update({
            phase: _duration_seconds(timestamp, phases[index + 1][2] if index + 1 < len(phases) else terminal_at)
            for index, (_moment, phase, timestamp) in enumerate(phases) if phase != "complete"
        })
    start_moment = parse_timezone_aware_review_run_timestamp_utc(started_at)
    terminal_moment = parse_timezone_aware_review_run_timestamp_utc(terminal_at)
    window_is_known = start_moment is not None and terminal_moment is not None and start_moment <= terminal_moment
    root = run_dir / "artifacts/trajectory/rvf"
    sources = [("main_session", root / "rollout.jsonl")]
    if (root / "subagents").is_dir():
        sources.extend((f"subagent:{child.name}", child / "rollout.jsonl")
                       for child in sorted((root / "subagents").iterdir()) if child.is_dir())
    seen_responses: dict[tuple[str, str], dict[str, int]] = {}
    phase_usage: dict[str, dict[str, int]] = {}
    component_usage: dict[str, Any] = {}
    conflicts: set[tuple[str, str]] = set()
    for component, path in sources:
        session_id = _rollout_session_identity(path)
        observed = {key: 0 for key in TOKEN_USAGE_KEYS}
        response_count = 0
        component_start = started_at
        coverage_reason = None
        if not window_is_known:
            coverage_reason = "review_run_time_bounds_unavailable"
        elif not session_id:
            coverage_reason = "native_session_identity_unavailable"
        elif component != "main_session":
            spawn = _read_summary(path.parent / "manifest.json").get("spawn") or {}
            spawned_at = spawn.get("spawned_at") if isinstance(spawn, dict) else None
            spawn_moment = parse_timezone_aware_review_run_timestamp_utc(spawned_at)
            if spawn_moment is None:
                coverage_reason = "subagent_spawn_timestamp_unavailable"
            elif spawn.get("agent_id") != session_id:
                coverage_reason = "subagent_spawn_identity_mismatch"
            elif spawn_moment > start_moment:
                component_start = spawned_at
        if path.is_file() and coverage_reason is None:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "token_usage_record":
                        continue
                    payload = record.get("payload") or {}
                    timestamp = record.get("timestamp")
                    if not review_run_timestamp_is_within_optional_bounds(
                        timestamp, window_start=component_start, window_end=terminal_at,
                    ):
                        continue
                    response_id, usage = payload.get("response_id"), payload.get("usage")
                    if payload.get("thread_id") != session_id or not isinstance(response_id, str) or not isinstance(usage, dict):
                        continue
                    if not all(type(usage.get(key)) is int and usage[key] >= 0 for key in TOKEN_USAGE_KEYS):
                        continue
                    usage = {key: usage[key] for key in TOKEN_USAGE_KEYS}
                    response_identity = (session_id, response_id)
                    if response_identity in seen_responses:
                        if seen_responses[response_identity] != usage:
                            conflicts.add(response_identity)
                        continue
                    seen_responses[response_identity] = usage
                    response_count += 1
                    for key in TOKEN_USAGE_KEYS:
                        observed[key] += usage[key]
                    response_moment = parse_timezone_aware_review_run_timestamp_utc(timestamp)
                    phase = next((phase for start, phase, _raw in reversed(phases)
                                  if phase_times_are_known and start <= response_moment), "unallocated")
                    if component != "main_session":
                        phase = "subagent_unallocated"
                    phase_usage.setdefault(phase, {key: 0 for key in TOKEN_USAGE_KEYS})
                    for key in TOKEN_USAGE_KEYS:
                        phase_usage[phase][key] += usage[key]
        component_usage[component] = {
            "source": str(path), "native_session_id": session_id, "observed_response_count": response_count,
            "coverage_status": "partial" if response_count else "unavailable",
            "coverage_reason": coverage_reason or (None if response_count else "no_native_response_observed_within_component_window"),
            "observed_token_usage": observed if response_count else None,
        }
    reviewers = run_dir / "artifacts/reviewers"
    if reviewers.is_dir():
        for reviewer in reviewers.iterdir():
            if reviewer.is_dir() and (reviewer / "review-result.json").is_file():
                component_usage[f"reviewer:{reviewer.name}"] = {
                    "coverage_status": "unavailable", "observed_token_usage": None,
                    "source": str(reviewer / "reviewer.summary.json"),
                }
    analyze_status_path = run_dir / "artifacts/analysis/.analyze-thread.status.json"
    analyze_status = _read_summary(analyze_status_path)
    return {
        "phase_wall_seconds": phase_wall_seconds, "phase_response_token_usage": phase_usage,
        "component_response_token_usage": component_usage,
        "observed_native_response_count": len(seen_responses),
        "conflicting_native_response_identities": [
            {"native_session_id": session_id, "response_id": response_id}
            for session_id, response_id in sorted(conflicts)
        ],
        "background_analysis": {
            "status_path": str(analyze_status_path),
            "completion_status": analyze_status.get("formal_analysis_completion_status", "pending"),
            "wall_seconds": _duration_seconds(analyze_status.get("started_at"), analyze_status.get("finished_at")),
            "token_usage_coverage_status": "unavailable",
        },
        "aggregation_note": "累计计数差值和逐响应观测是不同口径，不相加；并行组件累计时长不代表主任务额外等待。",
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

    import _rvf_pyroot  # noqa: WPS433,F401 — pyroot 上 sys.path，供 core.* import
    from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: WPS433

    log_root_raw = os.environ.get("CODEX_RVF_LOG_ROOT", "").strip()
    log_root_override = Path(log_root_raw).expanduser().resolve() if log_root_raw else None
    release_reason = "failed" if decision_kind in NON_REVIEW_COMPLETION_KINDS else "completed"
    did_review: bool | None = None
    if release_reason == "completed":
        # A genuine review leaves at least one reviewer artifact on disk; a no-op
        # follow-up (clean tree → 0 reviewers dispatched) leaves none. Only flip
        # leased units to `reviewed` when a reviewer actually ran, so a no-op
        # completion releases the lease without silently marking unreviewed work
        # reviewed (which would convert an over-dispatch into a missed-review).
        did_review = any(
            (run_dir / "artifacts" / "reviewers").glob("*/review-result.json")
        )
        result = reviewable_unit_diff_tracker.complete_review_scope(
            repo=repo,
            lease_id=lease_id,
            unit_ids=primary_units,
            scope_hash=scope_hash,
            run_id=run_id,
            reason=release_reason,
            mark_reviewed=did_review,
            log_root_override=log_root_override,
        )
    else:
        result = reviewable_unit_diff_tracker.lease_release(
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
        "did_review": did_review,
        **result,
    }


def _head_oid(repo: Path) -> str | None:
    import subprocess  # noqa: PLC0415 - off the hot path, only on finalize fallback

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    # A fresh repo with no commits prints nothing / the literal "HEAD".
    return head if head and head != "HEAD" else None


def _advance_review_highwater(
    run_dir: Path,
    repo: Path | None,
    event: dict[str, Any] | None,
    summary: dict[str, Any],
    finalize_record: dict[str, Any],
) -> dict[str, Any]:
    """review 真正完成时，把 last-reviewed 高水位推进到本次被审到的 HEAD。

    committed-round 漏审检测以该高水位（``review_highwater_marker``）而非 round-baseline
    作窗口下界：round-baseline 被 UserPromptSubmit **每条 prompt** 顶到当前 HEAD，会让
    「在某轮提交、却没在该轮自己的 Stop 里被审」的工作，一旦后续任意 prompt 推进 baseline
    就永久落出窗口（漏审）。高水位只在此处（``did_review`` 真完成）与 rvf-land 封窗推进，于是
    孤儿提交留在 ``high-water..HEAD`` 内直到真被审。纯 no-op / 失败完成不推进，偏向「重审」
    安全方向。键（task 优先、session 回退）与 committed-round 读取对齐，同 ``log_root()``。
    best-effort：任何失败折叠为 skip，绝不影响 finalize。见 ``review_highwater_marker`` 模块头。
    """
    if repo is None:
        return {"status": "skipped", "reason": "missing_repo"}
    lease_rel = finalize_record.get("tracker_lease_release")
    did_review = bool(lease_rel.get("did_review")) if isinstance(lease_rel, dict) else False
    if not did_review:
        return {"status": "skipped", "reason": "no_review_artifacts"}
    # S8·R23:RVF review 真完成计数(守护心跳的分子之一),best-effort 绝不影响 finalize。
    try:
        import agentic_initiation_guardian_dispatch as guardian_dispatch  # noqa: PLC0415

        guardian_dispatch.record_review_completion()
    except Exception:  # noqa: BLE001
        pass
    # 被审到的 HEAD = finalize 时的 HEAD：committed-round 审已提交工作时 HEAD 未动；dirty
    # review 时 dirty 工作尚未提交、HEAD 同样未动。复用 workspace_diff 已算的 head_after，
    # 缺失时现算 git rev-parse HEAD 兜底。
    reviewed_head: str | None = None
    workspace_diff = finalize_record.get("workspace_diff")
    if isinstance(workspace_diff, dict):
        candidate = workspace_diff.get("head_after")
        if isinstance(candidate, str) and candidate.strip():
            reviewed_head = candidate.strip()
    if not reviewed_head:
        reviewed_head = _head_oid(repo)
    if not reviewed_head:
        return {"status": "skipped", "reason": "no_head"}

    from core.session_scope_allocation import review_highwater_marker  # noqa: PLC0415 - lazy, off the hot import path

    event_ctx = event or {}
    try:
        from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

        task_id = current_kanban_task_id(event_ctx)
    except Exception:
        task_id = None
    session_id: str | None = None
    raw_session = event_ctx.get("session_id")
    if isinstance(raw_session, str) and raw_session.strip():
        session_id = raw_session.strip()
    elif isinstance(summary, dict):
        summary_session = summary.get("session_id")
        if isinstance(summary_session, str) and summary_session.strip():
            session_id = summary_session.strip()
    marker_path = review_highwater_marker.write_review_highwater(
        task_id=task_id,
        session_id=session_id,
        reviewed_head=reviewed_head,
        repo=str(repo),
        source="stop_finalize",
    )
    if marker_path is None:
        return {
            "status": "skipped",
            "reason": "no_marker_key",
            "reviewed_head": reviewed_head,
        }
    return {
        "status": "advanced",
        "reviewed_head": reviewed_head,
        "marker_path": str(marker_path),
    }


def read_review_run_finalization_evidence(
    *, run_dir: Path, run_id: str, repo: Path,
) -> dict[str, Any]:
    """只读核验首次终态及已完成缓存；不重跑阶段、不发布通知。"""
    import fcntl
    import hashlib

    run_dir = run_dir.resolve()
    artifacts = run_dir / "artifacts"
    execution_lock = artifacts / ".finalize-execution.lock"
    if execution_lock.is_symlink():
        raise ValueError("finalize 执行锁不能是符号链接")
    with execution_lock.open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            summary = _read_summary(run_dir / "summary.json")
            if (summary.get("run_id") != run_id or not isinstance(summary.get("repo"), str)
                    or Path(summary["repo"]).resolve() != repo.resolve()):
                raise ValueError("finalize 完成核验的 run 或仓库归属不符")
            lifecycle = read_review_run_lifecycle(run_dir)
            if lifecycle and lifecycle.get("run_id") != run_id:
                raise ValueError("finalize 首次终态的 run 身份不符")
            terminal_status = lifecycle.get("review_run_terminal_status") or summary.get("review_run_terminal_status")
            terminal_at = lifecycle.get("review_run_terminal_at") or summary.get("review_run_terminal_at")
            if (terminal_status and summary.get("review_run_terminal_status")
                    and summary["review_run_terminal_status"] != terminal_status):
                raise ValueError("finalize summary 与首次终态冲突")
            evidence: dict[str, Any] = {"terminal_status": terminal_status, "terminal_at": terminal_at,
                                        "completion": None}
            cache_path = artifacts / ".finalize.lock"
            if cache_path.is_symlink():
                raise ValueError("finalize 完成缓存不能是符号链接")
            if not cache_path.exists():
                return evidence
            if not cache_path.is_file():
                raise ValueError("finalize 完成缓存必须是普通文件")
            raw = cache_path.read_bytes()
            cached = json.loads(raw)
            if not isinstance(cached, dict):
                raise ValueError("finalize 完成缓存必须是对象")
            kind = cached.get("decision_kind")
            if (cached.get("run_id") != run_id or not isinstance(cached.get("run_dir"), str)
                    or Path(cached["run_dir"]).resolve() != run_dir or not isinstance(cached.get("repo"), str)
                    or Path(cached["repo"]).resolve() != repo.resolve() or not isinstance(kind, str) or not kind):
                raise ValueError("finalize 完成缓存的 run、仓库或决策身份不符")
            expected_terminal = kind if kind in NON_REVIEW_COMPLETION_KINDS else "handoff"
            if (not terminal_at or terminal_status != expected_terminal
                    or cached.get("review_run_terminal_status") != terminal_status
                    or cached.get("review_run_terminal_at") != terminal_at
                    or lifecycle.get("review_run_terminal_status") != terminal_status):
                raise ValueError("finalize 完成缓存与首次终态不符")
            errors = cached.get("errors")
            if not isinstance(errors, list):
                raise ValueError("finalize 完成缓存缺少明确的阶段错误结果")
            if errors:
                return evidence
            completed_at = cached.get("completed_at")
            if (not isinstance(completed_at, str)
                    or datetime.fromisoformat(completed_at.replace("Z", "+00:00")).tzinfo is None):
                raise ValueError("finalize 完成缓存缺少有效完成时间")
            projected = summary.get("finalize")
            if (not isinstance(projected, dict) or projected.get("errors") != []
                    or any(projected.get(key) != cached.get(key) for key in (
                        "run_id", "run_dir", "repo", "decision_kind", "completed_at",
                        "review_run_terminal_status", "review_run_terminal_at"))):
                raise ValueError("finalize 完成缓存与正式 summary 回执不符")
            evidence["completion"] = {"record": cached, "path": str(cache_path),
                                      "sha256": hashlib.sha256(raw).hexdigest()}
            return evidence
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def finalize_run(
    *,
    run_dir: Path,
    event: dict[str, Any] | None = None,
    decision_kind: str = "handoff",
) -> dict[str, Any]:
    import fcntl

    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with (artifacts_dir / ".finalize-execution.lock").open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        try:
            return _finalize_run_owned(run_dir=run_dir, event=event, decision_kind=decision_kind)
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def _terminal_archive_stage_artifact_digests(run_dir: Path, stage: str, record: dict[str, Any]) -> dict[str, str]:
    """只固定捕获/快照产物；可继续更新的计量与分析投影不反证历史阶段。"""
    artifacts = run_dir / "artifacts"
    if stage == "trajectory_capture":
        paths = list((artifacts / "trajectory").rglob("*"))
    elif stage == "workspace_diff" and (record.get("workspace_diff") or {}).get("status") == "complete":
        paths = [artifacts / name for name in ("after-workspace-snapshot.json", "workspace-diff.json", "workspace-diff.patch")]
    else:
        paths = []
    digests = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError("finalize 阶段产物不能跳出已绑定 run")
        if path.is_file():
            digests[str(path.relative_to(run_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _terminal_archive_finalization_progress_identity(run_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    contract = run_dir / "artifacts/inputs/scope.contract.json"
    if contract.is_symlink():
        raise ValueError("finalize 阶段记录的 scope contract 不能是符号链接")
    return {key: record.get(key) for key in (
        "run_id", "run_dir", "repo", "decision_kind", "review_run_terminal_status", "review_run_terminal_at",
    )} | {"scope_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest() if contract.is_file() else None}


def _read_terminal_archive_finalization_progress(
    run_dir: Path, identity: dict[str, Any],
) -> dict[str, Any] | None:
    path = run_dir / "artifacts" / TERMINAL_ARCHIVE_FINALIZATION_PROGRESS_FILENAME
    if path.is_symlink():
        raise ValueError("finalize 持久阶段记录不能是符号链接")
    if not path.exists():
        return None
    progress = json.loads(path.read_text())
    if not isinstance(progress, dict) or progress.get("identity") != identity:
        raise ValueError("finalize 持久阶段记录与 run、scope 或首次终态不符")
    record, completed = progress.get("record"), progress.get("completed_stages")
    if (not isinstance(record, dict) or not isinstance(completed, list)
            or any(stage not in FINALIZATION_STAGE_RESULT_KEYS for stage in completed)):
        raise ValueError("finalize 持久阶段记录缺少明确完成事实")
    if any(record.get(key) != identity.get(key) for key in (
        "run_id", "run_dir", "repo", "decision_kind", "review_run_terminal_status", "review_run_terminal_at",
    )):
        raise ValueError("finalize 阶段结果与其身份回执不符")
    digests = progress.get("artifact_sha256_by_stage")
    if not isinstance(digests, dict):
        raise ValueError("finalize 持久阶段记录缺少产物摘要")
    for stage in completed:
        if not isinstance(digests.get(stage), dict):
            raise ValueError("finalize 已确认阶段缺少产物摘要")
        for relative, digest in digests[stage].items():
            artifact = run_dir / relative
            if (not artifact.resolve().is_relative_to(run_dir.resolve()) or artifact.is_symlink()
                    or not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest):
                raise ValueError("finalize 已确认阶段的产物摘要不符；恢复原绑定产物后重验")
    return progress


def terminal_archive_finalization_progress_recovery_evidence_status(request: dict[str, Any]) -> str:
    """只以阶段证据可核验性的变化刷新预算，不使用每次 checkpoint 的内容变化。"""
    run_dir = Path(request["run_dir"])
    cache = run_dir / "artifacts/.finalize.lock"
    path = run_dir / "artifacts" / TERMINAL_ARCHIVE_FINALIZATION_PROGRESS_FILENAME
    if cache.exists() or cache.is_symlink() or not path.exists() and not path.is_symlink():
        return "not_pending"
    try:
        lifecycle = read_review_run_lifecycle(run_dir)
        record = {"run_id": request["run_id"], "run_dir": str(run_dir), "repo": request["repo_realpath"],
                  "decision_kind": request["decision_kind"],
                  "review_run_terminal_status": lifecycle.get("review_run_terminal_status") or request["decision_kind"],
                  "review_run_terminal_at": lifecycle.get("review_run_terminal_at") or request["requested_at"]}
        _read_terminal_archive_finalization_progress(run_dir, _terminal_archive_finalization_progress_identity(run_dir, record))
    except (KeyError, OSError, TypeError, ValueError):
        return "unavailable"
    return "verified"


def _finalize_run_owned(
    *, run_dir: Path, event: dict[str, Any] | None, decision_kind: str,
) -> dict[str, Any]:
    """对指定 run_dir 执行一次 finalize。已经 finalize 过则直接返回 lock 内的缓存结果。"""
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    lock_path = artifacts_dir / ".finalize.lock"
    resume_record: dict[str, Any] | None = None
    resuming_failed_cache = False

    if lock_path.exists():
        try:
            cached = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        else:
            if isinstance(cached, dict):
                if (cached.get("decision_kind") == decision_kind and decision_kind in NON_REVIEW_COMPLETION_KINDS
                        and cached.get("errors") and (event or {}).get("review_run_resume_failed_terminal_archive_stages")):
                    if Path(str(cached.get("run_dir", ""))).resolve() != run_dir.resolve():
                        raise ValueError("部分归档缓存的 run 身份不符")
                    # 独立保留失败结果；恢复只重做失败阶段，已释放的 lease 不重复执行。
                    digest = hashlib.sha256(json.dumps(cached, sort_keys=True).encode()).hexdigest()
                    history_path = artifacts_dir / "terminal-archive-finalization-attempts" / f"{digest}.json"
                    if not history_path.exists():
                        atomic_write_json(history_path, cached)
                    resume_record = cached
                    resuming_failed_cache = True
                else:
                    # 通知失败只能重试发布，不能把有效缓存当作读取失败而重做 finalize。
                    if cached.get("decision_kind") not in NON_REVIEW_COMPLETION_KINDS:
                        publish_completion(run_dir, cached)
                    cached.setdefault("already_finalized", True)
                    return cached

    summary = _read_summary(run_dir / "summary.json")
    repo = _resolve_repo(run_dir, summary, event)
    terminal_was_already_recorded = bool(read_review_run_lifecycle(run_dir).get("review_run_terminal_status"))
    terminal_at = (event or {}).get("review_run_terminal_observed_at") or _utc_now()
    lifecycle = record_review_run_terminal(
        run_dir, run_id=summary.get("run_id") or run_dir.name,
        terminal_status=decision_kind if decision_kind in NON_REVIEW_COMPLETION_KINDS else "handoff",
        terminal_at=terminal_at,
    )
    if decision_kind in NON_REVIEW_COMPLETION_KINDS and lifecycle["review_run_terminal_status"] != decision_kind:
        raise ValueError("已成立的首次终态不能再执行不同的非成功归档")
    if summary.get("run_id"):
        summary = merge_review_run_summary(run_dir, {"run_id": summary["run_id"]})

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
        "review_highwater": None,
        "analysis": None,
        "errors": [],
        "review_run_terminal_status": lifecycle["review_run_terminal_status"],
        "review_run_terminal_at": lifecycle["review_run_terminal_at"],
        "review_run_terminal_reason": (event or {}).get("review_run_terminal_reason") or (resume_record or {}).get("review_run_terminal_reason"),
    }
    progress_enabled = decision_kind in NON_REVIEW_COMPLETION_KINDS
    progress_identity = _terminal_archive_finalization_progress_identity(run_dir, finalize_record) if progress_enabled else None
    progress = _read_terminal_archive_finalization_progress(run_dir, progress_identity) if progress_enabled else None
    completed_stages = set(progress["completed_stages"]) if progress else set()
    artifact_digests = dict(progress["artifact_sha256_by_stage"]) if progress else {}
    if progress is not None and resume_record is None:
        resume_record = progress["record"]
    if (progress_enabled and resume_record is None
            and (terminal_was_already_recorded or (event or {}).get("review_run_recovering_incomplete_terminal_archive"))):
        # 进程可能在首次终态落盘后、阶段回执前退出；不能把现在的工作树当成过去。
        resume_record = dict(finalize_record)
    failed_stages = {error.get("stage") for error in (resume_record or {}).get("errors", [])}
    if progress_enabled and resume_record is not None and (progress is not None or not resuming_failed_cache):
        failed_stages.update(set(FINALIZATION_STAGE_RESULT_KEYS) - completed_stages)
    if resume_record:
        for key in ("trajectory", "usage", "workspace_diff", "tracker_lease_release", "review_highwater", "analysis"):
            finalize_record[key] = resume_record.get(key)
        finalize_record["previous_failed_finalization_preserved"] = True
        finalize_record["started_at"] = resume_record.get("started_at") or finalize_record["started_at"]
        if progress is not None:
            finalize_record["resumed_from_persistent_stage_progress"] = True

    def checkpoint(stage: str | None = None) -> None:
        if not progress_enabled:
            return
        if stage is not None:
            if any(error.get("stage") == stage for error in finalize_record["errors"]):
                completed_stages.discard(stage)
                artifact_digests.pop(stage, None)
            else:
                completed_stages.add(stage)
                artifact_digests[stage] = _terminal_archive_stage_artifact_digests(run_dir, stage, finalize_record)
        atomic_write_json(artifacts_dir / TERMINAL_ARCHIVE_FINALIZATION_PROGRESS_FILENAME, {
            "schema_version": 1, "identity": progress_identity, "record": finalize_record,
            "completed_stages": sorted(completed_stages), "artifact_sha256_by_stage": artifact_digests,
        })

    checkpoint()

    def perform_stage(stage: str) -> bool:
        return not resume_record or stage in failed_stages

    try:
        if perform_stage("trajectory_capture"):
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

    checkpoint("trajectory_capture")
    try:
        if perform_stage("usage_summary") or "trajectory_capture" in failed_stages:
            finalize_record["usage"] = _write_usage_summary(run_dir)
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "usage_summary",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    checkpoint("usage_summary")
    before_path = artifacts_dir / "before-workspace-snapshot.json"
    after_path = artifacts_dir / "after-workspace-snapshot.json"
    if not perform_stage("workspace_diff"):
        pass
    elif resume_record or (event or {}).get("review_run_skip_live_workspace_snapshot"):
        finalize_record["workspace_diff"] = {
            "status": "unavailable", "reason": "delayed_terminal_archive_recovery" if resume_record else "writers_were_active_at_terminal_observation",
        }
    elif repo is not None and before_path.exists():
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

    checkpoint("workspace_diff")
    try:
        if perform_stage("tracker_lease_release"):
            finalize_record["tracker_lease_release"] = _release_tracker_lease(
                run_dir, repo, decision_kind=decision_kind,
            )
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "tracker_lease_release",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    checkpoint("tracker_lease_release")
    try:
        if perform_stage("review_highwater"):
            finalize_record["review_highwater"] = _advance_review_highwater(
                run_dir, repo, event, summary, finalize_record
            )
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "review_highwater",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    checkpoint("review_highwater")
    finalize_record["completed_at"] = _utc_now()

    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        merged = _read_summary(summary_path)
        merged["finalize"] = finalize_record
        try:
            merge_review_run_summary(run_dir, {"run_id": summary.get("run_id") or run_dir.name, "finalize": finalize_record})
        except OSError as exc:
            finalize_record["errors"].append(
                {"stage": "summary_merge", "error": f"{type(exc).__name__}: {exc}"}
            )

    try:
        if perform_stage("analysis_scaffold") or {"trajectory_capture", "usage_summary", "workspace_diff"} & failed_stages:
            # 正式分析已经完成时保持其原始事实；更正信息留在独立恢复记录中。
            analysis_status = _read_summary(artifacts_dir / "analysis/.analyze-thread.status.json")
            if analysis_status.get("formal_analysis_completion_status") != "completed":
                finalize_record["analysis"] = _scaffold_analysis(run_dir)
    except Exception as exc:
        finalize_record["errors"].append(
            {
                "stage": "analysis_scaffold",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )

    checkpoint("analysis_scaffold")
    if decision_kind not in NON_REVIEW_COMPLETION_KINDS:
        try:
            freeze_completion_notification(run_dir, finalize_record)
        except Exception as exc:
            finalize_record["errors"].append(
                {"stage": "automatic_closeout_completion_freeze", "error": f"{type(exc).__name__}: {exc}"}
            )

    if summary_path.exists():
        merged = _read_summary(summary_path)
        merged["finalize"] = finalize_record
        merged["finalize_completed_at"] = finalize_record["completed_at"]
        try:
            merge_review_run_summary(run_dir, {
                "run_id": summary.get("run_id") or run_dir.name, "finalize": finalize_record,
                "finalize_completed_at": finalize_record["completed_at"],
            })
        except OSError as exc:
            finalize_record["errors"].append(
                {"stage": "summary_merge", "error": f"{type(exc).__name__}: {exc}"}
            )

    try:
        atomic_write_json(lock_path, finalize_record)
    except OSError as exc:
        finalize_record["errors"].append(
            {"stage": "lock_write", "error": f"{type(exc).__name__}: {exc}"}
        )

    if decision_kind not in NON_REVIEW_COMPLETION_KINDS:
        publish_completion(run_dir, finalize_record)
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
