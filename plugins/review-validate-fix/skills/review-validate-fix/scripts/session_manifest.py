#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REDIRECT_RE = re.compile(r"(?:^|\s)(?:>>?|1>|2>|&>)\s*(?P<path>[^&|;\s]+)")


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


def parse_apply_patch(repo: Path, patch_text: str, line_number: int) -> tuple[list[dict[str, Any]], set[str]]:
    operations: list[dict[str, Any]] = []
    paths: set[str] = set()
    current: dict[str, Any] | None = None
    for raw_line in patch_text.splitlines():
        if raw_line.startswith("*** Add File: "):
            rel = normalize_repo_path(repo, raw_line.removeprefix("*** Add File: "))
            if rel is None:
                current = None
                continue
            current = {"operation": "add", "path": rel, "line_number": line_number}
            operations.append(current)
            paths.add(rel)
            continue
        if raw_line.startswith("*** Delete File: "):
            rel = normalize_repo_path(repo, raw_line.removeprefix("*** Delete File: "))
            if rel is None:
                current = None
                continue
            current = {"operation": "delete", "path": rel, "line_number": line_number}
            operations.append(current)
            paths.add(rel)
            continue
        if raw_line.startswith("*** Update File: "):
            rel = normalize_repo_path(repo, raw_line.removeprefix("*** Update File: "))
            if rel is None:
                current = None
                continue
            current = {"operation": "update", "path": rel, "line_number": line_number}
            operations.append(current)
            paths.add(rel)
            continue
        if raw_line.startswith("*** Move to: ") and current is not None:
            rel = normalize_repo_path(repo, raw_line.removeprefix("*** Move to: "))
            if rel is None:
                continue
            current["move_to"] = rel
            paths.add(rel)
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


def build_manifest(repo: Path, transcript: Path) -> dict[str, Any]:
    root = git_root(repo)
    records = parse_jsonl(transcript)
    owned_paths: set[str] = set()
    apply_patch_operations: list[dict[str, Any]] = []
    command_candidates: list[dict[str, Any]] = []
    command_events: list[dict[str, Any]] = []

    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        tool_name = payload_tool_name(payload)
        if tool_name == "apply_patch":
            patch_text = payload_tool_input(payload)
            if not patch_text:
                continue
            operations, paths = parse_apply_patch(root, patch_text, int(record.get("_line_number", 0)))
            apply_patch_operations.extend(operations)
            owned_paths.update(paths)
            continue
        if tool_name != "exec_command":
            continue
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
        command_events.append(event)

    dirty = dirty_paths(root)
    owned_dirty = sorted(path for path in dirty if path in owned_paths)
    unattributed_dirty = sorted(path for path in dirty if path not in owned_paths)
    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "transcript": str(transcript.resolve()),
        "session_id": session_id_from_records(records),
        "confidence": "medium" if apply_patch_operations else "low",
        "owned_paths": sorted(owned_paths),
        "owned_dirty_paths": owned_dirty,
        "unattributed_dirty_paths": unattributed_dirty,
        "apply_patch_operations": apply_patch_operations,
        "command_path_candidates": command_candidates,
        "command_events": command_events,
        "warnings": [
            "Transcript-derived command side effects are conservative hints; without Pre/Post tool snapshots, shell writes cannot be fully attributed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an RVF session-scoped change manifest from a Codex JSONL transcript.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--transcript", required=True, help="Codex JSONL transcript / rollout path.")
    parser.add_argument("--output", help="Write manifest JSON to this path. Prints JSON to stdout when omitted.")
    args = parser.parse_args()

    try:
        transcript = Path(args.transcript).expanduser().resolve()
        if not transcript.exists():
            raise ValueError(f"transcript not found: {transcript}")
        manifest = build_manifest(Path(args.repo).expanduser().resolve(), transcript)
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
