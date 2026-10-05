"""枚举「同一 worktree 里、某个时刻之后还在活动的其他 agent 会话」——session 归属时间窗层的并发检测积木。

时间窗层（见 ``agent_transcript_tool_call_time_windows_extraction``）把文件判给本会话的前提是：文件改动
那一刻，**同一 worktree 没有别的 agent 会话在调用工具**。本模块负责找出那些「别的会话」。

各宿主的发现方式（均为实测；仓库里此前没有现成 helper）：

- Claude Code：``<config dir>/projects/<slug>/<session>.jsonl``。slug 是 cwd 把非字母数字字符换成 ``-``，
  有歧义（``/a/b-c`` 与 ``/a/b/c`` 同 slug），所以只拿 slug 前缀做预筛，再用记录里的 ``cwd`` 确认落在
  worktree 之下。
- Codex：``<CODEX_HOME>/sessions/**/rollout-*.jsonl``，按文件 mtime 剪枝，再读首条 ``session_meta`` 的
  ``cwd``。会话按**开始日期**分目录，跨天的长会话在旧目录里，所以不按目录日期剪枝。
- Kimi：``<KIMI_CODE_HOME>/session_index.jsonl`` 每行 ``{sessionId, sessionDir, workDir}``。

只读文件元数据与少量首行，不读完整 transcript（那由调用方按需交给时间窗抽取）。宿主之外的写入者
（用户在 IDE 里手改、未知宿主）发现不了，调用方据此把对不上的改动标成「谁都对不上」。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.session_scope_allocation.agent_transcript_tool_call_time_windows_extraction import (
    HOST_KIND_CLAUDE_CODE,
    HOST_KIND_CODEX,
    HOST_KIND_KIMI,
)

KANBAN_TASK_WORKTREE_PATH_PATTERN = re.compile(r"/\.cline/worktrees/(?P<task_id>[^/]+)/")
CLAUDE_PROJECT_SLUG_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^A-Za-z0-9]")
# 读 transcript 开头多少条记录去找 cwd（Claude 首几条可能是 summary / snapshot，没有 cwd）。
MAXIMUM_LEADING_RECORDS_SCANNED_FOR_WORKING_DIRECTORY = 60
# EXPERIMENT_NODE: 每个宿主最多确认多少个「近期活动的候选 transcript」——防止极端情况下扫描爆炸；超出时结果标记为不完整。
MAXIMUM_RECENTLY_ACTIVE_CANDIDATE_TRANSCRIPTS_PER_HOST = 400


@dataclass(frozen=True)
class AgentSessionThatWorkedInSameWorktree:
    host_kind: str
    session_id: str | None
    transcript_path: str
    working_directory: str
    last_activity_epoch_ms: int
    kanban_task_id_of_working_directory: str | None
    is_same_kanban_task_as_target_worktree: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_kind": self.host_kind,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "working_directory": self.working_directory,
            "last_activity_epoch_ms": self.last_activity_epoch_ms,
            "kanban_task_id_of_working_directory": self.kanban_task_id_of_working_directory,
            "is_same_kanban_task_as_target_worktree": self.is_same_kanban_task_as_target_worktree,
        }


@dataclass
class SameWorktreeAgentSessionDiscoveryResult:
    sessions: list[AgentSessionThatWorkedInSameWorktree] = field(default_factory=list)
    # 某个宿主的候选数超过上限、或目录读不了：结果可能漏会话，调用方应把「独占」判定降级。
    discovery_incomplete_reasons: list[str] = field(default_factory=list)


def kanban_task_id_from_working_directory(working_directory: str | None) -> str | None:
    if not working_directory:
        return None
    match = KANBAN_TASK_WORKTREE_PATH_PATTERN.search(working_directory.rstrip("/") + "/")
    return match.group("task_id") if match else None


def _path_spellings(path: Path) -> tuple[str, ...]:
    spellings = [os.path.normpath(str(path))]
    try:
        resolved = os.path.normpath(str(path.resolve()))
    except OSError:
        resolved = spellings[0]
    if resolved not in spellings:
        spellings.append(resolved)
    return tuple(spellings)


def working_directory_is_inside_worktree(working_directory: str | None, worktree_spellings: tuple[str, ...]) -> bool:
    if not working_directory:
        return False
    normalized = os.path.normpath(working_directory[len("file://"):] if working_directory.startswith("file://") else working_directory)
    return any(normalized == root or normalized.startswith(root + "/") for root in worktree_spellings)


def claude_project_directory_slug_for_working_directory(working_directory: str) -> str:
    return CLAUDE_PROJECT_SLUG_NON_ALPHANUMERIC_PATTERN.sub("-", working_directory)


def claude_projects_root_directory() -> Path:
    config_directory = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_directory).expanduser() if config_directory else Path.home() / ".claude") / "projects"


def kimi_code_home_directory() -> Path:
    home = os.environ.get("KIMI_CODE_HOME")
    return Path(home).expanduser() if home else Path.home() / ".kimi-code"


def codex_sessions_root_directory() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "sessions"


def _file_mtime_epoch_ms(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return None


def _leading_records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= MAXIMUM_LEADING_RECORDS_SCANNED_FOR_WORKING_DIRECTORY:
                    return
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _claude_transcript_working_directory_and_session_id(path: Path) -> tuple[str | None, str | None]:
    for record in _leading_records(path):
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            session_id = record.get("sessionId") if isinstance(record.get("sessionId"), str) else path.stem
            return cwd, session_id
    return None, None


def _codex_rollout_working_directory_and_session_id(path: Path) -> tuple[str | None, str | None]:
    for record in _leading_records(path):
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
            session_id = payload.get("id") if isinstance(payload.get("id"), str) else None
            return cwd, session_id
    return None, None


def discover_agent_sessions_that_worked_in_same_worktree(
    worktree_root: Path,
    *,
    activity_lower_bound_epoch_ms: int,
    excluded_transcript_paths: Iterable[str | Path] = (),
    excluded_session_ids: Iterable[str] = (),
    claude_projects_root: Path | None = None,
    codex_sessions_root: Path | None = None,
    kimi_code_home: Path | None = None,
) -> SameWorktreeAgentSessionDiscoveryResult:
    """找出 cwd 落在 ``worktree_root`` 之下、transcript 在 ``activity_lower_bound_epoch_ms`` 之后还有写入的会话。

    ``excluded_*`` 用来排除本会话自己（主 transcript 与子代理 transcript）。"""
    result = SameWorktreeAgentSessionDiscoveryResult()
    worktree_spellings = _path_spellings(worktree_root)
    target_kanban_task_id = kanban_task_id_from_working_directory(worktree_spellings[-1]) or kanban_task_id_from_working_directory(
        worktree_spellings[0]
    )
    excluded_paths = {os.path.normpath(str(path)) for path in excluded_transcript_paths}
    excluded_ids = {session_id for session_id in excluded_session_ids if session_id}

    def add_session(host_kind: str, session_id: str | None, transcript_path: Path, working_directory: str, mtime_ms: int) -> None:
        if os.path.normpath(str(transcript_path)) in excluded_paths or (session_id and session_id in excluded_ids):
            return
        kanban_task_id = kanban_task_id_from_working_directory(working_directory)
        result.sessions.append(
            AgentSessionThatWorkedInSameWorktree(
                host_kind=host_kind,
                session_id=session_id,
                transcript_path=str(transcript_path),
                working_directory=working_directory,
                last_activity_epoch_ms=mtime_ms,
                kanban_task_id_of_working_directory=kanban_task_id,
                is_same_kanban_task_as_target_worktree=bool(target_kanban_task_id) and kanban_task_id == target_kanban_task_id,
            )
        )

    # Claude Code
    projects_root = claude_projects_root if claude_projects_root is not None else claude_projects_root_directory()
    slug_prefixes = {claude_project_directory_slug_for_working_directory(spelling) for spelling in worktree_spellings}
    try:
        project_directories = [
            entry for entry in projects_root.iterdir() if entry.is_dir() and any(entry.name.startswith(prefix) for prefix in slug_prefixes)
        ] if projects_root.is_dir() else []
    except OSError as exc:
        project_directories = []
        result.discovery_incomplete_reasons.append(f"claude_projects_directory_unreadable: {exc}")
    claude_candidates: list[tuple[Path, int]] = []
    for project_directory in project_directories:
        for transcript in project_directory.glob("*.jsonl"):
            mtime_ms = _file_mtime_epoch_ms(transcript)
            if mtime_ms is not None and mtime_ms >= activity_lower_bound_epoch_ms:
                claude_candidates.append((transcript, mtime_ms))
    if len(claude_candidates) > MAXIMUM_RECENTLY_ACTIVE_CANDIDATE_TRANSCRIPTS_PER_HOST:
        result.discovery_incomplete_reasons.append("claude_code_recently_active_candidate_transcripts_over_limit")
        claude_candidates = sorted(claude_candidates, key=lambda item: item[1], reverse=True)[:MAXIMUM_RECENTLY_ACTIVE_CANDIDATE_TRANSCRIPTS_PER_HOST]
    for transcript, mtime_ms in claude_candidates:
        cwd, session_id = _claude_transcript_working_directory_and_session_id(transcript)
        if working_directory_is_inside_worktree(cwd, worktree_spellings):
            add_session(HOST_KIND_CLAUDE_CODE, session_id, transcript, str(cwd), mtime_ms)

    # Codex
    sessions_root = codex_sessions_root if codex_sessions_root is not None else codex_sessions_root_directory()
    codex_candidates: list[tuple[Path, int]] = []
    if sessions_root.is_dir():
        try:
            for directory, _subdirectories, file_names in os.walk(sessions_root):
                for file_name in file_names:
                    if not (file_name.startswith("rollout-") and file_name.endswith(".jsonl")):
                        continue
                    rollout = Path(directory) / file_name
                    mtime_ms = _file_mtime_epoch_ms(rollout)
                    if mtime_ms is not None and mtime_ms >= activity_lower_bound_epoch_ms:
                        codex_candidates.append((rollout, mtime_ms))
        except OSError as exc:
            result.discovery_incomplete_reasons.append(f"codex_sessions_directory_unreadable: {exc}")
    if len(codex_candidates) > MAXIMUM_RECENTLY_ACTIVE_CANDIDATE_TRANSCRIPTS_PER_HOST:
        result.discovery_incomplete_reasons.append("codex_recently_active_candidate_transcripts_over_limit")
        codex_candidates = sorted(codex_candidates, key=lambda item: item[1], reverse=True)[:MAXIMUM_RECENTLY_ACTIVE_CANDIDATE_TRANSCRIPTS_PER_HOST]
    for rollout, mtime_ms in codex_candidates:
        cwd, session_id = _codex_rollout_working_directory_and_session_id(rollout)
        if working_directory_is_inside_worktree(cwd, worktree_spellings):
            add_session(HOST_KIND_CODEX, session_id, rollout, str(cwd), mtime_ms)

    # Kimi
    kimi_home = kimi_code_home if kimi_code_home is not None else kimi_code_home_directory()
    session_index = kimi_home / "session_index.jsonl"
    if session_index.is_file():
        try:
            index_lines = session_index.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            index_lines = []
            result.discovery_incomplete_reasons.append(f"kimi_session_index_unreadable: {exc}")
        for line in index_lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            work_directory = entry.get("workDir")
            session_directory = entry.get("sessionDir")
            if not isinstance(session_directory, str) or not working_directory_is_inside_worktree(work_directory, worktree_spellings):
                continue
            main_wire = Path(session_directory) / "agents" / "main" / "wire.jsonl"
            agent_wires = list((Path(session_directory) / "agents").glob("*/wire.jsonl"))
            latest_mtime_ms = max((_file_mtime_epoch_ms(wire) or 0 for wire in agent_wires), default=0)
            if latest_mtime_ms >= activity_lower_bound_epoch_ms and main_wire.is_file():
                session_id = entry.get("sessionId") if isinstance(entry.get("sessionId"), str) else None
                add_session(HOST_KIND_KIMI, session_id, main_wire, str(work_directory), latest_mtime_ms)
    return result
