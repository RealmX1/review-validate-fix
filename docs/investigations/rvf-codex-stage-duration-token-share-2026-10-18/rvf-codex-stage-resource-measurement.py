#!/usr/bin/env python3
"""只读采集 RVF 证据；仅向显式指定的调查产物目录写入统计结果。"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import math
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
STAGE_LABELS = {
    "main_prepare": "主 Agent：准备与派发",
    "main_review_coordination": "主 Agent：review 期间协调",
    "main_post_review_combined": "主 Agent：归并、验证修复与交接",
    "external_reviewer": "独立 reviewer（含 fallback）",
    "validation_subagent": "独立验证修复子代理",
    "main_host_auxiliary_subagent": "主会话的宿主辅助子代理",
    "analyze": "Analyze 后台 LLM",
    "finalize": "确定性 finalize（scaffold 前）",
}


def utc_seconds(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso_utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def jsonl_records(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, raw in enumerate(handle, 1):
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield number, value
    except OSError:
        return


def normalized_usage(usage, dialect="codex"):
    """把独立 cache 桶合入输入；reasoning 是输出子集，不再相加。"""
    if dialect == "codex":
        result = {key: int(usage.get(key) or 0) for key in TOKEN_FIELDS}
    else:
        names = {
            "cursor": ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens"),
            "omp": ("input", "cacheRead", "cacheWrite", "output"),
            "kimi": ("inputOther", "inputCacheRead", "inputCacheCreation", "output"),
            "claude": ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"),
        }[dialect]
        noncached, cached, created, output = (int(usage.get(key) or 0) for key in names)
        result = dict(zip(TOKEN_FIELDS, (noncached + cached + created, cached, created, output, 0, noncached + cached + created + output)))
    result["noncached_input_tokens"] = result["input_tokens"] - result["cached_input_tokens"]
    result["noncached_input_plus_output_tokens"] = result["noncached_input_tokens"] + result["output_tokens"]
    return result


def add_usage(usages):
    return {key: sum(value.get(key, 0) for value in usages) for key in (*TOKEN_FIELDS, "noncached_input_tokens", "noncached_input_plus_output_tokens")}


def codex_usage_records(path, start, end):
    """优先逐响应记录；旧版本按相邻累计快照差分，丢弃继承的历史基线。"""
    direct, fallback, seen = [], [], set()
    previous = None
    for line, record in jsonl_records(path):
        timestamp = utc_seconds(record.get("timestamp"))
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "token_usage_record":
            identity = payload.get("response_id") or f"ordinal:{record.get('ordinal', line)}"
            if identity in seen:
                continue
            seen.add(identity)
            if timestamp is not None and start <= timestamp <= end and isinstance(payload.get("usage"), dict):
                direct.append({"timestamp": iso_utc(timestamp), "identity": identity, "line": line, "usage": normalized_usage(payload["usage"]), "method": "per_response_usage"})
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        current = info.get("total_token_usage")
        if not isinstance(current, dict):
            continue
        if previous is None or any(int(current.get(key) or 0) < int(previous.get(key) or 0) for key in TOKEN_FIELDS):
            delta = info.get("last_token_usage")
            method = "last_usage_at_initial_or_reset_snapshot"
        else:
            delta = {key: int(current.get(key) or 0) - int(previous.get(key) or 0) for key in TOKEN_FIELDS}
            method = "cumulative_snapshot_delta"
        previous = current
        if isinstance(delta, dict) and timestamp is not None and start <= timestamp <= end and int(delta.get("total_tokens") or 0) > 0:
            fallback.append({"timestamp": iso_utc(timestamp), "identity": f"snapshot:{line}", "line": line, "usage": normalized_usage(delta), "method": method})
    # 两种表示来自同一批响应，不能相加。
    return direct if direct else fallback


def duration_union(intervals):
    merged = []
    for start, end in sorted((a, b) for a, b in intervals if a is not None and b is not None and b >= a):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def percentile(values, percentile_value):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile_value
    lower, upper = math.floor(index), math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


class Measurement:
    def __init__(self, args):
        self.args = args
        self.cutoff = utc_seconds(args.cutoff_utc)
        self.oldest = self.cutoff - 30 * 86400
        self.components = []
        self.ledger = []
        self.evidence = {}
        self.inventory = []
        self.runs = []
        self.codex_session_paths = list(args.codex_sessions_root.glob("*/*/*/rollout-*.jsonl"))
        self.kimi_session_paths = list(args.kimi_sessions_root.glob("*/session_*"))
        self.codex_children_by_parent = collections.defaultdict(list)
        for path in self.codex_session_paths:
            # 只读首行元数据；新版 collaboration 不再写 collab_agent_spawn_end。
            first = next(jsonl_records(path), (None, {}))[1]
            metadata = first.get("payload") or {}
            source = metadata.get("source") or {}
            spawn = (source.get("subagent") or {}).get("thread_spawn") if isinstance(source, dict) else None
            if isinstance(spawn, dict) and spawn.get("parent_thread_id"):
                self.codex_children_by_parent[spawn["parent_thread_id"]].append((path, metadata))

    def record_evidence(self, path):
        if path is None or str(path) in self.evidence or not path.is_file():
            return
        raw = path.read_bytes()
        self.evidence[str(path)] = {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def codex_session(self, session_id):
        candidates = [p for p in self.codex_session_paths if session_id in p.name]
        return next((p for p in candidates if p.name.endswith(f"-{session_id}.jsonl")), candidates[0] if candidates else None)

    def component(self, run, stage, component_id, source, usage_records=None, start=None, end=None, **extra):
        self.record_evidence(source)
        row = {"run_id": run["run_id"], "project": run["project"], "task_id": run["task_id"], "stage": stage,
               "component_id": component_id, "source": str(source) if source else None,
               "started_at_utc": iso_utc(start), "ended_at_utc": iso_utc(end),
               "wall_seconds": round(end - start, 6) if start is not None and end is not None and end >= start else None,
               "token_measurement_status": "observed" if usage_records is not None else "unknown",
               "usage": add_usage([record["usage"] for record in usage_records]) if usage_records is not None else None,
               "usage_record_count": len(usage_records) if usage_records is not None else None, **extra}
        self.components.append(row)
        for record in usage_records or []:
            self.ledger.append({"run_id": run["run_id"], "component_id": component_id, "stage": stage, "source": str(source), **record})
        return row

    def reviewer(self, run, directory, events):
        summary_path = directory / "reviewer.summary.json"
        summary = read_json(summary_path)
        source = directory / "reviewer.stdout.txt"
        rows = list(jsonl_records(source))
        dialect = summary.get("output_format", "")
        endings = [r for r in events if r.get("component") == "reviewer" and r.get("reviewer_id") == directory.name and r.get("event") in ("completed", "failed", "timeout")]
        ending = endings[-1] if endings else {}
        end = utc_seconds(ending.get("timestamp"))
        start = None
        records, provider = None, directory.name
        result_rows = [(n, r) for n, r in rows if r.get("type") == "result" and isinstance(r.get("usage"), dict)]
        if "cursor" in dialect and result_rows and end is not None:
            line, result = result_rows[-1]
            records = [{"identity": result.get("request_id") or f"result:{line}", "line": line, "timestamp": iso_utc(end), "usage": normalized_usage(result["usage"], "cursor"), "method": "terminal_result"}]
            duration = result.get("duration_ms")
            start = end - duration / 1000 if end is not None and isinstance(duration, (int, float)) else None
        elif "oh_my_pi" in dialect:
            records = []
            for line, event in rows:
                message = event.get("message") or {}
                if event.get("type") == "session":
                    start = utc_seconds(event.get("timestamp"))
                if event.get("type") == "message_end" and message.get("role") == "assistant" and isinstance(message.get("usage"), dict):
                    timestamp = utc_seconds(message.get("timestamp"))
                    if timestamp is None or timestamp <= self.cutoff:
                        records.append({"identity": message.get("id") or f"message_end:{line}", "line": line, "timestamp": iso_utc(timestamp), "usage": normalized_usage(message["usage"], "omp"), "method": "assistant_message_end"})
        elif "claude" in dialect and result_rows and end is not None:
            line, result = result_rows[-1]
            records = [{"identity": result.get("session_id") or f"result:{line}", "line": line, "timestamp": iso_utc(end), "usage": normalized_usage(result["usage"], "claude"), "method": "terminal_result"}]
            start = end - result.get("duration_ms", 0) / 1000 if end is not None else None
        elif "kimi" in dialect:
            hints = [r.get("session_id") for _, r in rows if r.get("type") == "session.resume_hint"]
            session = next((p for p in self.kimi_session_paths if hints and p.name == hints[-1]), None)
            if session is None:
                # 超时退出来不及打印 resume_hint 时，用 cwd、创建时间与精确 prompt 路径交叉定位。
                possible = []
                runner_start = utc_seconds(summary.get("created_at"))
                for candidate in self.kimi_session_paths:
                    state = read_json(candidate / "state.json")
                    created = utc_seconds(state.get("createdAt"))
                    if state.get("cwd") != summary.get("cwd") or created is None or runner_start is None or abs(created - runner_start) > 180:
                        continue
                    wire = candidate / "agents/main/wire.jsonl"
                    if wire.is_file() and str(directory / "reviewer.prompt.txt") in wire.read_text(errors="replace"):
                        possible.append(candidate)
                if len(possible) == 1:
                    session = possible[0]
            if session:
                source = session / "agents/main/wire.jsonl"
                records = []
                for wire in sorted((session / "agents").glob("*/wire.jsonl")):
                    self.record_evidence(wire)
                    for line, event in jsonl_records(wire):
                        timestamp = utc_seconds(event.get("time") or event.get("created_at"))
                        if event.get("type") == "agent.turn.started" and start is None:
                            start = timestamp
                        if event.get("type") == "usage.record" and isinstance(event.get("usage"), dict) and timestamp is not None and timestamp <= self.cutoff:
                            records.append({"identity": f"{wire.parent.name}:usage:{line}", "line": line, "timestamp": iso_utc(timestamp), "usage": normalized_usage(event["usage"], "kimi"), "method": "kimi_wire_usage_record", "wire_source": str(wire)})
        if end is not None and end > self.cutoff:
            end = self.cutoff
        if start is None:
            # summary.created_at 写在 reviewer runner 入口；它包含健康探测与启动开销。
            start = utc_seconds(summary.get("created_at"))
        row = self.component(run, "external_reviewer", directory.name, source, records, start, end,
                             provider=provider, output_format=dialect, returncode=ending.get("returncode"),
                             duration_boundary="provider_duration_or_session_start" if "cursor" in dialect or "oh_my_pi" in dialect or "kimi" in dialect else "runner_entry_to_terminal",
                             terminal_event_id=ending.get("event_id"))
        self.record_evidence(summary_path)
        return row

    def measure_codex_descendants(self, run, parent_id, start, finish, stage="validation_subagent"):
        seen, discovered = set(), []

        def visit(thread_id):
            for child, metadata in self.codex_children_by_parent.get(thread_id, []):
                agent_id = metadata.get("id")
                spawned = utc_seconds(metadata.get("timestamp"))
                if not agent_id or agent_id in seen or spawned is None or not start <= spawned <= finish:
                    continue
                seen.add(agent_id)
                records = codex_usage_records(child, spawned, finish)
                terminal_times = [utc_seconds(r.get("timestamp")) for _, r in jsonl_records(child)
                                  if isinstance(r.get("payload"), dict) and r["payload"].get("type") == "task_complete"
                                  and utc_seconds(r.get("timestamp")) is not None and spawned <= utc_seconds(r.get("timestamp")) <= finish]
                last_usage = max((utc_seconds(r["timestamp"]) for r in records), default=spawned)
                ended = max(terminal_times) if terminal_times else last_usage
                effective_stage = "main_host_auxiliary_subagent" if stage == "validation_subagent" and str(metadata.get("agent_path", "")).endswith("/skill_learner") else stage
                self.component(run, effective_stage, agent_id, child, records, spawned, ended,
                               agent_path=metadata.get("agent_path"), parent_thread_id=thread_id,
                               duration_boundary="spawn_to_task_complete" if terminal_times else "spawn_to_last_usage_lower_bound")
                discovered.append(agent_id)
                visit(agent_id)

        if parent_id:
            visit(parent_id)
        return discovered

    def analyze(self, run, directory):
        analysis = directory / "artifacts/analysis"
        status_path = analysis / ".analyze-thread.status.json"
        status = read_json(status_path)
        self.record_evidence(status_path)
        if not status:
            run["analyze_status"] = "not_launched_or_unrecorded"
            return
        start, end = utc_seconds(status.get("started_at")), utc_seconds(status.get("finished_at"))
        log_path = analysis / ".analyze-thread.log"
        self.record_evidence(log_path)
        text = log_path.read_text(errors="replace") if log_path.is_file() else ""
        match = re.search(r"^session id:\s*(\S+)", text, re.M)
        source = self.codex_session(match.group(1)) if match else None
        records = codex_usage_records(source, start, min(end or self.cutoff, self.cutoff)) if source and start else None
        summary = analysis / "summary.md"
        body = summary.read_text(errors="replace") if summary.is_file() else ""
        self.record_evidence(summary)
        causality = read_json(analysis / "causality.json")
        self.record_evidence(analysis / "causality.json")
        run["analyze_status"] = "completed" if end is not None and end <= self.cutoff else "running_at_cutoff"
        run["analyze_returncode"] = status.get("returncode") if run["analyze_status"] == "completed" else None
        run["analyze_formal_archive_complete_at_inspection"] = bool(body and "TODO(rvf-analyze)" not in body and causality)
        run["analyze_formal_archive_todo_count"] = body.count("TODO(rvf-analyze)")
        run["analyze_session_id"] = match.group(1) if match else None
        run["analyze_footer_tokens_used"] = int(re.search(r"tokens used\s*\n([\d,]+)", text).group(1).replace(",", "")) if re.search(r"tokens used\s*\n([\d,]+)", text) else None
        run["analyze_wall_seconds"] = end - start if start is not None and end is not None and end <= self.cutoff else None
        run["analyze_finished_at_utc"] = iso_utc(end) if end and end <= self.cutoff else None
        finalized = utc_seconds(run.get("finalize_completed_at_utc"))
        run["scaffold_and_analyze_dispatch_gap_seconds"] = start - finalized if start is not None and finalized is not None else None
        self.component(run, "analyze", "analyze", source or log_path, records, start, min(end or self.cutoff, self.cutoff),
                       returncode=run["analyze_returncode"], session_id=run["analyze_session_id"],
                       formal_archive_complete=run["analyze_formal_archive_complete_at_inspection"])
        if match and start:
            self.measure_codex_descendants(run, match.group(1), start, min(end or self.cutoff, self.cutoff), stage="analyze")

    def measure_run(self, directory, summary, host_manifest, session_manifest, event_rows):
        start = datetime.strptime(directory.name[4:20], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        repo = summary.get("repo") or session_manifest.get("repo") or ""
        match = re.search(r"/\.cline/worktrees/([^/]+)/([^/]+)", repo)
        if not match:
            self.inventory.append({"run_id": directory.name, "classification": "not_confirmed_cline_kanban_task", "repo": repo})
            return
        finalize = summary.get("finalize") or {}
        completed = utc_seconds(finalize.get("completed_at") or summary.get("finalize_completed_at"))
        finish = min(completed or self.cutoff, self.cutoff)
        run = {"run_id": directory.name, "project": match.group(2), "task_id": match.group(1), "run_dir": str(directory),
               "started_at_utc": iso_utc(start), "finalize_completed_at_utc": iso_utc(completed) if completed and completed <= self.cutoff else None,
               "finalized_at_cutoff": completed is not None and completed <= self.cutoff,
               "deployment_label_recorded_in_summary": summary.get("rvf_plugin_deploy"),
               "start_boundary": "run_id_creation_time_second_precision",
               "main_wall_seconds": finish - start, "unknown_cost_components": [], "health_probe_attempt_count": 0}
        events = [r for _, r in event_rows if utc_seconds(r.get("timestamp")) is not None and utc_seconds(r.get("timestamp")) <= self.cutoff]
        for path in [directory / "summary.json", directory / "events.jsonl", directory / "artifacts/session-manifest.json", directory / "artifacts/trajectory/rvf/rollout.manifest.json"]:
            self.record_evidence(path)
        reviewer_rows = [self.reviewer(run, q.parent, events) for q in sorted((directory / "artifacts/reviewers").glob("*/reviewer.summary.json"))]
        reviewer_starts = [utc_seconds(r["started_at_utc"]) for r in reviewer_rows if r["started_at_utc"]]
        reviewer_ends = [utc_seconds(r["ended_at_utc"]) for r in reviewer_rows if r["ended_at_utc"]]
        first_review = min(reviewer_starts) if reviewer_starts else finish
        last_review = max(reviewer_ends) if reviewer_ends else finish
        first_review, last_review = max(start, min(first_review, finish)), max(start, min(last_review, finish))
        last_review = max(first_review, last_review)
        run["review_window_wall_seconds"] = last_review - first_review
        run["reviewer_process_seconds_sum"] = sum(r["wall_seconds"] or 0 for r in reviewer_rows)
        run["reviewer_process_seconds_union"] = duration_union([(utc_seconds(r["started_at_utc"]), utc_seconds(r["ended_at_utc"])) for r in reviewer_rows])
        run["reviewer_count"] = len(reviewer_rows)
        captured = directory / "artifacts/trajectory/rvf/rollout.jsonl"
        source = captured if captured.is_file() else Path(session_manifest.get("transcript") or host_manifest.get("source_path") or "/nonexistent")
        main_records = codex_usage_records(source, start, finish) if source.is_file() else None
        spans = [("main_prepare", start, first_review), ("main_review_coordination", first_review, last_review), ("main_post_review_combined", last_review, finish)]
        for stage, begin, end in spans:
            selected = [r for r in main_records if begin <= utc_seconds(r["timestamp"]) < end or (stage == "main_post_review_combined" and utc_seconds(r["timestamp"]) == end)] if main_records is not None else None
            self.component(run, stage, stage, source if source.is_file() else None, selected, begin, end)
        run["main_token_records"] = len(main_records) if main_records is not None else None
        # 子代理只取本轮实际 spawn；历史会话中出现过的代理不能全量归入本轮。
        agents = []
        main_metadata = next(jsonl_records(source), (None, {}))[1].get("payload", {}) if source.is_file() else {}
        descendant_ids = self.measure_codex_descendants(run, main_metadata.get("id"), start, finish)
        if source.is_file():
            for line, record in jsonl_records(source):
                payload = record.get("payload") or {}
                ts = utc_seconds(record.get("timestamp"))
                if isinstance(payload, dict) and payload.get("type") == "collab_agent_spawn_end" and ts is not None and start <= ts <= finish:
                    agents.append((payload.get("new_thread_id"), ts, payload.get("call_id")))
        for agent_id, spawned, call_id in agents:
            if not agent_id or agent_id in descendant_ids:
                continue
            child = self.codex_session(agent_id)
            usage = codex_usage_records(child, spawned, finish) if child else None
            child_end = max((utc_seconds(r["timestamp"]) for r in usage), default=spawned) if usage is not None else None
            self.component(run, "validation_subagent", agent_id, child, usage, spawned, child_end, spawn_call_id=call_id,
                           duration_boundary="spawn_to_last_usage_lower_bound")
        run["spawned_subagent_count"] = len(set(descendant_ids) | {a[0] for a in agents if a[0]})
        finalized_start = utc_seconds(finalize.get("started_at"))
        if run["finalized_at_cutoff"] and finalized_start is not None:
            self.component(run, "finalize", "deterministic_finalize", directory / "summary.json", [], finalized_start, completed,
                           duration_boundary="completed_at_is_written_before_analysis_scaffold")
        for event in events:
            if event.get("event") in ("health_completed", "pre_run_health_completed"):
                run["health_probe_attempt_count"] += 1
                # 当前事件只存退出码与命令，未归档这些独立调用的 usage。
                run["unknown_cost_components"].append("health_probe:" + event.get("event_id", "unknown"))
        repeated_reviewers = collections.Counter(event.get("reviewer_id") for event in events if event.get("component") == "reviewer" and event.get("event") == "completed")
        for reviewer_id, count in repeated_reviewers.items():
            if count > 1:
                run["unknown_cost_components"].append(f"overwritten_reviewer_attempt:{reviewer_id}:{count}")
        self.analyze(run, directory)
        existing = read_json(directory / "artifacts/usage/usage-summary.json")
        self.record_evidence(directory / "artifacts/usage/usage-summary.json")
        run["existing_usage_summary_total_tokens"] = existing.get("total_tokens")
        run["existing_usage_summary_started_at"] = existing.get("started_at")
        run["existing_usage_starts_before_rvf"] = utc_seconds(existing.get("started_at")) is not None and utc_seconds(existing.get("started_at")) < start
        owned = [r for r in self.components if r["run_id"] == run["run_id"]]
        known = [r["usage"] for r in owned if r["usage"] is not None]
        run["observed_usage"] = add_usage(known)
        analyze_components = [r["usage"] for r in owned if r["stage"] == "analyze" and r["usage"] is not None]
        analyze_usage = add_usage(analyze_components) if analyze_components else None
        run["analyze_usage"] = analyze_usage
        run["missing_usage_component_ids"] = [r["component_id"] for r in owned if r["usage"] is None]
        run["principal_component_tokens_complete"] = not run["missing_usage_component_ids"] and main_records is not None
        run["all_component_tokens_complete"] = run["principal_component_tokens_complete"] and not run["unknown_cost_components"]
        if run.get("analyze_finished_at_utc"):
            run["full_wall_seconds"] = utc_seconds(run["analyze_finished_at_utc"]) - start
            run["analyze_elapsed_share"] = run["analyze_wall_seconds"] / run["full_wall_seconds"]
        if analyze_usage and run["observed_usage"]["total_tokens"]:
            run["analyze_observed_total_token_share"] = analyze_usage["total_tokens"] / run["observed_usage"]["total_tokens"]
            run["analyze_observed_noncached_plus_output_share"] = analyze_usage["noncached_input_plus_output_tokens"] / run["observed_usage"]["noncached_input_plus_output_tokens"] if run["observed_usage"]["noncached_input_plus_output_tokens"] else None
        self.runs.append(run)
        self.inventory.append({"run_id": directory.name, "classification": "included_codex_cline_kanban_rvf", "project": run["project"], "task_id": run["task_id"]})

    def collect(self):
        for directory in sorted(self.args.runs_root.iterdir()):
            try:
                timestamp = datetime.strptime(directory.name[4:20], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if not self.oldest <= timestamp <= self.cutoff:
                continue
            if not (directory / "artifacts/inputs/scope.contract.json").is_file():
                self.inventory.append({"run_id": directory.name, "classification": "dispatcher_or_no_prepared_scope_contract"})
                continue
            summary = read_json(directory / "summary.json")
            manifest = read_json(directory / "artifacts/trajectory/rvf/rollout.manifest.json")
            session = read_json(directory / "artifacts/session-manifest.json") or read_json(directory / "artifacts/inputs/session-manifest.json")
            events = list(jsonl_records(directory / "events.jsonl"))
            host = manifest.get("host") or ((summary.get("finalize") or {}).get("trajectory") or {}).get("host")
            routing = next((r.get("main_harness") for _, r in events if r.get("main_harness")), None)
            if not host:
                host = routing
            if not host:
                transcript = session.get("transcript") or ""
                host = "codex" if "/.codex/sessions/" in transcript else None
            if host != "codex":
                self.inventory.append({"run_id": directory.name, "classification": "other_host" if host else "unknown_host", "host": host})
                continue
            print("采集", directory.name, flush=True)
            self.measure_run(directory, summary, manifest, session, events)

    def aggregate(self, runs):
        completed = [r for r in runs if r["finalized_at_cutoff"] and r.get("analyze_status") == "completed"]
        principal = [r for r in completed if r["principal_component_tokens_complete"]]
        principal_ids = {r["run_id"] for r in principal}
        stages = {}
        for stage, label in STAGE_LABELS.items():
            rows = [r for r in self.components if r["run_id"] in principal_ids and r["stage"] == stage]
            stages[stage] = {"label": label, "component_count": len(rows), "usage": add_usage([r["usage"] for r in rows if r["usage"]]),
                             "process_wall_seconds_sum": sum(r["wall_seconds"] or 0 for r in rows),
                             "process_wall_seconds_median": percentile([r["wall_seconds"] for r in rows if r["wall_seconds"] is not None], .5),
                             "process_wall_seconds_p90": percentile([r["wall_seconds"] for r in rows if r["wall_seconds"] is not None], .9)}
        total = add_usage([r["observed_usage"] for r in principal])
        analysis = add_usage([r["analyze_usage"] for r in principal if r["analyze_usage"]])
        wall_runs = [r for r in completed if r.get("full_wall_seconds") is not None]
        return {"run_count": len(runs), "finalized_count": sum(r["finalized_at_cutoff"] for r in runs), "completed_analyze_count": len(completed),
                "principal_complete_run_count": len(principal), "principal_complete_run_ids": sorted(principal_ids),
                "finalized_without_recorded_analyze_count": sum(r["finalized_at_cutoff"] and r.get("analyze_status") == "not_launched_or_unrecorded" for r in runs),
                "unfinished_at_cutoff_count": sum(not r["finalized_at_cutoff"] for r in runs),
                "all_component_complete_run_count": sum(r["all_component_tokens_complete"] for r in completed),
                "principal_complete_coverage_of_completed_analyze": len(principal) / len(completed) if completed else None,
                "formal_archive_complete_analyze_count": sum(r.get("analyze_formal_archive_complete_at_inspection", False) for r in completed),
                "health_probe_attempt_count_in_principal_cohort": sum(r["health_probe_attempt_count"] for r in principal),
                "observed_usage_principal_cohort": total, "analyze_usage_principal_cohort": analysis,
                "analyze_observed_total_token_share": analysis["total_tokens"] / total["total_tokens"] if total["total_tokens"] else None,
                "analyze_observed_noncached_plus_output_share": analysis["noncached_input_plus_output_tokens"] / total["noncached_input_plus_output_tokens"] if total["noncached_input_plus_output_tokens"] else None,
                "per_run_analyze_total_token_share_median": percentile([r["analyze_observed_total_token_share"] for r in principal if "analyze_observed_total_token_share" in r], .5),
                "per_run_analyze_total_token_share_p90": percentile([r["analyze_observed_total_token_share"] for r in principal if "analyze_observed_total_token_share" in r], .9),
                "analyze_wall_seconds_sum": sum(r["analyze_wall_seconds"] for r in wall_runs),
                "full_wall_seconds_sum": sum(r["full_wall_seconds"] for r in wall_runs),
                "main_wall_seconds_sum": sum(r["main_wall_seconds"] for r in wall_runs),
                "analyze_elapsed_share": sum(r["analyze_wall_seconds"] for r in wall_runs) / sum(r["full_wall_seconds"] for r in wall_runs) if wall_runs else None,
                "analyze_wall_seconds_median": percentile([r["analyze_wall_seconds"] for r in wall_runs], .5),
                "analyze_wall_seconds_p90": percentile([r["analyze_wall_seconds"] for r in wall_runs], .9),
                "per_run_analyze_elapsed_share_median": percentile([r["analyze_elapsed_share"] for r in wall_runs], .5),
                "per_run_analyze_elapsed_share_p90": percentile([r["analyze_elapsed_share"] for r in wall_runs], .9),
                "existing_summary_includes_pre_rvf_count": sum(r["existing_usage_starts_before_rvf"] for r in completed), "stages": stages}

    def write(self):
        self.args.output_directory.mkdir(parents=True, exist_ok=True)
        primary = [r for r in self.runs if utc_seconds(r["started_at_utc"]) >= self.cutoff - 7 * 86400]
        report = {"schema_version": 1, "cutoff_utc": iso_utc(self.cutoff), "primary_window_start_utc": iso_utc(self.cutoff - 7 * 86400),
                  "comparison_window_start_utc": iso_utc(self.oldest), "inventory_classification_counts": dict(collections.Counter(r["classification"] for r in self.inventory)),
                  "primary_7_days": self.aggregate(primary), "comparison_30_days": self.aggregate(self.runs),
                  "primary_by_project": {p: self.aggregate([r for r in primary if r["project"] == p]) for p in sorted({r["project"] for r in primary})},
                  "comparison_by_recorded_deployment_label": {str(p): self.aggregate([r for r in self.runs if r["deployment_label_recorded_in_summary"] == p]) for p in sorted({r["deployment_label_recorded_in_summary"] for r in self.runs}, key=str)},
                  "limitations": ["健康探测 usage 未完整归档，主要组件占比是已观测口径，不能称为所有调用的精确占比。", "目录创建时间作为 RVF 起点，精度为秒；不把它之前的实现与发起决策对话计入。", "归并、验证修复、最终验证与交接缺乏可靠阶段事件，合并展示。", "正式归档状态在采集时检查，不伪装成截止时刻的历史快照。", "finalize.completed_at 在 scaffold 之前写入，不能当作 scaffold 完成耗时。", "summary 的部署标签可能在后续操作中重写，仅作为记录标签分组，不能证明每个阶段运行相同代码。", "主 Agent 阶段按 reviewer 活动时间窗归属，属于时间窗分类，不是逐条语义判断。"]}
        outputs = {"rvf-codex-resource-summary.json": report, "rvf-codex-run-component-measurements.json": {"runs": self.runs, "components": self.components},
                   "rvf-codex-response-token-ledger.json.gz": self.ledger, "rvf-codex-source-evidence-manifest.json": self.evidence,
                   "rvf-codex-run-inventory.json": self.inventory}
        for filename, payload in outputs.items():
            output_path = self.args.output_directory / filename
            if filename.endswith(".gz"):
                output_path.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0))
            else:
                output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: report["primary_7_days"][k] for k in ["run_count", "completed_analyze_count", "principal_complete_run_count", "analyze_observed_total_token_share", "analyze_observed_noncached_plus_output_share", "analyze_elapsed_share"]}, ensure_ascii=False))


def self_check():
    assert normalized_usage({"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 110})["total_tokens"] == 110
    for dialect, usage in [("cursor", {"inputTokens": 20, "cacheReadTokens": 80, "outputTokens": 10}), ("omp", {"input": 20, "cacheRead": 80, "output": 10}), ("kimi", {"inputOther": 20, "inputCacheRead": 80, "output": 10})]:
        assert normalized_usage(usage, dialect)["total_tokens"] == 110
    assert duration_union([(0, 10), (5, 20), (30, 35)]) == 25
    assert percentile([1, 2, 3], .5) == 2
    # 专门构造继承基线、重复快照、累计重置和重复 response，防止占比失真。
    with tempfile.TemporaryDirectory(prefix="rvf-resource-measurement-parser-fixture-") as folder:
        path = Path(folder) / "synthetic-rollout.jsonl"
        def snapshot(timestamp, total, last):
            return {"timestamp": timestamp, "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total, "total_tokens": total}, "last_token_usage": {"input_tokens": last, "total_tokens": last}}}}
        path.write_text("\n".join(json.dumps(r) for r in [snapshot("2026-10-18T00:00:00Z", 1000, 10), snapshot("2026-10-18T00:00:01Z", 1015, 15), snapshot("2026-10-18T00:00:02Z", 1015, 15), snapshot("2026-10-18T00:00:03Z", 20, 20)]))
        records = codex_usage_records(path, utc_seconds("2026-10-18T00:00:00Z"), utc_seconds("2026-10-18T00:00:03Z"))
        assert add_usage([r["usage"] for r in records])["total_tokens"] == 45
        response = {"timestamp": "2026-10-18T00:00:01Z", "type": "token_usage_record", "payload": {"response_id": "synthetic-response", "usage": {"input_tokens": 15, "total_tokens": 15}}}
        path.write_text("\n".join(json.dumps(r) for r in [response, response, snapshot("2026-10-18T00:00:02Z", 1015, 15)]))
        records = codex_usage_records(path, utc_seconds("2026-10-18T00:00:00Z"), utc_seconds("2026-10-18T00:00:03Z"))
        assert len(records) == 1 and records[0]["usage"]["total_tokens"] == 15
    print("核算自检通过：继承基线、重复快照、计数重置、响应去重、缓存桶、reasoning 子集、并行时间并集与分位数。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff-utc", default="2026-10-18T23:30:59Z")
    parser.add_argument("--runs-root", type=Path, default=Path.home() / ".rvf/user-local-data/run-logs/runs")
    parser.add_argument("--codex-sessions-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--kimi-sessions-root", type=Path, default=Path.home() / ".kimi-code/sessions")
    parser.add_argument("--output-directory", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    measurement = Measurement(args)
    measurement.collect()
    measurement.write()


if __name__ == "__main__":
    main()
