#!/usr/bin/env python3
"""只读实测：同一 worktree 多 agent 会话并发的频率，与「时间窗归属」在现时脏 worktree 上的判定分布。

回答 08ac4 计划步骤 5 的闸门问题：时间窗（文件改动时刻落在本会话某次可能写文件的调用期间、且同一时刻
同一 worktree 没有别的会话在调工具）能不能单独当归属证据。

两部分：

1. **历史部分（只读 transcript）**：对 08ac4 统计输出里 410 个可判定 run，取它们的归属窗口
   ``[window_start_utc, run_started_at_utc]``，用新的 core 积木枚举同一 worktree 的其他会话，统计
   - 窗口内有其他会话调用过工具的 run 占比；
   - 其他会话的「可能写文件」调用与本会话的「可能写文件」调用在时间上重叠的 run 占比；
   - 上面两项在 92 个漏审 run 里的占比。
   历史文件的改动时刻已经拿不到，所以历史部分只能回答「并发有多常见」，不能回放四类判定。
2. **现时部分**：对 ``~/.cline/worktrees/*/*`` 下当前有改动、且能找到最近活动会话的 worktree，把最近活动的
   那个会话当作「本会话」，跑 ``build_manifest``（不写 tracker）得到强 / 中证据，再对**本会话可证明写过的路径**
   （Edit/Write/apply_patch、子代理写工具、命令文本）单独跑时间窗判定，看时间窗与强证据是否一致：
   「有争议」占比超过 20% ⇒ 时间窗只做交叉校验、不单独归属（计划步骤 5 的闸门）。

只读：不写 tracker（``tracker_enabled=False``），git 一律带 ``GIT_OPTIONAL_LOCKS=0``（``git status`` 不顺手刷新
别的任务 worktree 的 index）。输出写到 ``--output-directory``（默认 Kanban 交接目录，持久）。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ["GIT_OPTIONAL_LOCKS"] = "0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.session_scope_allocation import session_change_manifest  # noqa: E402
from core.session_scope_allocation.agent_sessions_that_worked_in_same_worktree_discovery import (  # noqa: E402
    discover_agent_sessions_that_worked_in_same_worktree,
)
from core.session_scope_allocation.shell_command_named_write_target_paths_extraction import (  # noqa: E402
    analyze_shell_command_named_write_targets,
)
from core.session_scope_allocation.agent_transcript_tool_call_time_windows_extraction import (  # noqa: E402
    AgentSessionToolCallTimeWindows,
    extract_agent_session_tool_call_time_windows,
    iso_timestamp_to_epoch_ms,
)
from core.session_scope_allocation.layered_session_write_attribution_for_unattributed_dirty_paths import (  # noqa: E402
    EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION,
    UNCOVERED_REASON_CONTESTED,
    UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE,
    classify_dirty_paths_by_file_change_time_and_tool_call_windows_without_registering,
)

# 时间窗判定里「只落在本会话调用期间」的结论：与强证据一致即为「判对」。
TIME_WINDOW_VERDICT_ONLY_THIS_SESSION = UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE

DEFAULT_STATISTICS_INPUT_PATH = Path(
    "/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/"
    "from-task-08ac4-rvf-session-owned-attribution-coverage-gap-history-statistics-output-2026-10-04-revised-after-rvf-review/"
    "per-run-and-per-missed-path-statistics.json"
)
DEFAULT_OUTPUT_DIRECTORY = Path(
    "/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/"
    "from-task-08ac4-time-window-attribution-feasibility-measurement-2026-10-05"
)
KANBAN_TASK_WORKTREES_ROOT = Path.home() / ".cline" / "worktrees"
# EXPERIMENT_NODE: 「有争议」占比超过多少就让时间窗退为只做交叉校验（计划步骤 5 的闸门）。
CONTESTED_SHARE_THRESHOLD_FOR_DOWNGRADING_TIME_WINDOW_TO_CROSS_CHECK_ONLY = 0.20
STRONG_OR_MEDIUM_EVIDENCE_LABELS = {
    "apply_patch",
    "claude_write",
    "exec_command",
    "file_write_tool_call_by_subagent",
    "file_write_tool_call_in_main_session_transcript",
    "shell_command_named_write_target",
}


def _utc_text_to_epoch_ms(text: str) -> int | None:
    if not text:
        return None
    return iso_timestamp_to_epoch_ms(text if text.endswith("Z") or "+" in text else text + "Z")


_SHELL_COMMAND_IS_READ_ONLY_CACHE: dict[tuple[str, str | None], bool] = {}


def _possibly_writing_windows_excluding_read_only_shell_commands(collected: AgentSessionToolCallTimeWindows) -> list[Any]:
    """与归属侧同口径（复核 M2）：已知只读工具、子代理容器、整条只读的 shell 命令都不算可能写入。"""
    windows = []
    for window in collected.possibly_writing_windows():
        if window.shell_command_text:
            key = (window.shell_command_text, window.shell_working_directory)
            if key not in _SHELL_COMMAND_IS_READ_ONLY_CACHE:
                _SHELL_COMMAND_IS_READ_ONLY_CACHE[key] = analyze_shell_command_named_write_targets(
                    window.shell_command_text, window.shell_working_directory
                ).has_no_possible_write_effect
            if _SHELL_COMMAND_IS_READ_ONLY_CACHE[key]:
                continue
        windows.append(window)
    return windows


def _possibly_writing_intervals(collected: AgentSessionToolCallTimeWindows, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    intervals = []
    for window in _possibly_writing_windows_excluding_read_only_shell_commands(collected):
        window_end = window.ended_at_epoch_ms if window.ended_at_epoch_ms is not None else end_ms
        if window_end < start_ms or window.started_at_epoch_ms > end_ms:
            continue
        intervals.append((max(window.started_at_epoch_ms, start_ms), min(window_end, end_ms)))
    return intervals


def _merged(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _overlap_milliseconds(first: list[tuple[int, int]], second: list[tuple[int, int]]) -> int:
    total = 0
    for start_a, end_a in _merged(first):
        for start_b, end_b in _merged(second):
            total += max(0, min(end_a, end_b) - max(start_a, start_b))
    return total


def measure_history(statistics_input_path: Path) -> dict[str, Any]:
    statistics = json.loads(statistics_input_path.read_text(encoding="utf-8"))
    runs = [row for row in statistics["run_rows"] if row.get("write_origin_analyzable")]
    windows_cache: dict[str, AgentSessionToolCallTimeWindows] = {}

    def windows_of(transcript_path: str) -> AgentSessionToolCallTimeWindows:
        if transcript_path not in windows_cache:
            windows_cache[transcript_path] = extract_agent_session_tool_call_time_windows(Path(transcript_path))
        return windows_cache[transcript_path]

    per_run: list[dict[str, Any]] = []
    for row in runs:
        end_ms = _utc_text_to_epoch_ms(row.get("run_started_at_utc") or "")
        transcript_path = row.get("transcript_path")
        repository = row.get("repository")
        if end_ms is None or not transcript_path or not repository:
            per_run.append({"run_id": row["run_id"], "measurable": False, "host_kind": row.get("host_kind")})
            continue
        own = windows_of(transcript_path)
        # 统计脚本里 ``window_start_utc`` 为空 = 这是该 transcript 上的第一个 run，窗口从会话开头算起。
        start_ms = _utc_text_to_epoch_ms(row.get("window_start_utc") or "") or own.first_activity_epoch_ms
        if start_ms is None:
            per_run.append({"run_id": row["run_id"], "measurable": False, "host_kind": row.get("host_kind")})
            continue
        own_intervals = _possibly_writing_intervals(own, start_ms, end_ms)
        discovery = discover_agent_sessions_that_worked_in_same_worktree(
            Path(repository),
            activity_lower_bound_epoch_ms=start_ms,
            excluded_transcript_paths=[transcript_path, *own.subagent_transcript_paths_read],
        )
        other_sessions_active: list[dict[str, Any]] = []
        other_intervals_all: list[tuple[int, int]] = []
        own_call_ids = own.call_ids
        for session in discovery.sessions:
            if own.session_id and session.session_id == own.session_id:
                continue
            other = windows_of(session.transcript_path)
            intervals = [
                interval
                for window, interval in (
                    (window, (max(window.started_at_epoch_ms, start_ms), min(window.ended_at_epoch_ms or end_ms, end_ms)))
                    for window in _possibly_writing_windows_excluding_read_only_shell_commands(other)
                    if not (window.call_id and window.call_id in own_call_ids)
                    and (window.ended_at_epoch_ms or end_ms) >= start_ms
                    and window.started_at_epoch_ms <= end_ms
                )
            ]
            if intervals:
                other_intervals_all.extend(intervals)
                other_sessions_active.append(
                    {
                        "host_kind": session.host_kind,
                        "session_id": session.session_id,
                        "is_same_kanban_task": session.is_same_kanban_task_as_target_worktree,
                        "possibly_writing_call_count_in_window": len(intervals),
                    }
                )
        overlap_ms = _overlap_milliseconds(own_intervals, other_intervals_all)
        own_total_ms = sum(end - start for start, end in _merged(own_intervals))
        per_run.append(
            {
                "run_id": row["run_id"],
                "measurable": True,
                "host_kind": row.get("host_kind"),
                "kanban_task_id": row.get("kanban_task_id"),
                "missed_session_written_path_count": row.get("missed_session_written_path_count", 0),
                "attribution_window_hours": round((end_ms - start_ms) / 3_600_000, 2),
                "own_possibly_writing_call_milliseconds": own_total_ms,
                "other_sessions_with_possibly_writing_calls_in_window": other_sessions_active,
                "own_and_other_possibly_writing_overlap_milliseconds": overlap_ms,
                "discovery_incomplete_reasons": discovery.discovery_incomplete_reasons,
            }
        )

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        measurable = [row for row in rows if row.get("measurable")]
        with_other = [row for row in measurable if row["other_sessions_with_possibly_writing_calls_in_window"]]
        with_overlap = [row for row in measurable if row["own_and_other_possibly_writing_overlap_milliseconds"] > 0]
        with_other_same_task = [
            row for row in with_other if any(session["is_same_kanban_task"] for session in row["other_sessions_with_possibly_writing_calls_in_window"])
        ]
        own_ms = sum(row["own_possibly_writing_call_milliseconds"] for row in measurable)
        overlap_ms = sum(row["own_and_other_possibly_writing_overlap_milliseconds"] for row in measurable)
        return {
            "run_count": len(rows),
            "measurable_run_count": len(measurable),
            "runs_with_other_session_possibly_writing_calls_in_window": len(with_other),
            "runs_with_other_session_in_same_kanban_task": len(with_other_same_task),
            "runs_with_own_and_other_possibly_writing_call_overlap": len(with_overlap),
            "own_possibly_writing_call_time_share_overlapped_by_other_sessions": round(overlap_ms / own_ms, 4) if own_ms else None,
        }

    missed = [row for row in per_run if row.get("missed_session_written_path_count", 0) > 0]
    by_host: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in per_run:
        by_host[str(row.get("host_kind"))].append(row)
    return {
        "all_analyzable_runs": aggregate(per_run),
        "runs_with_missed_review": aggregate(missed),
        "by_host_kind": {host: aggregate(rows) for host, rows in sorted(by_host.items())},
        "per_run": per_run,
    }


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, check=False).stdout


def measure_current_worktrees() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    per_worktree: list[dict[str, Any]] = []
    verdict_counts_for_provably_written_paths: collections.Counter = collections.Counter()
    verdict_counts_for_unattributed_paths: collections.Counter = collections.Counter()
    for worktree in sorted(path for path in KANBAN_TASK_WORKTREES_ROOT.glob("*/*") if (path / ".git").exists()):
        if not _git(worktree, "status", "--porcelain=v1", "-uall").strip():
            continue
        discovery = discover_agent_sessions_that_worked_in_same_worktree(
            worktree.resolve(), activity_lower_bound_epoch_ms=now_ms - 3 * 24 * 3600 * 1000
        )
        if not discovery.sessions:
            continue
        own_session = max(discovery.sessions, key=lambda session: session.last_activity_epoch_ms)
        transcript = Path(own_session.transcript_path)
        try:
            manifest = session_change_manifest.build_manifest(
                worktree, transcript, tracker_enabled=False, enable_layered_session_write_attribution=True
            )
        except Exception as exc:  # noqa: BLE001 - 实测脚本：单个 worktree 失败不影响其它
            per_worktree.append({"worktree": str(worktree), "error": f"{type(exc).__name__}: {exc}"})
            continue
        layered_by_path = {item["path"]: item["evidence"] for item in manifest.get("attribution_evidence", [])}
        main_loop_owned = set(manifest.get("owned_dirty_paths", [])) - set(layered_by_path)
        provably_written = {path for path in main_loop_owned} | {
            path for path, evidence in layered_by_path.items() if evidence in STRONG_OR_MEDIUM_EVIDENCE_LABELS
        }
        verdicts = classify_dirty_paths_by_file_change_time_and_tool_call_windows_without_registering(
            repository_root=worktree.resolve(), main_transcript_path=transcript, dirty_paths=provably_written
        ) if provably_written else {}
        provable_counter = collections.Counter(entry["verdict"] for entry in verdicts.values())
        verdict_counts_for_provably_written_paths.update(provable_counter)
        unattributed_counter = collections.Counter()
        for reason, entries in (manifest.get("uncovered_dirty_paths_by_reason") or {}).items():
            unattributed_counter[reason] += len(entries)
        unattributed_counter[EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION] += sum(
            1
            for evidence in layered_by_path.values()
            if evidence == EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION
        )
        verdict_counts_for_unattributed_paths.update(unattributed_counter)
        per_worktree.append(
            {
                "worktree": str(worktree),
                "own_session": own_session.to_dict(),
                "other_session_count": len(discovery.sessions) - 1,
                "dirty_path_count": len(set(manifest.get("owned_dirty_paths", [])) | set(manifest.get("unattributed_dirty_paths", []))),
                "main_loop_owned_dirty_path_count": len(main_loop_owned),
                "layered_attribution_counts": dict(collections.Counter(layered_by_path.values())),
                "time_window_verdicts_for_provably_written_paths": dict(provable_counter),
                "verdicts_for_paths_unattributed_after_layers_1_2": dict(unattributed_counter),
                "layered_attribution_elapsed_seconds": (manifest.get("layered_session_write_attribution") or {}).get("elapsed_seconds"),
            }
        )
    provable_total = sum(verdict_counts_for_provably_written_paths.values())
    contested_share = (
        verdict_counts_for_provably_written_paths.get(UNCOVERED_REASON_CONTESTED, 0) / provable_total if provable_total else None
    )
    return {
        "worktree_count": len(per_worktree),
        "time_window_verdicts_for_provably_written_paths": dict(verdict_counts_for_provably_written_paths),
        "contested_share_among_provably_written_paths": round(contested_share, 4) if contested_share is not None else None,
        "agreement_share_among_provably_written_paths": round(
            verdict_counts_for_provably_written_paths.get(TIME_WINDOW_VERDICT_ONLY_THIS_SESSION, 0) / provable_total, 4
        ) if provable_total else None,
        "verdicts_for_paths_unattributed_after_layers_1_2": dict(verdict_counts_for_unattributed_paths),
        "gate_threshold": CONTESTED_SHARE_THRESHOLD_FOR_DOWNGRADING_TIME_WINDOW_TO_CROSS_CHECK_ONLY,
        "gate_downgrade_time_window_to_cross_check_only": (
            contested_share is not None and contested_share > CONTESTED_SHARE_THRESHOLD_FOR_DOWNGRADING_TIME_WINDOW_TO_CROSS_CHECK_ONLY
        ),
        "per_worktree": per_worktree,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--statistics-input", type=Path, default=DEFAULT_STATISTICS_INPUT_PATH)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--skip-current", action="store_true")
    arguments = parser.parse_args()
    started = time.monotonic()
    result: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not arguments.skip_history:
        result["history"] = measure_history(arguments.statistics_input)
    if not arguments.skip_current:
        result["current"] = measure_current_worktrees()
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    output_path = arguments.output_directory / "time-window-attribution-feasibility-measurement.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        key: value
        for key, value in {
            "history_all": result.get("history", {}).get("all_analyzable_runs"),
            "history_missed": result.get("history", {}).get("runs_with_missed_review"),
            "history_by_host": result.get("history", {}).get("by_host_kind"),
            "current": {k: v for k, v in result.get("current", {}).items() if k != "per_worktree"},
        }.items()
        if value
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
