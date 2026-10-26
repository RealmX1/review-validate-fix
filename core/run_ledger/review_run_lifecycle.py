"""保存独立于组件 summary 写入时刻的审查生命周期。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from core.review_run_qualification_mutation_lock import guard_review_run_qualification_mutation


REVIEW_PHASE_SEQUENCE = ("prepare", "review", "merge", "validate_fix", "verify", "handoff", "complete")
TERMINAL_FIELDS = ("review_run_started_at", "review_run_terminal_status", "review_run_terminal_at")
TIMEZONE_AWARE_REVIEW_RUN_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:[.,](?P<fraction>\d+))?(?:Z|[+-]\d{2}(?::?\d{2})?)"
)


def parse_timezone_aware_review_run_timestamp_utc(value: Any) -> tuple[datetime, Decimal] | None:
    """比较 aware UTC 秒和精确小数秒，避免 datetime 静默截断纳秒；保留原文。"""
    if not isinstance(value, str):
        return None
    match = TIMEZONE_AWARE_REVIEW_RUN_TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        utc_second = moment.astimezone(timezone.utc).replace(microsecond=0)
        fractional_second = Decimal("0." + (match.group("fraction") or "0"))
        return utc_second, fractional_second
    except (TypeError, ValueError, OverflowError):
        return None


def review_run_timestamp_is_within_optional_bounds(
    timestamp: Any, *, window_start: Any = None, window_end: Any = None,
) -> bool:
    """无边界时保留全量证据；指定边界后，未知时刻不取得窗口资格。"""
    if window_start is None and window_end is None:
        return True
    moment = parse_timezone_aware_review_run_timestamp_utc(timestamp)
    start = parse_timezone_aware_review_run_timestamp_utc(window_start)
    end = parse_timezone_aware_review_run_timestamp_utc(window_end)
    if (moment is None or (window_start is not None and start is None)
            or (window_end is not None and end is None)):
        return False
    return (start is None or start <= moment) and (end is None or moment <= end)


def is_review_run_prepare_started_event(event: dict[str, Any]) -> bool:
    """复用真实 prepare 入口的事件合同，不能将其它组件开始当成开审。"""
    return (event.get("component") == "prepare-run" and event.get("phase") == "prepare"
            and event.get("event") == "started" and event.get("status") == "started"
            and event.get("reason_code") == "prepare_started")


@contextmanager
def review_run_metadata_lock(run_directory: Path) -> Iterator[None]:
    """仅锁短暂的元数据提交；崩溃和重启由内核释放，不占用业务 base 锁。"""
    artifacts = run_directory / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / ".review-run-metadata.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"运行元数据必须是对象：{path}")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{secrets.token_hex(8)}.pending")
    try:
        with pending.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def read_review_run_lifecycle(run_directory: Path) -> dict[str, Any]:
    return read_json_object(run_directory / "artifacts/review-run-lifecycle.json")


def review_run_start_evidence(run_directory: Path, summary: dict[str, Any]) -> tuple[str | None, str]:
    """读取不可变起点或精确 run 的 prepare 事件，不把组件更新时间当作替代证据。"""
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None, "unavailable"
    start = summary.get("review_run_started_at")
    if parse_timezone_aware_review_run_timestamp_utc(start) is not None:
        return start, summary.get("review_run_start_evidence_source", "run_summary")
    try:
        lifecycle = read_review_run_lifecycle(run_directory)
    except (OSError, ValueError):
        lifecycle = {}
    if lifecycle and lifecycle.get("run_id") == run_id:
        start = lifecycle.get("review_run_started_at")
        if parse_timezone_aware_review_run_timestamp_utc(start) is not None:
            return start, lifecycle.get("review_run_start_evidence_source", "run_lifecycle")
    events = run_directory / "events.jsonl"
    if events.is_file():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("run_id") != run_id:
                continue
            if (is_review_run_prepare_started_event(event)
                    and parse_timezone_aware_review_run_timestamp_utc(event.get("timestamp")) is not None):
                return event["timestamp"], "archived_prepare_started_event"
    return None, "unavailable"


def _validate_identity(payload: dict[str, Any], run_id: str) -> None:
    if payload and payload.get("run_id") != run_id:
        raise ValueError("审查生命周期的 run 身份不符")


def record_review_run_phase(
    run_directory: Path, *, run_id: str, phase: str, observed_at: str,
    starts_review_run: bool = False, start_evidence_source: str = "prepare_started_event",
) -> dict[str, Any]:
    if phase not in REVIEW_PHASE_SEQUENCE:
        return read_review_run_lifecycle(run_directory)
    with review_run_metadata_lock(run_directory):
        lifecycle = read_review_run_lifecycle(run_directory)
        _validate_identity(lifecycle, run_id)
        if not lifecycle and not starts_review_run:
            return {}
        previous = json.dumps(lifecycle, sort_keys=True)
        if not lifecycle:
            lifecycle = {
                "schema_version": 1, "run_id": run_id,
                "review_run_started_at": observed_at,
                "review_run_start_evidence_source": start_evidence_source,
                "review_run_terminal_status": None, "review_run_terminal_at": None,
                "phase_started_at": {}, "phase": "prepare",
            }
        if not lifecycle.get("review_run_terminal_status"):
            current = lifecycle.get("phase", "prepare")
            if REVIEW_PHASE_SEQUENCE.index(phase) >= REVIEW_PHASE_SEQUENCE.index(current):
                lifecycle["phase_started_at"].setdefault(phase, observed_at)
                lifecycle["phase"] = phase
        if previous != json.dumps(lifecycle, sort_keys=True):
            atomic_write_json(run_directory / "artifacts/review-run-lifecycle.json", lifecycle)
        return lifecycle


def record_review_run_terminal(
    run_directory: Path, *, run_id: str, terminal_status: str, terminal_at: str,
) -> dict[str, Any]:
    with review_run_metadata_lock(run_directory):
        lifecycle = read_review_run_lifecycle(run_directory)
        _validate_identity(lifecycle, run_id)
        if lifecycle.get("review_run_terminal_status"):
            return lifecycle
        if not lifecycle:
            # 缺少开审记录时保持未知，不能以 finalize 时刻冒充开审时刻。
            summary = read_json_object(run_directory / "summary.json")
            if summary:
                _validate_identity(summary, run_id)
            start, source = review_run_start_evidence(run_directory, summary)
            lifecycle = {
                "schema_version": 1, "run_id": run_id, "review_run_started_at": start,
                "review_run_start_evidence_source": source, "phase": "prepare",
                "phase_started_at": {},
            }
        lifecycle["review_run_terminal_status"] = terminal_status
        lifecycle["review_run_terminal_at"] = terminal_at
        if terminal_status == "handoff":
            lifecycle["phase"] = "complete"
        atomic_write_json(run_directory / "artifacts/review-run-lifecycle.json", lifecycle)
        return lifecycle


def project_review_run_lifecycle(
    payload: dict[str, Any], lifecycle: dict[str, Any],
) -> dict[str, Any]:
    if not lifecycle:
        return payload
    _validate_identity(lifecycle, payload["run_id"])
    payload.update({key: lifecycle.get(key) for key in TERMINAL_FIELDS})
    payload["review_run_start_evidence_source"] = lifecycle.get("review_run_start_evidence_source")
    phase = lifecycle["phase"]
    payload["rvf_state_phase"] = phase
    payload["rvf_state_phase_index"] = REVIEW_PHASE_SEQUENCE.index(phase)
    state = dict(payload.get("rvf_state") or {})
    state.update({"phase": phase, "phase_index": REVIEW_PHASE_SEQUENCE.index(phase)})
    payload["rvf_state"] = state
    return payload


@guard_review_run_qualification_mutation("run_directory")
def merge_review_run_summary(
    run_directory: Path, updates: dict[str, Any], *, preserved_keys: set[str] | None = None,
) -> dict[str, Any]:
    """先取得资格锁，再提交元数据，保留终态并与消费者落地互斥。"""
    with review_run_metadata_lock(run_directory):
        path = run_directory / "summary.json"
        previous = read_json_object(path)
        if previous.get("run_id") and updates.get("run_id") and previous["run_id"] != updates["run_id"]:
            raise ValueError("组件 summary 不能改写已绑定的 run 身份")
        if preserved_keys is None:
            merged = {**previous, **updates}
        else:
            merged = dict(updates)
            keys = set(preserved_keys)
            if previous.get("review_run_terminal_status"):
                keys.update(previous)
            for key in keys:
                if key not in merged or merged[key] is None:
                    if previous.get(key) is not None:
                        merged[key] = previous[key]
        lifecycle = read_review_run_lifecycle(run_directory)
        project_review_run_lifecycle(merged, lifecycle)
        atomic_write_json(path, merged)
        return merged


def observe_bound_review_run_component_phase(run_directory: Path, *, phase: str) -> dict[str, Any]:
    """真实组件入口记录阶段；观测 I/O 缺口不阻断已经授权的业务操作。"""
    try:
        summary = read_json_object(run_directory / "summary.json")
        run_id = summary.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return {"status": "unavailable", "reason": "missing_bound_run_identity"}
        lifecycle = record_review_run_phase(run_directory, run_id=run_id, phase=phase,
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        if lifecycle:
            merge_review_run_summary(run_directory, {"run_id": run_id})
        return {"status": "recorded" if lifecycle else "unavailable", "phase": lifecycle.get("phase")}
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
