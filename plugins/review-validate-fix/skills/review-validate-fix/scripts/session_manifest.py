#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_tracker


REDIRECT_RE = re.compile(r"(?:^|\s)(?:>>?|1>|2>|&>)\s*(?P<path>[^&|;\s]+)")
CLAUDE_WRITE_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


@dataclass(frozen=True)
class PatchHunk:
    path: str
    operation: str
    mutations: tuple[str, ...]


@dataclass(frozen=True)
class CurrentHunk:
    path: str
    anchor: diff_tracker.HunkAnchor
    mutations: tuple[str, ...]


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run_git(repo: Path, args: list[str], *, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        raise RuntimeError(stderr.strip() or stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(str(run_git(repo, ["rev-parse", "--show-toplevel"])).strip()).resolve()


def normalize_repo_path(
    repo: Path,
    path: str,
    *,
    base_dir: Path | None = None,
    allow_relative: bool = True,
) -> str | None:
    root = repo.resolve()
    value = path.strip().strip("'\"")
    if not value or value in {"-", "/dev/null"}:
        return None
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
    if not allow_relative:
        return None
    try:
        return ((base_dir or root).resolve() / value).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def parse_status_z(data: bytes) -> set[str]:
    parts = [part for part in data.split(b"\0") if part]
    paths: set[str] = set()
    index = 0
    while index < len(parts):
        record = parts[index].decode("utf-8", "surrogateescape")
        if len(record) >= 4:
            xy = record[:2]
            path = record[3:]
            paths.add(path)
            if "R" in xy or "C" in xy:
                index += 1
                if index < len(parts):
                    paths.add(parts[index].decode("utf-8", "surrogateescape"))
        index += 1
    return paths


def dirty_paths(repo: Path) -> list[str]:
    data = run_git(repo, ["status", "--porcelain=v1", "-z", "-uall"], text=False)
    assert isinstance(data, bytes)
    return sorted(parse_status_z(data))


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                records.append({"type": "parse_error", "line_number": line_number})
                continue
            if isinstance(record, dict):
                record["_line_number"] = line_number
                records.append(record)
    return records


def session_id_from_records(records: list[dict[str, Any]]) -> str | None:
    for record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            return payload["id"]
    for record in records:
        session_id = record.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def payload_tool_name(payload: dict[str, Any]) -> str | None:
    name = payload.get("name")
    return name if isinstance(name, str) else None


def payload_tool_input(payload: dict[str, Any]) -> str | None:
    value = payload.get("input")
    if isinstance(value, str):
        return value
    value = payload.get("arguments")
    if isinstance(value, str):
        return value
    return None


def claude_tool_uses(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        item
        for item in content
        if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(item.get("name"), str)
    ]


def claude_write_path(repo: Path, item: dict[str, Any]) -> str | None:
    if item.get("name") not in CLAUDE_WRITE_TOOL_NAMES:
        return None
    tool_input = item.get("input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("notebook_path" if item.get("name") == "NotebookEdit" else "file_path")
    if not isinstance(file_path, str):
        return None
    return normalize_repo_path(repo, file_path)


def parse_apply_patch_details(
    repo: Path,
    patch_text: str,
    line_number: int,
) -> tuple[list[dict[str, Any]], set[str], dict[str, list[PatchHunk]]]:
    operations: list[dict[str, Any]] = []
    paths: set[str] = set()
    hunks: dict[str, list[PatchHunk]] = {}
    current: dict[str, Any] | None = None
    current_path: str | None = None
    current_operation: str | None = None
    current_mutations: list[str] = []

    def flush_hunk() -> None:
        nonlocal current_mutations
        if current_path is not None and current_operation is not None and current_mutations:
            hunks.setdefault(current_path, []).append(
                PatchHunk(
                    path=current_path,
                    operation=current_operation,
                    mutations=tuple(current_mutations),
                )
            )
        current_mutations = []

    def begin_file(operation: str, raw_path: str) -> str | None:
        nonlocal current, current_path, current_operation
        flush_hunk()
        rel = normalize_repo_path(repo, raw_path)
        if rel is None:
            current = None
            current_path = None
            current_operation = None
            return None
        current = {"operation": operation, "path": rel, "line_number": line_number}
        operations.append(current)
        paths.add(rel)
        current_path = rel
        current_operation = operation
        return rel

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("*** Add File: "):
            begin_file("add", raw_line.removeprefix("*** Add File: "))
            continue
        if raw_line.startswith("*** Delete File: "):
            begin_file("delete", raw_line.removeprefix("*** Delete File: "))
            continue
        if raw_line.startswith("*** Update File: "):
            begin_file("update", raw_line.removeprefix("*** Update File: "))
            continue
        if raw_line.startswith("*** Move to: ") and current is not None:
            rel = normalize_repo_path(repo, raw_line.removeprefix("*** Move to: "))
            if rel is None:
                continue
            current["move_to"] = rel
            paths.add(rel)
            flush_hunk()
            current_path = rel
            current_operation = "update"
            continue
        if raw_line.startswith("@@"):
            flush_hunk()
            continue
        if current_path is None:
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            current_mutations.append(raw_line)
    flush_hunk()
    return operations, paths, hunks


def parse_apply_patch(repo: Path, patch_text: str, line_number: int) -> tuple[list[dict[str, Any]], set[str]]:
    operations, paths, _hunks = parse_apply_patch_details(repo, patch_text, line_number)
    return operations, paths


def parse_exec_arguments(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def command_workdir(repo: Path, workdir: str | None) -> Path | None:
    root = repo.resolve()
    if not workdir:
        return root
    candidate = Path(workdir)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def command_path_candidates(repo: Path, command: str, workdir: Path | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    allow_relative = workdir is not None
    for match in REDIRECT_RE.finditer(command):
        rel = normalize_repo_path(repo, match.group("path"), base_dir=workdir, allow_relative=allow_relative)
        if rel is not None:
            candidates.append({"path": rel, "reason": "shell_redirect", "confidence": "medium"})

    tokens = command_tokens(command)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"touch", "mkdir", "rm", "mv", "cp"}:
            for arg in tokens[index + 1 :]:
                if arg.startswith("-"):
                    continue
                rel = normalize_repo_path(repo, arg, base_dir=workdir, allow_relative=allow_relative)
                if rel is not None:
                    candidates.append({"path": rel, "reason": token, "confidence": "low"})
                    if token in {"touch", "mkdir", "rm"}:
                        break
            break
        if token == "tee":
            for arg in tokens[index + 1 :]:
                if arg.startswith("-"):
                    continue
                rel = normalize_repo_path(repo, arg, base_dir=workdir, allow_relative=allow_relative)
                if rel is not None:
                    candidates.append({"path": rel, "reason": "tee", "confidence": "medium"})
                    break
            break
        index += 1
    return dedupe_candidates(candidates)


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (str(candidate.get("path")), str(candidate.get("reason")))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def parse_record_timestamp(record: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def head_commit(repo: Path) -> str | None:
    try:
        return str(run_git(repo, ["rev-parse", "--verify", "HEAD"])).strip() or None
    except RuntimeError:
        return None


def head_committed_at(repo: Path) -> datetime | None:
    try:
        raw = str(run_git(repo, ["show", "-s", "--format=%cI", "HEAD"])).strip()
    except RuntimeError:
        return None
    if not raw:
        return None
    return parse_record_timestamp({"timestamp": raw})


def exec_success_call_ids(records: list[dict[str, Any]]) -> set[str]:
    successful: set[str] = set()
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "exec_command_end":
            continue
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and payload.get("exit_code") == 0:
            successful.add(call_id)
    return successful


def is_git_commit_command(repo: Path, command: str, workdir: Path | None) -> bool:
    if workdir is None:
        return False
    tokens = command_tokens(command)
    if not tokens:
        return False
    for index, token in enumerate(tokens):
        if token != "git":
            continue
        cursor = index + 1
        while cursor < len(tokens):
            candidate = tokens[cursor]
            if candidate in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
                cursor += 2
                continue
            if candidate.startswith("-"):
                cursor += 1
                continue
            if candidate == "commit":
                return True
            break
    return False


def tool_name_for_record(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    return payload_tool_name(payload) if isinstance(payload, dict) else None


def has_timestamp_comparable_tool(record: dict[str, Any]) -> bool:
    if tool_name_for_record(record) in {"apply_patch", "exec_command"}:
        return True
    return any(item.get("name") in CLAUDE_WRITE_TOOL_NAMES for item in claude_tool_uses(record))


def should_include_tool_record(
    record: dict[str, Any],
    *,
    cutoff_line_number: int | None,
    cutoff_timestamp: datetime | None,
    include_all: bool,
) -> bool:
    if include_all:
        return True
    line_number = record.get("_line_number")
    if cutoff_line_number is not None and isinstance(line_number, int):
        return line_number > cutoff_line_number
    timestamp = parse_record_timestamp(record)
    if cutoff_timestamp is not None and timestamp is not None:
        return timestamp > cutoff_timestamp
    return True


def ownership_baseline(
    repo: Path,
    records: list[dict[str, Any]],
    *,
    include_all: bool,
) -> tuple[dict[str, Any], int | None, datetime | None, list[str]]:
    head = head_commit(repo)
    committed_at = head_committed_at(repo)
    warnings: list[str] = []
    if include_all:
        return (
            {
                "mode": "include_all_transcript_ownership",
                "head": head,
                "head_committed_at": committed_at.isoformat() if committed_at is not None else None,
                "cutoff_line_number": None,
                "cutoff_reason": "disabled_by_cli",
                "included_tool_record_count": 0,
                "ignored_tool_record_count": 0,
            },
            None,
            None,
            warnings,
        )

    successful_call_ids = exec_success_call_ids(records)
    cutoff_line: int | None = None
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload_tool_name(payload) != "exec_command":
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or call_id not in successful_call_ids:
            continue
        args_text = payload_tool_input(payload)
        if not args_text:
            continue
        args = parse_exec_arguments(args_text)
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        raw_workdir = args.get("workdir") if isinstance(args.get("workdir"), str) else None
        resolved_workdir = command_workdir(repo, raw_workdir)
        if is_git_commit_command(repo, cmd, resolved_workdir):
            line = record.get("_line_number")
            cutoff_line = int(line) if isinstance(line, int) else cutoff_line

    if cutoff_line is not None:
        return (
            {
                "mode": "line_cutoff",
                "head": head,
                "head_committed_at": committed_at.isoformat() if committed_at is not None else None,
                "cutoff_line_number": cutoff_line,
                "cutoff_reason": "last_successful_repo_local_git_commit",
                "included_tool_record_count": 0,
                "ignored_tool_record_count": 0,
            },
            cutoff_line,
            None,
            warnings,
        )

    has_tool_timestamps = any(
        has_timestamp_comparable_tool(record) and parse_record_timestamp(record) is not None for record in records
    )
    if committed_at is not None and has_tool_timestamps:
        return (
            {
                "mode": "head_commit_time",
                "head": head,
                "head_committed_at": committed_at.isoformat(),
                "cutoff_line_number": None,
                "cutoff_reason": "head_committed_at",
                "included_tool_record_count": 0,
                "ignored_tool_record_count": 0,
            },
            None,
            committed_at,
            warnings,
        )

    warnings.append(
        "ownership_baseline_fallback: transcript has no successful git commit cutoff and no comparable tool timestamps; using full-transcript ownership"
    )
    return (
        {
            "mode": "legacy_full_transcript",
            "head": head,
            "head_committed_at": committed_at.isoformat() if committed_at is not None else None,
            "cutoff_line_number": None,
            "cutoff_reason": "missing_commit_cutoff_and_tool_timestamps",
            "included_tool_record_count": 0,
            "ignored_tool_record_count": 0,
        },
        None,
        None,
        warnings,
    )


def current_diff_hunks(repo: Path, path: str) -> list[CurrentHunk]:
    try:
        diff = run_git(repo, ["diff", "-U3", "--no-color", "HEAD", "--", path])
    except RuntimeError:
        return []
    if not isinstance(diff, str) or not diff:
        return []
    hunks: list[CurrentHunk] = []
    pending_header: tuple[tuple[int, int], tuple[int, int], str] | None = None
    pending_header_line = ""
    context_lines: list[str] = []
    mutation_lines: list[str] = []
    in_hunk = False

    def flush() -> None:
        if pending_header is None or not mutation_lines:
            return
        old_range, new_range, _ = pending_header
        hunks.append(
            CurrentHunk(
                path=path,
                anchor=diff_tracker.HunkAnchor(
                    header=diff_tracker._normalize_header(pending_header_line),
                    context_hash=diff_tracker._context_hash(context_lines),
                    old_range=old_range,
                    new_range=new_range,
                ),
                mutations=tuple(mutation_lines),
            )
        )

    for raw_line in diff.splitlines():
        if raw_line.startswith("@@"):
            flush()
            parsed = diff_tracker._hunk_header_parts(raw_line)
            if parsed is None:
                pending_header = None
                pending_header_line = ""
                context_lines = []
                mutation_lines = []
                in_hunk = False
                continue
            pending_header = parsed
            pending_header_line = raw_line
            context_lines = []
            mutation_lines = []
            in_hunk = True
            continue
        if not in_hunk or pending_header is None:
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            mutation_lines.append(raw_line)
            continue
        if len(context_lines) < 3:
            context_lines.append(raw_line)
    flush()
    return hunks


def live_apply_patch_units(
    repo: Path,
    patch_hunks_by_path: dict[str, list[PatchHunk]],
    dirty: set[str],
) -> list[tuple[diff_tracker.OwnedUnit, str]]:
    units: list[tuple[diff_tracker.OwnedUnit, str]] = []
    seen: set[tuple[str, str, str]] = set()
    current_cache: dict[str, list[CurrentHunk]] = {}
    for path, patch_hunks in patch_hunks_by_path.items():
        if path not in dirty:
            continue
        current = current_cache.setdefault(path, current_diff_hunks(repo, path))
        path_matched = False
        for patch_hunk in patch_hunks:
            patch_mutations = set(patch_hunk.mutations)
            if not patch_mutations:
                continue
            for current_hunk in current:
                if not patch_mutations.issubset(set(current_hunk.mutations)):
                    continue
                key = (path, "hunk", current_hunk.anchor.header)
                if key not in seen:
                    seen.add(key)
                    units.append(
                        (
                            diff_tracker.OwnedUnit(path=path, unit="hunk", hunk_anchor=current_hunk.anchor),
                            "apply_patch",
                        )
                    )
                path_matched = True
        if path_matched:
            continue
        # Adds/deletes and untracked update-style test fixtures can have no
        # parseable git diff hunk. Use path-level ownership only when the
        # current diff has no hunks at all; if hunks exist but none matched,
        # treat the apply_patch hunk as no longer live.
        if not current or any(item.operation in {"add", "delete"} for item in patch_hunks):
            key = (path, "path", "")
            if key not in seen:
                seen.add(key)
                units.append((diff_tracker.OwnedUnit(path=path, unit="path", hunk_anchor=None), "apply_patch"))
    return units


def build_manifest(
    repo: Path,
    transcript: Path,
    *,
    tracker_enabled: bool = True,
    tracker_run_id: str | None = None,
    tracker_log_root: Path | None = None,
    include_all_transcript_ownership: bool = False,
) -> dict[str, Any]:
    root = git_root(repo)
    records = parse_jsonl(transcript)
    baseline, cutoff_line_number, cutoff_timestamp, baseline_warnings = ownership_baseline(
        root,
        records,
        include_all=include_all_transcript_ownership,
    )
    owned_paths: set[str] = set()
    apply_patch_operations: list[dict[str, Any]] = []
    command_candidates: list[dict[str, Any]] = []
    command_events: list[dict[str, Any]] = []
    claude_write_events: list[dict[str, Any]] = []
    dirty = dirty_paths(root)
    dirty_set = set(dirty)
    live_owned_units: list[tuple[diff_tracker.OwnedUnit, str]] = []
    live_exec_paths: set[str] = set()
    included_tool_record_count = 0
    ignored_tool_record_count = 0

    for record in records:
        payload = record.get("payload")
        tool_name = payload_tool_name(payload) if isinstance(payload, dict) else None
        if tool_name == "apply_patch":
            assert isinstance(payload, dict)
            patch_text = payload_tool_input(payload)
            if not patch_text:
                continue
            operations, paths, patch_hunks = parse_apply_patch_details(
                root,
                patch_text,
                int(record.get("_line_number", 0)),
            )
            apply_patch_operations.extend(operations)
            owned_paths.update(paths)
            if should_include_tool_record(
                record,
                cutoff_line_number=cutoff_line_number,
                cutoff_timestamp=cutoff_timestamp,
                include_all=include_all_transcript_ownership,
            ):
                included_tool_record_count += 1
                live_owned_units.extend(live_apply_patch_units(root, patch_hunks, dirty_set))
            else:
                ignored_tool_record_count += 1
            continue
        if tool_name != "exec_command":
            for item in claude_tool_uses(record):
                name = item.get("name")
                path = claude_write_path(root, item)
                if path is None:
                    continue
                event = {
                    "line_number": record.get("_line_number"),
                    "name": name,
                    "path": path,
                }
                claude_write_events.append(event)
                owned_paths.add(path)
                if should_include_tool_record(
                    record,
                    cutoff_line_number=cutoff_line_number,
                    cutoff_timestamp=cutoff_timestamp,
                    include_all=include_all_transcript_ownership,
                ):
                    included_tool_record_count += 1
                    if path in dirty_set:
                        live_owned_units.append(
                            (diff_tracker.OwnedUnit(path=path, unit="path", hunk_anchor=None), "claude_write")
                        )
                else:
                    ignored_tool_record_count += 1
            continue
        assert isinstance(payload, dict)
        args_text = payload_tool_input(payload)
        if not args_text:
            continue
        args = parse_exec_arguments(args_text)
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        raw_workdir = args.get("workdir") if isinstance(args.get("workdir"), str) else None
        resolved_workdir = command_workdir(root, raw_workdir)
        event = {
            "line_number": record.get("_line_number"),
            "cmd": cmd,
            "workdir": raw_workdir,
        }
        candidates = command_path_candidates(root, cmd, resolved_workdir)
        if candidates:
            event["path_candidates"] = candidates
            command_candidates.extend(
                {
                    "path": item["path"],
                    "reason": item["reason"],
                    "confidence": item["confidence"],
                    "cmd": cmd,
                    "line_number": record.get("_line_number"),
                }
                for item in candidates
            )
            owned_paths.update(str(item["path"]) for item in candidates)
            if should_include_tool_record(
                record,
                cutoff_line_number=cutoff_line_number,
                cutoff_timestamp=cutoff_timestamp,
                include_all=include_all_transcript_ownership,
            ):
                included_tool_record_count += 1
                for item in candidates:
                    path = str(item["path"])
                    if path in dirty_set:
                        live_exec_paths.add(path)
            else:
                ignored_tool_record_count += 1
        command_events.append(event)

    seen_live_units: set[tuple[str, str, str]] = set()
    deduped_live_units: list[tuple[diff_tracker.OwnedUnit, str]] = []
    for owned_unit, evidence in live_owned_units:
        key = (
            owned_unit.path,
            owned_unit.unit,
            owned_unit.hunk_anchor.header if owned_unit.hunk_anchor is not None else "",
        )
        if key in seen_live_units:
            continue
        seen_live_units.add(key)
        deduped_live_units.append((owned_unit, evidence))
    for path in sorted(live_exec_paths):
        key = (path, "path", "")
        if key in seen_live_units:
            continue
        seen_live_units.add(key)
        deduped_live_units.append((diff_tracker.OwnedUnit(path=path, unit="path", hunk_anchor=None), "exec_command"))

    owned_dirty = sorted({owned_unit.path for owned_unit, _evidence in deduped_live_units if owned_unit.path in dirty_set})
    unattributed_dirty = sorted(path for path in dirty if path not in set(owned_dirty))
    session_id = session_id_from_records(records)
    baseline["included_tool_record_count"] = included_tool_record_count
    baseline["ignored_tool_record_count"] = ignored_tool_record_count

    tracker_payload: dict[str, Any] = {"status": "skipped"}
    tracker_units: list[dict[str, Any]] = []
    if tracker_enabled and owned_dirty:
        owned_dirty_set = set(owned_dirty)
        tracker_apply_patch_paths = {
            owned_unit.path
            for owned_unit, evidence in deduped_live_units
            if evidence == "apply_patch" and owned_unit.path in owned_dirty_set
        }
        tracker_exec_only_paths = {
            owned_unit.path
            for owned_unit, evidence in deduped_live_units
            if evidence in {"exec_command", "claude_write"} and owned_unit.path in owned_dirty_set
        }
        register_session_id = session_id or (tracker_run_id or f"transcript-{transcript.name}")
        tracker_units = [
            {
                "path": owned_unit.path,
                "unit": owned_unit.unit,
                "evidence": evidence,
                "hunk_anchor": owned_unit.hunk_anchor.to_dict() if owned_unit.hunk_anchor is not None else None,
            }
            for owned_unit, evidence in deduped_live_units
            if owned_unit.path in owned_dirty_set
        ]
        result = diff_tracker.register_claims(
            repo=root,
            session_id=register_session_id,
            run_id=tracker_run_id,
            worktree=root,
            branch=None,
            owned_paths=owned_dirty,
            apply_patch_paths=tracker_apply_patch_paths,
            exec_only_paths=tracker_exec_only_paths,
            owned_units_override=[
                (owned_unit, evidence)
                for owned_unit, evidence in deduped_live_units
                if owned_unit.path in owned_dirty_set
            ],
            log_root_override=tracker_log_root,
        )
        tracker_payload = result.to_dict()
        tracker_payload["session_id"] = register_session_id
        tracker_payload["owned_units"] = tracker_units
    elif not tracker_enabled:
        tracker_payload = {"status": "disabled_by_caller"}
    else:
        tracker_payload = {"status": "no_owned_dirty_paths"}

    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "transcript": str(transcript.resolve()),
        "session_id": session_id,
        "confidence": "medium" if apply_patch_operations or claude_write_events else "low",
        "ownership_baseline": baseline,
        "owned_paths": sorted(owned_paths),
        "owned_dirty_paths": owned_dirty,
        "unattributed_dirty_paths": unattributed_dirty,
        "apply_patch_operations": apply_patch_operations,
        "command_path_candidates": command_candidates,
        "command_events": command_events,
        "claude_write_events": claude_write_events,
        "tracker": tracker_payload,
        "warnings": [
            "Transcript-derived command side effects are conservative hints; without Pre/Post tool snapshots, shell writes cannot be fully attributed.",
            *baseline_warnings,
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an RVF session-scoped change manifest from a Codex JSONL transcript.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--transcript", required=True, help="Codex JSONL transcript / rollout path.")
    parser.add_argument("--output", help="Write manifest JSON to this path. Prints JSON to stdout when omitted.")
    parser.add_argument(
        "--no-tracker",
        action="store_true",
        help="Skip writing claims to the global reviewed-diff tracker. For tests and debugging only.",
    )
    parser.add_argument(
        "--tracker-run-id",
        help="Associate tracker claims with this RVF run id; falls back to environment / no run id.",
    )
    parser.add_argument(
        "--include-all-transcript-ownership",
        action="store_true",
        help="Debug compatibility mode: derive ownership from the full transcript instead of filtering at the live HEAD baseline.",
    )
    args = parser.parse_args()

    try:
        transcript = Path(args.transcript).expanduser().resolve()
        if not transcript.exists():
            raise ValueError(f"transcript not found: {transcript}")
        manifest = build_manifest(
            Path(args.repo).expanduser().resolve(),
            transcript,
            tracker_enabled=not args.no_tracker,
            tracker_run_id=args.tracker_run_id,
            include_all_transcript_ownership=args.include_all_transcript_ownership,
        )
    except Exception as exc:
        return fail(str(exc), 2)

    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).expanduser().resolve().write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
