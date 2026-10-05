"""对 ``build_manifest`` 主循环之后仍无归属的脏路径，分层补判是不是本会话写的。

决定与理由见 08ac4 调查报告 §5.5（2026-10-05 用户两次拍板 + 独立复核 M1–M16）。git diff 仍是「改了什么」的唯一
依据，本模块只回答「是谁写的」。每个仍无归属的脏路径按下表依次判定，先命中者生效：

| 序 | evidence 标签 | 判定 | 强度 |
|---|---|---|---|
| 1 | ``file_write_tool_call_by_subagent`` / ``file_write_tool_call_in_main_session_transcript`` | 子代理（或主循环不认识的宿主格式 / MCP 写工具）的**成功**写工具调用写明了该路径 | 强 |
| 2 | ``shell_command_named_write_target`` | 本会话（含子代理）的 shell 命令按段解析出的写入目标（只认能还原成确切路径的） | 中 |
| 3 | ``agent_explicit_claim`` | agent 用 ``rvf scope claim`` 显式认领、且文件内容与认领时一致 | 强（agent 负责） |
| 4 | ``kanban_single_task_worktree_unowned_dirty_change_default_attribution`` | 只在 Kanban 单任务 worktree：没有任何证据表明别的会话写了它 ⇒ 默认归本会话 | 弱 |

第 4 层的「别的会话写了它」有两种证据，命中任一就不默认认领：

- tracker 里这个路径已被别的会话以强 / 中证据持有；
- 文件的改动时刻（``max(mtime, ctime)``）落在同一 worktree 另一个已知 agent 会话的**可能写入**调用期间
  （只读命令不算，fork / resume 复制来的同一调用不算）。

时间窗**不单独授予归属**（独立复核 M1：它只说明「最后一次被碰时谁有一个开着的调用」，IDE 手改、守护进程、
未知宿主都看不见）。在非单任务 worktree 里，它只给未覆盖路径打一个「可能是你在 hh:mm 的 <cmd> 写的」标签，
帮 agent 决定要不要认领。

判不准的路径不登记归属，分四类写进 manifest 的 ``uncovered_dirty_paths_by_reason``：

- ``contested``：改动时刻同时落在本会话与别的会话的可能写入调用期间；或（非单任务 worktree）文件已删除、
  看不到改动时刻；
- ``written_by_other_agent_session``：改动时刻只落在别的会话的调用期间，或 tracker 里别的会话以强证据持有
  （标明是否同一 Kanban 任务）；
- ``time_window_suggests_this_session_outside_single_task_worktree``：非单任务 worktree 里，改动时刻只落在
  本会话的调用期间——像是本会话写的，但不自动认领；
- ``no_agent_session_tool_call_matches``：非单任务 worktree 里谁的调用期间都对不上（例如 IDE 手改）。

只在 ``rvf start`` 与 prepare 时由调用方显式开启（复核 M14），平时干活与遗留 Stop 路由都没有额外开销。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.session_scope_allocation.agent_sessions_that_worked_in_same_worktree_discovery import (
    AgentSessionThatWorkedInSameWorktree,
    discover_agent_sessions_that_worked_in_same_worktree,
)
from core.session_scope_allocation.agent_transcript_tool_call_time_windows_extraction import (
    AgentSessionToolCallTimeWindows,
    AgentToolCallTimeWindow,
    epoch_ms_to_iso_timestamp,
    extract_agent_session_tool_call_time_windows,
)
from core.session_scope_allocation.shell_command_named_write_target_paths_extraction import (
    ShellCommandNamedWriteTargetAnalysis,
    analyze_shell_command_named_write_targets,
    resolve_shell_command_named_write_targets_to_dirty_paths,
    unique_basename_to_dirty_path_index,
)

EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT = "file_write_tool_call_by_subagent"
EVIDENCE_FILE_WRITE_TOOL_CALL_IN_MAIN_SESSION_TRANSCRIPT = "file_write_tool_call_in_main_session_transcript"
EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET = "shell_command_named_write_target"
EVIDENCE_AGENT_EXPLICIT_CLAIM = "agent_explicit_claim"
EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION = (
    "kanban_single_task_worktree_unowned_dirty_change_default_attribution"
)

# 弱证据：够把路径拿来给本会话审，不够让别的会话把它判成「本会话独占」而从自己的横幅里拿掉（候选摘要据此过滤）。
WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS = frozenset({
    EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION,
})
LAYERED_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS = frozenset({
    EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT,
    EVIDENCE_FILE_WRITE_TOOL_CALL_IN_MAIN_SESSION_TRANSCRIPT,
    EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET,
    EVIDENCE_AGENT_EXPLICIT_CLAIM,
    EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION,
})

UNCOVERED_REASON_CONTESTED = "contested"
UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION = "written_by_other_agent_session"
UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE = (
    "time_window_suggests_this_session_outside_single_task_worktree"
)
UNCOVERED_REASON_NO_AGENT_SESSION_TOOL_CALL_MATCHES = "no_agent_session_tool_call_matches"

KANBAN_SINGLE_TASK_WORKTREE_PATH_PATTERN = re.compile(r"/\.cline/worktrees/[^/]+/[^/]+/?$")
# EXPERIMENT_NODE: 文件改动时刻与工具调用起止比对的容差——复核实测同步调用两侧各有约 2 秒 hook 开销，容差再大只会合并相邻调用、放大并发误报。
FILE_CHANGE_TIME_TOLERANCE_AROUND_TOOL_CALL_WINDOW_MILLISECONDS = 1500
# EXPERIMENT_NODE: 枚举同 worktree 其他会话时往回看多久——太短会漏掉更早改动时的并发会话，太长会多读 transcript。
OTHER_SESSION_DISCOVERY_MAXIMUM_LOOKBACK_MILLISECONDS = 7 * 24 * 3600 * 1000


@dataclass(frozen=True)
class LayeredSessionWriteAttributionEvidence:
    repository_relative_path: str
    evidence: str
    confidence: str  # strong | medium | weak
    tool_call_source: str | None = None
    call_id: str | None = None
    transcript_path: str | None = None
    transcript_line_number: int | None = None
    window_started_at_epoch_ms: int | None = None
    window_ended_at_epoch_ms: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.repository_relative_path,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "tool_call_source": self.tool_call_source,
            "call_id": self.call_id,
            "transcript_path": self.transcript_path,
            "transcript_line_number": self.transcript_line_number,
            "window_started_at": epoch_ms_to_iso_timestamp(self.window_started_at_epoch_ms),
            "window_ended_at": epoch_ms_to_iso_timestamp(self.window_ended_at_epoch_ms),
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class LayeredSessionWriteAttributionResult:
    attributed: list[LayeredSessionWriteAttributionEvidence] = field(default_factory=list)
    uncovered_dirty_paths_by_reason: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # 本层登记了归属的主会话工具调用行号：计入 watermark，审完后不再重复认领。
    main_session_attributing_transcript_line_numbers: list[int] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def attributed_paths(self) -> set[str]:
        return {item.repository_relative_path for item in self.attributed}


def repository_is_kanban_single_task_worktree(repository_root: Path) -> bool:
    """``…/.cline/worktrees/<task id>/<repo>``：Cline Kanban 给单个任务开的 worktree。"""
    return any(
        KANBAN_SINGLE_TASK_WORKTREE_PATH_PATTERN.search(spelling)
        for spelling in {str(repository_root), str(repository_root.resolve())}
    )


def observe_dirty_path_change_epoch_ms(repository_root: Path, repository_relative_path: str) -> int | None:
    """``max(mtime, ctime)``（``lstat``，不跟随符号链接）。文件已不存在 → None。

    ctime 覆盖 ``mv`` / ``cp -p`` / tar 这类保留 mtime 的落地方式（改名会更新 ctime）。"""
    # EXPERIMENT_NODE: 改动时刻取 max(mtime, ctime) 还是只取 mtime——ctime 也会被 chmod / 扩展属性变更推后（复核实测）。
    try:
        stat_result = os.lstat(repository_root / repository_relative_path)
    except OSError:
        return None
    return int(max(stat_result.st_mtime, stat_result.st_ctime) * 1000)


def _repository_relative_path_of_tool_path(repository_root: Path, raw_path: str, working_directory: str | None) -> str | None:
    value = raw_path.strip().strip("'\"")
    if not value:
        return None
    if value.startswith("file://"):
        value = value[len("file://"):]
    if not os.path.isabs(value):
        value = os.path.join(working_directory or str(repository_root), value)
    normalized = os.path.normpath(value)
    for root in {os.path.normpath(str(repository_root)), os.path.normpath(str(repository_root.resolve()))}:
        if normalized.startswith(root + "/"):
            return normalized[len(root) + 1:]
    return None


class _ShellCommandAnalysisCache:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str | None], ShellCommandNamedWriteTargetAnalysis] = {}

    def analysis_of(self, window: AgentToolCallTimeWindow) -> ShellCommandNamedWriteTargetAnalysis | None:
        if not window.shell_command_text:
            return None
        key = (window.shell_command_text, window.shell_working_directory)
        if key not in self._cache:
            self._cache[key] = analyze_shell_command_named_write_targets(window.shell_command_text, window.shell_working_directory)
        return self._cache[key]

    def window_is_possibly_writing(self, window: AgentToolCallTimeWindow) -> bool:
        """可能改文件的调用：排除已知只读工具、子代理容器调用、整条只由只读命令组成的 shell 调用（复核 M2）。"""
        if window.tool_call_cannot_write_repository_files or window.tool_call_is_subagent_container_with_own_transcript:
            return False
        analysis = self.analysis_of(window)
        return analysis is None or not analysis.has_no_possible_write_effect


def _window_is_after_attribution_cutoff(
    window: AgentToolCallTimeWindow,
    *,
    main_transcript_path: Path,
    main_transcript_cutoff_line_number: int | None,
    attribution_lower_bound_epoch_ms: int | None,
    now_epoch_ms: int,
) -> bool:
    """主会话 transcript 里的调用按行号截断（与 ``build_manifest`` 主循环同口径）；子代理 / 其它文件按时间截断。"""
    if (
        main_transcript_cutoff_line_number is not None
        and window.is_main_session_tool_call
        and os.path.normpath(window.transcript_path) == os.path.normpath(str(main_transcript_path))
    ):
        return window.transcript_line_number > main_transcript_cutoff_line_number
    if attribution_lower_bound_epoch_ms is None:
        return True
    return window.ended_at_epoch_ms_or(now_epoch_ms) > attribution_lower_bound_epoch_ms


def _evidence_from_window(
    path: str, evidence: str, confidence: str, window: AgentToolCallTimeWindow | None, detail: dict[str, Any] | None = None
) -> LayeredSessionWriteAttributionEvidence:
    if window is None:
        return LayeredSessionWriteAttributionEvidence(path, evidence, confidence, detail=detail or {})
    return LayeredSessionWriteAttributionEvidence(
        repository_relative_path=path,
        evidence=evidence,
        confidence=confidence,
        tool_call_source=window.tool_call_source,
        call_id=window.call_id,
        transcript_path=window.transcript_path,
        transcript_line_number=window.transcript_line_number,
        window_started_at_epoch_ms=window.started_at_epoch_ms,
        window_ended_at_epoch_ms=window.ended_at_epoch_ms,
        detail=detail or {},
    )


def _windows_containing_change_time(
    windows: list[AgentToolCallTimeWindow], change_epoch_ms: int, *, now_epoch_ms: int, tolerance_ms: int
) -> list[AgentToolCallTimeWindow]:
    return [
        window
        for window in windows
        if window.started_at_epoch_ms - tolerance_ms <= change_epoch_ms <= window.ended_at_epoch_ms_or(now_epoch_ms) + tolerance_ms
    ]


def _own_time_window_hint(windows: list[AgentToolCallTimeWindow], *, now_epoch_ms: int) -> dict[str, Any] | None:
    if not windows:
        return None
    best = min(windows, key=lambda window: window.ended_at_epoch_ms_or(now_epoch_ms) - window.started_at_epoch_ms)
    return {
        "possibly_written_by_this_session_tool_call_at": epoch_ms_to_iso_timestamp(best.started_at_epoch_ms),
        "possibly_written_by_this_session_tool_name": best.tool_name,
        "possibly_written_by_this_session_command_excerpt": (best.shell_command_text or "")[:200] or None,
        "possibly_written_by_this_session_tool_call_source": best.tool_call_source,
    }


def _other_session_summary(session: AgentSessionThatWorkedInSameWorktree, window: AgentToolCallTimeWindow) -> dict[str, Any]:
    return {
        "host_kind": session.host_kind,
        "session_id": session.session_id,
        "transcript_path": session.transcript_path,
        "is_same_kanban_task": session.is_same_kanban_task_as_target_worktree,
        "session_last_activity_at": epoch_ms_to_iso_timestamp(session.last_activity_epoch_ms),
        "overlapping_tool_name": window.tool_name,
        "overlapping_tool_call_started_at": epoch_ms_to_iso_timestamp(window.started_at_epoch_ms),
        "overlapping_command_excerpt": (window.shell_command_text or "")[:200] or None,
    }


def _deduplicated_other_session_summaries(
    hits: list[tuple[AgentSessionThatWorkedInSameWorktree, AgentToolCallTimeWindow]],
) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for session, window in hits:
        summaries.setdefault(session.transcript_path, _other_session_summary(session, window))
    return list(summaries.values())


def attribute_unattributed_dirty_paths_with_layered_session_write_evidence(
    *,
    repository_root: Path,
    main_transcript_path: Path,
    session_id: str | None,
    unattributed_dirty_paths: set[str],
    main_transcript_cutoff_line_number: int | None,
    attribution_lower_bound_epoch_ms: int | None,
    agent_explicit_claim_paths: set[str] | None = None,
    paths_held_by_other_sessions_with_strong_evidence: set[str] | None = None,
    repository_is_kanban_single_task_worktree_override: bool | None = None,
    enable_time_window_layer: bool = True,
    now_epoch_ms: int | None = None,
    own_session_tool_call_time_windows: AgentSessionToolCallTimeWindows | None = None,
    claude_projects_root: Path | None = None,
    codex_sessions_root: Path | None = None,
    kimi_code_home: Path | None = None,
) -> LayeredSessionWriteAttributionResult:
    started_monotonic = time.monotonic()
    now_ms = now_epoch_ms if now_epoch_ms is not None else int(time.time() * 1000)
    result = LayeredSessionWriteAttributionResult()
    remaining = set(unattributed_dirty_paths)
    if not remaining:
        result.diagnostics = {"status": "no_unattributed_dirty_paths"}
        return result

    own = own_session_tool_call_time_windows or extract_agent_session_tool_call_time_windows(main_transcript_path)
    analyses = _ShellCommandAnalysisCache()
    own_windows_after_cutoff = [
        window
        for window in own.windows
        if _window_is_after_attribution_cutoff(
            window,
            main_transcript_path=main_transcript_path,
            main_transcript_cutoff_line_number=main_transcript_cutoff_line_number,
            attribution_lower_bound_epoch_ms=attribution_lower_bound_epoch_ms,
            now_epoch_ms=now_ms,
        )
    ]

    def attribute(evidence_item: LayeredSessionWriteAttributionEvidence, window: AgentToolCallTimeWindow | None) -> None:
        remaining.discard(evidence_item.repository_relative_path)
        result.attributed.append(evidence_item)
        if window is not None and window.is_main_session_tool_call and os.path.normpath(window.transcript_path) == os.path.normpath(
            str(main_transcript_path)
        ):
            result.main_session_attributing_transcript_line_numbers.append(window.transcript_line_number)

    # 第 1 层：成功的写工具调用写明的路径（子代理；主循环不认识的宿主格式；MCP 写工具）。
    for window in own_windows_after_cutoff:
        if window.tool_call_failed:
            continue
        for raw_path in window.file_write_paths:
            path = _repository_relative_path_of_tool_path(repository_root, raw_path, window.shell_working_directory)
            if path is None or path not in remaining:
                continue
            label = (
                EVIDENCE_FILE_WRITE_TOOL_CALL_IN_MAIN_SESSION_TRANSCRIPT
                if window.is_main_session_tool_call
                else EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT
            )
            attribute(_evidence_from_window(path, label, "strong", window, {"tool_name": window.tool_name}), window)

    # 第 2 层：shell 命令写明的写入目标。
    for window in own_windows_after_cutoff:
        if not remaining:
            break
        analysis = analyses.analysis_of(window)
        if analysis is None:
            continue
        for match in resolve_shell_command_named_write_targets_to_dirty_paths(
            analysis,
            repository_root=repository_root,
            dirty_paths=remaining,
            unique_basename_to_dirty_path=unique_basename_to_dirty_path_index(remaining),
        ):
            if match.repository_relative_path not in remaining:
                continue
            attribute(
                _evidence_from_window(
                    match.repository_relative_path,
                    EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET,
                    "medium",
                    window,
                    {
                        "write_kind": match.write_kind,
                        "match_kind": match.match_kind,
                        "trigger_command_excerpt": match.trigger_command_excerpt,
                    },
                ),
                window,
            )

    # 第 3 层：agent 显式认领（每次重新注入，避免被 tracker 的孤儿清理删掉）。
    for path in sorted((agent_explicit_claim_paths or set()) & remaining):
        attribute(LayeredSessionWriteAttributionEvidence(path, EVIDENCE_AGENT_EXPLICIT_CLAIM, "strong"), None)

    single_task_worktree = (
        repository_is_kanban_single_task_worktree_override
        if repository_is_kanban_single_task_worktree_override is not None
        else repository_is_kanban_single_task_worktree(repository_root)
    )
    diagnostics: dict[str, Any] = {
        "status": "ok",
        "repository_is_kanban_single_task_worktree": single_task_worktree,
        "own_session_host_kind": own.host_kind,
        "own_session_tool_call_window_count": len(own.windows),
        "own_session_tool_call_window_count_after_cutoff": len(own_windows_after_cutoff),
        "own_session_subagent_transcripts_read": len(own.subagent_transcript_paths_read),
        "own_session_transcript_parse_problems": list(own.transcript_parse_problems),
        "time_window_layer_enabled": enable_time_window_layer,
    }

    # 第 4 层：单任务 worktree 默认认领（排除别的会话写的）；其它 worktree 只打时间窗标签。
    if remaining:
        _classify_remaining_paths_with_default_attribution_and_time_window_hints(
            repository_root=repository_root,
            main_transcript_path=main_transcript_path,
            session_id=session_id or own.session_id,
            own=own,
            own_windows_after_cutoff=own_windows_after_cutoff,
            analyses=analyses,
            remaining=remaining,
            single_task_worktree=single_task_worktree,
            paths_held_by_other_sessions_with_strong_evidence=paths_held_by_other_sessions_with_strong_evidence or set(),
            enable_time_window_layer=enable_time_window_layer,
            attribute=attribute,
            result=result,
            diagnostics=diagnostics,
            now_ms=now_ms,
            claude_projects_root=claude_projects_root,
            codex_sessions_root=codex_sessions_root,
            kimi_code_home=kimi_code_home,
        )
    diagnostics["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
    result.diagnostics = diagnostics
    return result


def _classify_remaining_paths_with_default_attribution_and_time_window_hints(
    *,
    repository_root: Path,
    main_transcript_path: Path,
    session_id: str | None,
    own: AgentSessionToolCallTimeWindows,
    own_windows_after_cutoff: list[AgentToolCallTimeWindow],
    analyses: _ShellCommandAnalysisCache,
    remaining: set[str],
    single_task_worktree: bool,
    paths_held_by_other_sessions_with_strong_evidence: set[str],
    enable_time_window_layer: bool,
    attribute: Callable[[LayeredSessionWriteAttributionEvidence, AgentToolCallTimeWindow | None], None],
    result: LayeredSessionWriteAttributionResult,
    diagnostics: dict[str, Any],
    now_ms: int,
    claude_projects_root: Path | None,
    codex_sessions_root: Path | None,
    kimi_code_home: Path | None,
) -> None:
    def uncovered(reason: str, entry: dict[str, Any]) -> None:
        result.uncovered_dirty_paths_by_reason.setdefault(reason, []).append(entry)

    tolerance_ms = FILE_CHANGE_TIME_TOLERANCE_AROUND_TOOL_CALL_WINDOW_MILLISECONDS
    change_epoch_ms_by_path = {path: observe_dirty_path_change_epoch_ms(repository_root, path) for path in sorted(remaining)}
    own_possibly_writing_windows = [window for window in own_windows_after_cutoff if analyses.window_is_possibly_writing(window)]
    other_sessions_with_windows: list[tuple[AgentSessionThatWorkedInSameWorktree, list[AgentToolCallTimeWindow]]] = []
    discovery_incomplete_reasons: list[str] = []
    if enable_time_window_layer:
        observed_change_times = [value for value in change_epoch_ms_by_path.values() if value is not None]
        discovery_lower_bound_ms = max(
            min(observed_change_times, default=now_ms) - tolerance_ms,
            now_ms - OTHER_SESSION_DISCOVERY_MAXIMUM_LOOKBACK_MILLISECONDS,
        )
        discovery = discover_agent_sessions_that_worked_in_same_worktree(
            repository_root,
            activity_lower_bound_epoch_ms=discovery_lower_bound_ms,
            excluded_transcript_paths=[main_transcript_path, *own.subagent_transcript_paths_read],
            claude_projects_root=claude_projects_root,
            codex_sessions_root=codex_sessions_root,
            kimi_code_home=kimi_code_home,
        )
        discovery_incomplete_reasons = list(discovery.discovery_incomplete_reasons)
        own_call_ids = own.call_ids
        for session in discovery.sessions:
            extracted = extract_agent_session_tool_call_time_windows(Path(session.transcript_path))
            if session_id and session.session_id == session_id:
                # 同一会话的另一个 transcript 文件（Codex resume 会以同一 id 另起 rollout；Claude 换 cwd 会进另一个
                # project 目录）：算本会话。
                own_possibly_writing_windows.extend(
                    window
                    for window in extracted.windows
                    if analyses.window_is_possibly_writing(window) and window.ended_at_epoch_ms_or(now_ms) >= discovery_lower_bound_ms
                )
                continue
            # fork / resume 会把本会话的历史调用原样复制进新 transcript（时间戳、调用 id 都相同）：同一调用 id 视为
            # 本会话自己的调用，不算并发（复核 M3）。
            windows = [
                window
                for window in extracted.windows
                if analyses.window_is_possibly_writing(window) and not (window.call_id and window.call_id in own_call_ids)
            ]
            if windows:
                other_sessions_with_windows.append((session, windows))
        diagnostics.update(
            {
                "other_agent_sessions_in_same_worktree": [session.to_dict() for session, _windows in other_sessions_with_windows],
                "other_agent_session_discovery_incomplete_reasons": discovery_incomplete_reasons,
                "other_agent_session_discovery_lower_bound_at": epoch_ms_to_iso_timestamp(discovery_lower_bound_ms),
                "file_change_time_tolerance_milliseconds": tolerance_ms,
            }
        )

    for path in sorted(remaining):
        change_ms = change_epoch_ms_by_path.get(path)
        change_at = epoch_ms_to_iso_timestamp(change_ms)
        if path in paths_held_by_other_sessions_with_strong_evidence:
            uncovered(
                UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION,
                {"path": path, "detail": "tracker_records_other_session_strong_evidence", "file_changed_at": change_at},
            )
            continue
        own_hits = (
            _windows_containing_change_time(own_possibly_writing_windows, change_ms, now_epoch_ms=now_ms, tolerance_ms=tolerance_ms)
            if change_ms is not None
            else []
        )
        other_hits = (
            [
                (session, window)
                for session, windows in other_sessions_with_windows
                for window in _windows_containing_change_time(windows, change_ms, now_epoch_ms=now_ms, tolerance_ms=tolerance_ms)
            ]
            if change_ms is not None
            else []
        )
        hint = _own_time_window_hint(own_hits, now_epoch_ms=now_ms)
        if other_hits:
            uncovered(
                UNCOVERED_REASON_CONTESTED if own_hits else UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION,
                {
                    "path": path,
                    "detail": (
                        "file_change_time_overlaps_this_and_other_agent_session_tool_calls"
                        if own_hits
                        else "file_change_time_inside_other_agent_session_tool_call_only"
                    ),
                    "file_changed_at": change_at,
                    "other_sessions": _deduplicated_other_session_summaries(other_hits),
                    **(hint or {}),
                },
            )
            continue
        if single_task_worktree:
            detail: dict[str, Any] = {"file_changed_at": change_at}
            if change_ms is None:
                detail["file_deleted_change_time_unobservable"] = True
            if hint:
                detail.update(hint)
            if discovery_incomplete_reasons:
                detail["other_agent_session_discovery_incomplete_reasons"] = discovery_incomplete_reasons
            attribute(
                LayeredSessionWriteAttributionEvidence(
                    path, EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION, "weak", detail=detail
                ),
                None,
            )
            continue
        if change_ms is None:
            uncovered(UNCOVERED_REASON_CONTESTED, {"path": path, "detail": "deleted_file_change_time_unobservable"})
            continue
        if hint:
            uncovered(
                UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE,
                {"path": path, "file_changed_at": change_at, **hint},
            )
            continue
        uncovered(UNCOVERED_REASON_NO_AGENT_SESSION_TOOL_CALL_MATCHES, {"path": path, "file_changed_at": change_at})


def classify_dirty_paths_by_file_change_time_and_tool_call_windows_without_registering(
    *,
    repository_root: Path,
    main_transcript_path: Path,
    dirty_paths: set[str],
    main_transcript_cutoff_line_number: int | None = None,
    attribution_lower_bound_epoch_ms: int | None = None,
    now_epoch_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """只跑时间窗判定（按「非单任务 worktree」口径，不默认认领），逐路径给出结论，不登记任何归属。
    供可行性实测与交叉校验用。返回 ``{path: {"verdict": <UNCOVERED_REASON_…>, **detail}}``。"""
    now_ms = now_epoch_ms if now_epoch_ms is not None else int(time.time() * 1000)
    own = extract_agent_session_tool_call_time_windows(main_transcript_path)
    analyses = _ShellCommandAnalysisCache()
    own_windows_after_cutoff = [
        window
        for window in own.windows
        if _window_is_after_attribution_cutoff(
            window,
            main_transcript_path=main_transcript_path,
            main_transcript_cutoff_line_number=main_transcript_cutoff_line_number,
            attribution_lower_bound_epoch_ms=attribution_lower_bound_epoch_ms,
            now_epoch_ms=now_ms,
        )
    ]
    scratch = LayeredSessionWriteAttributionResult()
    _classify_remaining_paths_with_default_attribution_and_time_window_hints(
        repository_root=repository_root,
        main_transcript_path=main_transcript_path,
        session_id=own.session_id,
        own=own,
        own_windows_after_cutoff=own_windows_after_cutoff,
        analyses=analyses,
        remaining=set(dirty_paths),
        single_task_worktree=False,
        paths_held_by_other_sessions_with_strong_evidence=set(),
        enable_time_window_layer=True,
        attribute=lambda _evidence, _window: None,
        result=scratch,
        diagnostics={},
        now_ms=now_ms,
        claude_projects_root=None,
        codex_sessions_root=None,
        kimi_code_home=None,
    )
    verdicts: dict[str, dict[str, Any]] = {}
    for reason, entries in scratch.uncovered_dirty_paths_by_reason.items():
        for entry in entries:
            verdicts[str(entry["path"])] = {"verdict": reason, **entry}
    return verdicts


def evidence_label_is_weak_session_write_attribution(evidence: str | None) -> bool:
    return evidence in WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS


__all__ = [
    "EVIDENCE_AGENT_EXPLICIT_CLAIM",
    "EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT",
    "EVIDENCE_FILE_WRITE_TOOL_CALL_IN_MAIN_SESSION_TRANSCRIPT",
    "EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION",
    "EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET",
    "LAYERED_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS",
    "LayeredSessionWriteAttributionEvidence",
    "LayeredSessionWriteAttributionResult",
    "UNCOVERED_REASON_CONTESTED",
    "UNCOVERED_REASON_NO_AGENT_SESSION_TOOL_CALL_MATCHES",
    "UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE",
    "UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION",
    "WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS",
    "attribute_unattributed_dirty_paths_with_layered_session_write_evidence",
    "classify_dirty_paths_by_file_change_time_and_tool_call_windows_without_registering",
    "evidence_label_is_weak_session_write_attribution",
    "observe_dirty_path_change_epoch_ms",
    "repository_is_kanban_single_task_worktree",
]
