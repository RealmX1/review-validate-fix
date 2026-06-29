#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402

IGNORE_FILE = ".review-validate-fix-ignore"


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run_git(repo: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(run_git(repo, ["rev-parse", "--show-toplevel"]).strip()).resolve()


def normalize_exclude_prefix(prefix: str) -> str | None:
    normalized = prefix.strip().replace("\\", "/")
    if not normalized or normalized.startswith("#"):
        return None
    is_directory_prefix = normalized.endswith("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    return f"{normalized}/" if is_directory_prefix else normalized


def load_exclude_prefixes(repo: Path, extra_prefixes: list[str]) -> list[str]:
    prefixes: list[str] = []
    ignore_file = repo / IGNORE_FILE
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            normalized = normalize_exclude_prefix(line)
            if normalized is not None:
                prefixes.append(normalized)
    for prefix in extra_prefixes:
        normalized = normalize_exclude_prefix(prefix)
        if normalized is not None:
            prefixes.append(normalized)
    return sorted(set(prefixes))


def exclude_pathspecs(exclude_prefixes: list[str]) -> list[str]:
    pathspecs: list[str] = []
    for prefix in exclude_prefixes:
        escaped = prefix.replace("\\", "\\\\")
        for char in "*?[]":
            escaped = escaped.replace(char, f"\\{char}")
        pathspecs.append(f":(exclude,top,glob){escaped}*")
    return pathspecs


def untracked_files(repo: Path, exclude_prefixes: list[str]) -> list[str]:
    args = ["git", "ls-files", "--others", "--exclude-standard", "-z"]
    if exclude_prefixes:
        args.extend(["--", ".", *exclude_pathspecs(exclude_prefixes)])
    completed = subprocess.run(
        args,
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    return sorted(item.decode("utf-8", "surrogateescape") for item in completed.stdout.split(b"\0") if item)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def markdown_fence(text: str) -> str:
    longest = 0
    current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(3, longest + 1)


def read_text(path: Path, max_bytes: int) -> tuple[str | None, dict[str, Any]]:
    size = path.stat().st_size
    digest = sha256_file(path)
    info: dict[str, Any] = {"size": size, "sha256": digest}
    if size > max_bytes:
        info.update({"omitted": True, "reason": f"size exceeds max_file_bytes={max_bytes}"})
        return None, info
    data = path.read_bytes()
    if b"\0" in data:
        info.update({"omitted": True, "reason": "binary file"})
        return None, info
    info["omitted"] = False
    return data.decode("utf-8", "replace"), info


def note_from_info(info: dict[str, Any]) -> str:
    if info.get("omitted"):
        return f"omitted: {info['reason']}; {info['size']} bytes; sha256={info['sha256']}"
    return f"{info['size']} bytes; sha256={info['sha256']}"


def load_session_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"session manifest must be a JSON object: {path}")
    return payload


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def session_owned_paths(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    return sorted(set(string_list(manifest.get("owned_paths"))) | set(string_list(manifest.get("owned_dirty_paths"))))


def session_owned_dirty_paths(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    if "owned_dirty_paths" not in manifest:
        return session_owned_paths(manifest)
    dirty = string_list(manifest.get("owned_dirty_paths"))
    return sorted(set(dirty))


def validate_session_manifest(manifest: dict[str, Any] | None, root: Path, path: Path | None) -> None:
    if manifest is None:
        return
    manifest_repo = manifest.get("repo")
    if not isinstance(manifest_repo, str) or not manifest_repo.strip():
        raise ValueError(f"session manifest is missing repo: {path}")
    if Path(manifest_repo).expanduser().resolve() != root:
        raise ValueError(f"session manifest repo does not match current repo: {manifest_repo} != {root}")
    if not string_list(manifest.get("owned_paths")):
        raise ValueError(f"session manifest has no owned paths; refusing to build empty Session-Owned scope: {path}")


def diff_for_paths(repo: Path, paths: list[str], exclude_prefixes: list[str]) -> str:
    if not paths:
        return ""
    args = ["diff", "--find-renames", "HEAD", "--", *paths]
    if exclude_prefixes:
        args.extend(exclude_pathspecs(exclude_prefixes))
    return run_git(repo, args).rstrip()


def filter_diff_for_hunk_headers(diff_text: str, allowed_headers: set[str]) -> str:
    if not diff_text or not allowed_headers:
        return diff_text.rstrip()
    output: list[str] = []
    file_header: list[str] = []
    in_allowed_hunk = False
    in_any_hunk = False
    emitted_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            file_header = [line]
            in_allowed_hunk = False
            in_any_hunk = False
            emitted_header = False
            continue
        if line.startswith("@@"):
            in_any_hunk = True
            normalized = reviewable_unit_diff_tracker._normalize_header(line)
            in_allowed_hunk = normalized in allowed_headers
            if in_allowed_hunk and not emitted_header:
                output.extend(file_header)
                emitted_header = True
            if in_allowed_hunk:
                output.append(line)
            continue
        if not emitted_header and not in_any_hunk:
            file_header.append(line)
            continue
        if in_allowed_hunk:
            output.append(line)
    return "\n".join(output).rstrip()


def diff_for_tracker_scope(repo: Path, tracker_scope: dict[str, Any], exclude_prefixes: list[str]) -> str:
    paths = [path for path in tracker_scope.get("paths", []) if isinstance(path, str)]
    if not paths:
        return ""
    hunk_headers_by_path: dict[str, set[str]] = {}
    path_level_paths: set[str] = set()
    for entry in tracker_scope.get("hunks", []):
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        header = entry.get("hunk_header")
        if isinstance(header, str) and header:
            hunk_headers_by_path.setdefault(path, set()).add(reviewable_unit_diff_tracker._normalize_header(header))
        else:
            path_level_paths.add(path)
    chunks: list[str] = []
    for path in paths:
        if path in path_level_paths or path not in hunk_headers_by_path:
            chunk = diff_for_paths(repo, [path], exclude_prefixes)
        else:
            chunk = filter_diff_for_hunk_headers(
                diff_for_paths(repo, [path], exclude_prefixes),
                hunk_headers_by_path[path],
            )
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks).rstrip()


def build_packet(
    repo: Path,
    session_context: Path | None,
    session_manifest_path: Path | None,
    max_file_bytes: int,
    primary_files: list[str],
    background_files: list[str],
    exclude_prefixes: list[str] | None = None,
    allow_missing_session_context: bool = False,
) -> tuple[str, dict[str, Any]]:
    root = git_root(repo)
    all_exclude_prefixes = load_exclude_prefixes(root, exclude_prefixes or [])
    generated = datetime.now(timezone.utc).isoformat()
    status_args = ["status", "--short", "-uall"]
    diff_args = ["diff", "--find-renames", "HEAD", "--"]
    if all_exclude_prefixes:
        status_args.extend(["--", ".", *exclude_pathspecs(all_exclude_prefixes)])
        diff_args.extend([".", *exclude_pathspecs(all_exclude_prefixes)])
    status = run_git(root, status_args).rstrip()
    diff = run_git(root, diff_args).rstrip()
    untracked = untracked_files(root, all_exclude_prefixes)
    session_manifest = load_session_manifest(session_manifest_path)
    validate_session_manifest(session_manifest, root, session_manifest_path)
    owned_paths = session_owned_paths(session_manifest)

    cross_session_conflicts: list[dict[str, Any]] = []
    tracker_status: str | None = None
    tracker_repo_key: str | None = None
    tracker_scope: dict[str, Any] | None = None
    if session_manifest is not None:
        tracker_meta = session_manifest.get("tracker") if isinstance(session_manifest.get("tracker"), dict) else None
        if tracker_meta is not None:
            tracker_status = tracker_meta.get("status") if isinstance(tracker_meta.get("status"), str) else None
            tracker_repo_key = tracker_meta.get("repo_key") if isinstance(tracker_meta.get("repo_key"), str) else None
            current_session = tracker_meta.get("session_id") or session_manifest.get("session_id") or ""
            if isinstance(current_session, str) and current_session:
                owned_units = reviewable_unit_diff_tracker.owned_units_from_manifest(session_manifest)
                if owned_units:
                    conflicts = reviewable_unit_diff_tracker.list_conflicts(
                        root,
                        current_session_id=current_session,
                        owned_units=owned_units,
                    )
                    cross_session_conflicts = [conflict.to_dict() for conflict in conflicts]
            scope_candidate = tracker_meta.get("tracker_scope")
            if isinstance(scope_candidate, dict) and (
                isinstance(scope_candidate.get("unit_ids"), list)
                and isinstance(scope_candidate.get("lease_id"), str)
                and isinstance(scope_candidate.get("scope_hash"), str)
                and isinstance(scope_candidate.get("paths"), list)
            ):
                tracker_scope = scope_candidate
    session_context_text = ""
    if session_context is None:
        if not allow_missing_session_context:
            raise ValueError(
                "session context is required: write a main-agent scope-of-work summary and pass "
                "--session-context <file>; use --allow-missing-session-context only for debug"
            )
    else:
        session_context_text = session_context.read_text(encoding="utf-8").strip()
        if not session_context_text and not allow_missing_session_context:
            raise ValueError(f"session context file is empty: {session_context}")
    metadata: dict[str, Any] = {
        "generated": generated,
        "repo": str(root),
        "max_file_bytes": max_file_bytes,
        "ignore_file": IGNORE_FILE,
        "excluded_path_prefixes": all_exclude_prefixes,
        "status_bytes": len(status.encode("utf-8")),
        "diff_bytes": len(diff.encode("utf-8")),
        "untracked_count": len(untracked),
        "session_context_provided": bool(session_context_text),
        "session_context_bytes": len(session_context_text.encode("utf-8")),
        "scope_of_work_file": str(session_context) if session_context is not None else None,
        "session_manifest_file": str(session_manifest_path) if session_manifest_path is not None else None,
        "session_manifest_provided": session_manifest is not None,
        "session_manifest_confidence": session_manifest.get("confidence") if session_manifest is not None else None,
        "session_owned_paths": owned_paths,
        "session_owned_path_count": len(owned_paths),
        "session_owned_dirty_paths": string_list(session_manifest.get("owned_dirty_paths")) if session_manifest is not None else [],
        "unattributed_dirty_paths": string_list(session_manifest.get("unattributed_dirty_paths")) if session_manifest is not None else [],
        "owned_untracked_count": 0,
        "background_untracked_count": 0,
        "primary_files": primary_files,
        "background_files": background_files,
        "untracked_files": [],
        "tracker_status": tracker_status,
        "tracker_repo_key": tracker_repo_key,
        "cross_session_conflicts": cross_session_conflicts,
        "tracker_scope_present": tracker_scope is not None,
        "tracker_scope_unit_count": len(tracker_scope["unit_ids"]) if tracker_scope is not None else 0,
        "tracker_scope_lease_id": tracker_scope["lease_id"] if tracker_scope is not None else None,
        "tracker_scope_hash": tracker_scope["scope_hash"] if tracker_scope is not None else None,
        "tracker_scope_paths": [
            path for path in tracker_scope["paths"] if isinstance(path, str)
        ] if tracker_scope is not None else [],
        "tracker_scope_source_session_id": tracker_scope.get("source_session_id")
        if tracker_scope is not None and isinstance(tracker_scope.get("source_session_id"), str)
        else None,
        "tracker_scope_takeover_from_session_id": tracker_scope.get("takeover_from_session_id")
        if tracker_scope is not None and isinstance(tracker_scope.get("takeover_from_session_id"), str)
        else None,
    }
    owned_dirty_paths = session_owned_dirty_paths(session_manifest)
    owned_diff_paths = [
        path for path in metadata["tracker_scope_paths"] if isinstance(path, str)
    ] if tracker_scope is not None else owned_dirty_paths
    owned_path_set = set(owned_diff_paths)
    if tracker_scope is not None:
        owned_diff = diff_for_tracker_scope(root, tracker_scope, all_exclude_prefixes)
    else:
        owned_diff = diff_for_paths(root, owned_diff_paths, all_exclude_prefixes) if session_manifest is not None else ""
    owned_untracked = [path for path in untracked if path in owned_path_set]
    background_untracked = [path for path in untracked if path not in owned_path_set]
    metadata["session_owned_diff_paths"] = owned_diff_paths
    metadata["owned_untracked_count"] = len(owned_untracked) if session_manifest is not None else len(untracked)
    metadata["background_untracked_count"] = len(background_untracked) if session_manifest is not None else 0

    lines: list[str] = [
        "# Review Packet",
        "",
        f"Generated: {generated}",
        "",
        "All paths below are relative to the repository root. This packet is the review input; reviewers should use the provided scope-of-work file as the scope anchor and use this packet as evidence.",
        "",
        "## Runtime environment notes",
        "",
        "- Reviewer/validator subagents may run inside RVF task worktrees where `.venv` is a symlink to the parent worktree. If `.venv/bin/python` cannot import the modules under review, prefer `python3 -m <module>` against the system interpreter or create a temporary venv in the task worktree; do NOT depend on the parent worktree's `.venv`.",
        "- For Python tests, prefer `python3 -m pytest <target>`. Direct `pytest` may be missing on PATH inside a fresh task worktree.",
        "- Do not invoke validation under `~/Documents/GitHub/...` checkout state; runtime state is canonical at `~/plugins/review-validate-fix/skills/review-validate-fix/state/`.",
        "- 本 review-packet 反映 RVF dispatch 触发瞬间 origin worktree 的 dirty snapshot；origin 之后的继续编辑不会回流到本 task worktree。`unattributed dirty paths` 列于下方仅作上下文，**不属于本轮 review 范围**。",
        "",
    ]

    if primary_files or background_files:
        lines.extend(["## Review Scope", ""])
        if primary_files:
            lines.extend(["Primary files for this turn:"])
            lines.extend(f"- {path}" for path in primary_files)
            lines.append("")
        if background_files:
            lines.extend(["Background WIP files already present before this turn:"])
            lines.extend(f"- {path}" for path in background_files)
            lines.append("")

    if session_context_text:
        lines.extend(["## Session Context", "", session_context_text, ""])

    if session_manifest is not None:
        lines.extend(
            [
                "## Session Manifest",
                "",
                "This manifest is the session-scoped ownership anchor. Reviewers must treat owned paths as the default review scope and treat unattributed dirty paths as background WIP unless a session-owned change directly depends on them.",
                "",
                f"- manifest file: `{session_manifest_path}`",
                f"- session id: `{session_manifest.get('session_id') or '(unknown)'}`",
                f"- confidence: `{session_manifest.get('confidence') or 'unknown'}`",
                "",
                "Session-owned paths:",
            ]
        )
        if owned_paths:
            lines.extend(f"- {path}" for path in owned_paths)
        else:
            lines.append("(none)")
        lines.extend(["", "Unattributed dirty paths (background, not default review scope):"])
        unattributed = string_list(session_manifest.get("unattributed_dirty_paths"))
        if unattributed:
            lines.extend(f"- {path}" for path in unattributed)
        else:
            lines.append("(none)")
        lines.append("")
        patch_ownership = session_manifest.get("patch_ownership")
        if isinstance(patch_ownership, dict):
            unresolved = patch_ownership.get("unresolved_owned_patch_hunks")
            expected_paths = string_list(patch_ownership.get("expected_apply_patch_paths"))
            expected_units = string_list(patch_ownership.get("expected_apply_patch_unit_ids"))
            lines.extend(
                [
                    "Patch ownership audit:",
                    f"- transcript max line: `{patch_ownership.get('transcript_max_line_number') or '(none)'}`",
                    f"- expected apply_patch paths: `{len(expected_paths)}`",
                    f"- expected apply_patch units: `{len(expected_units)}`",
                    f"- unresolved owned patch hunks: `{len(unresolved) if isinstance(unresolved, list) else 0}`",
                    "",
                ]
            )
        edit_claims = session_manifest.get("edit_claims")
        if isinstance(edit_claims, list):
            latest_user_lines = sorted(
                {
                    item.get("latest_user_line_number")
                    for item in edit_claims
                    if isinstance(item, dict) and isinstance(item.get("latest_user_line_number"), int)
                }
            )
            lines.extend(
                [
                    "Edit claim audit:",
                    f"- edit claim count: `{len(edit_claims)}`",
                    f"- latest user context line count: `{len(latest_user_lines)}`",
                    "",
                ]
            )

    if tracker_scope is not None:
        lines.extend(
            [
                "## Tracker Scope",
                "",
                "This section is the allocator-assigned scope anchor. Reviewers must treat the listed unit_ids as authoritative scope; `## Allocated Git Diff` below is the path-projection.",
                "",
                f"- lease id: `{tracker_scope['lease_id']}`",
                f"- scope hash: `{tracker_scope['scope_hash']}`",
                f"- transcript max line: `{tracker_scope.get('transcript_max_line_number') or '(none)'}`",
            ]
        )
        source_session = tracker_scope.get("source_session_id")
        if isinstance(source_session, str) and source_session.strip():
            lines.append(f"- source session: `{source_session}`")
        else:
            lines.append("- source session: (none)")
        takeover_session = tracker_scope.get("takeover_from_session_id")
        if isinstance(takeover_session, str) and takeover_session.strip():
            lines.append(f"- takeover from session: `{takeover_session}`")
        else:
            lines.append("- takeover from session: (none)")
        lines.append("")
        scope_unit_ids = [uid for uid in tracker_scope["unit_ids"] if isinstance(uid, str)]
        scope_hunks_raw = tracker_scope.get("hunks")
        scope_hunks = [h for h in scope_hunks_raw if isinstance(h, dict)] if isinstance(scope_hunks_raw, list) else []
        if scope_hunks:
            hunks_by_unit: dict[str, list[dict[str, Any]]] = {}
            for entry in scope_hunks:
                uid = entry.get("unit_id")
                if isinstance(uid, str):
                    hunks_by_unit.setdefault(uid, []).append(entry)
            lines.append("Allocated units:")
            for uid in scope_unit_ids:
                entries = hunks_by_unit.get(uid, [])
                if entries:
                    head = entries[0]
                    path_text = head.get("path") if isinstance(head.get("path"), str) else "(unknown path)"
                    hunk_header = head.get("hunk_header") if isinstance(head.get("hunk_header"), str) else None
                    if hunk_header:
                        lines.append(f"- `{uid}` — path `{path_text}` — hunk `{hunk_header}`")
                    else:
                        lines.append(f"- `{uid}` — path `{path_text}` — (path-level)")
                else:
                    lines.append(f"- `{uid}` — (no hunk metadata)")
            lines.append("")
        else:
            lines.append("Allocated unit_ids:")
            for uid in scope_unit_ids:
                lines.append(f"- `{uid}`")
            lines.append("")
        scope_paths = [path for path in tracker_scope["paths"] if isinstance(path, str)]
        lines.append("Allocated paths:")
        if scope_paths:
            lines.extend(f"- {path}" for path in scope_paths)
        else:
            lines.append("(none — units are hunk-only; see allocated units above)")
        lines.append("")

    if cross_session_conflicts:
        lines.extend(
            [
                "## Cross-Session Conflicts",
                "",
                "Other live RVF sessions in this clone hold claims that overlap with the current session-owned scope. Reviewers should treat these as scope contention: prefer issuing a `lock-request` review-result artifact and let the main session resolve before re-running.",
                "",
            ]
        )
        for conflict in cross_session_conflicts:
            anchor = f" `{conflict['hunk_header']}`" if conflict.get("hunk_header") else ""
            run_id = conflict.get("other_run_id") or "(unknown run)"
            branch = conflict.get("other_branch") or "(unknown branch)"
            worktree = conflict.get("other_worktree") or "(unknown worktree)"
            last_seen = conflict.get("last_seen_at") or "(unknown)"
            lines.append(
                f"- `{conflict['path']}`{anchor} — claimed by session `{conflict['other_session_id']}` "
                f"(run `{run_id}`, branch `{branch}`, worktree `{worktree}`, last_seen `{last_seen}`)"
            )
        lines.append("")

    lines.extend(
        [
            "## Packet Stats",
            "",
            f"- tracked diff bytes: {metadata['diff_bytes']}",
            f"- untracked files: {metadata['untracked_count']}",
            f"- session-owned paths: {metadata['session_owned_path_count']}",
            f"- max inline file bytes: {max_file_bytes}",
            "",
        ]
    )
    lines.extend(["## Excluded Paths", ""])
    if all_exclude_prefixes:
        lines.append(f"Source ignore file: `{IGNORE_FILE}` when present, plus any `--exclude-path-prefix` arguments.")
        lines.append("")
        lines.extend(f"- {prefix}" for prefix in all_exclude_prefixes)
        lines.append("")
    else:
        lines.extend(["(none)", ""])
    lines.extend(["## Git Status", "", "```text", status or "(clean)", "```", ""])
    if tracker_scope is not None:
        scope_paths = [path for path in tracker_scope["paths"] if isinstance(path, str)]
        if scope_paths:
            allocated_body = owned_diff or "(no tracked diff for allocated paths)"
        else:
            allocated_body = "(allocator did not allocate any path; see Tracker Scope hunks above for unit-level scope)"
        lines.extend(
            [
                "## Allocated Git Diff",
                "",
                "Path-limited diff over the allocator-assigned paths. Reviewers must treat the listed unit_ids in `## Tracker Scope` as authoritative scope; this diff is the path projection.",
                "",
                "```diff",
                allocated_body,
                "```",
                "",
                "## Full Git Diff HEAD (Evidence Only)",
                "",
                "This full dirty diff may include other sessions' work. Use it only as supporting evidence for direct dependencies from session-owned changes.",
                "",
                "```diff",
                diff or "(no tracked diff)",
                "```",
                "",
            ]
        )
    elif session_manifest is not None:
        lines.extend(
            [
                "## Session-Owned Git Diff",
                "",
                "Path-limited diff for session-owned paths. This is the default code review scope.",
                "",
                "```diff",
                owned_diff or "(no tracked diff for session-owned paths)",
                "```",
                "",
                "## Full Git Diff HEAD (Evidence Only)",
                "",
                "This full dirty diff may include other sessions' work. Use it only as supporting evidence for direct dependencies from session-owned changes.",
                "",
                "```diff",
                diff or "(no tracked diff)",
                "```",
                "",
            ]
        )
    else:
        lines.extend(["## Git Diff HEAD", "", "```diff", diff or "(no tracked diff)", "```", ""])
    lines.extend(["## Untracked Files", ""])

    if not untracked:
        lines.extend(["(none)", ""])
    else:
        if session_manifest is not None and background_untracked:
            lines.extend(
                [
                    "Background untracked paths below were not attributed to this session and are not inlined:",
                    "",
                ]
            )
            lines.extend(f"- {path}" for path in background_untracked)
            lines.append("")
        inline_untracked = owned_untracked if session_manifest is not None else untracked
        if session_manifest is not None:
            lines.extend(["Session-owned untracked file contents:", ""])
        for rel in inline_untracked:
            path = root / rel
            lines.extend([f"### {rel}", ""])
            if not path.is_file():
                metadata["untracked_files"].append({"path": rel, "omitted": True, "reason": "not a regular file"})
                lines.extend([f"omitted: not a regular file at packet build time", ""])
                continue
            content, info = read_text(path, max_file_bytes)
            info["path"] = rel
            metadata["untracked_files"].append(info)
            lines.extend([note_from_info(info), ""])
            if content is not None:
                fence = markdown_fence(content)
                lines.extend([f"{fence}text", content.rstrip("\n"), fence, ""])

    packet = "\n".join(lines).rstrip() + "\n"
    metadata["packet_bytes"] = len(packet.encode("utf-8"))
    metadata["inlined_untracked_count"] = sum(1 for item in metadata["untracked_files"] if not item.get("omitted"))
    metadata["omitted_untracked_count"] = sum(1 for item in metadata["untracked_files"] if item.get("omitted"))
    return packet, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a self-contained review packet for review-validate-fix.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--session-context", help="Required file containing the main-agent work summary.")
    parser.add_argument("--session-manifest", help="Optional JSON manifest describing paths owned by this Codex session.")
    parser.add_argument("--output", help="Write packet to this file instead of stdout.")
    parser.add_argument("--metadata-output", help="Write packet metadata JSON to this file.")
    parser.add_argument("--max-file-bytes", type=int, default=200_000, help="Max untracked file bytes to inline.")
    parser.add_argument("--max-packet-bytes", type=int, default=0, help="Fail if the generated packet exceeds this many bytes. 0 disables the check.")
    parser.add_argument("--primary-file", action="append", default=[], help="Path known to be primary work for this turn. May be repeated.")
    parser.add_argument("--background-file", action="append", default=[], help="Path known to be pre-existing background WIP. May be repeated.")
    parser.add_argument("--exclude-path-prefix", action="append", default=[], help=f"Path prefix to omit from status, diff, and untracked packet sections. May be repeated. {IGNORE_FILE} is also honored when present.")
    parser.add_argument(
        "--allow-missing-session-context",
        action="store_true",
        help="Debug-only escape hatch. Normal review runs must pass --session-context.",
    )
    args = parser.parse_args()

    try:
        repo = Path(args.repo).expanduser().resolve()
        session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        session_manifest = Path(args.session_manifest).expanduser().resolve() if args.session_manifest else None
        if session_manifest is not None and not session_manifest.exists():
            raise ValueError(f"session manifest file not found: {session_manifest}")
        packet, metadata = build_packet(
            repo,
            session_context,
            session_manifest,
            args.max_file_bytes,
            args.primary_file,
            args.background_file,
            args.exclude_path_prefix,
            args.allow_missing_session_context,
        )
        if args.max_packet_bytes and metadata["packet_bytes"] > args.max_packet_bytes:
            raise ValueError(
                f"packet size {metadata['packet_bytes']} exceeds max_packet_bytes={args.max_packet_bytes}; "
                "lower --max-file-bytes or split the run context"
            )
    except Exception as exc:
        return fail(str(exc), 2)

    if args.output:
        Path(args.output).expanduser().resolve().write_text(packet, encoding="utf-8")
    else:
        print(packet, end="")
    if args.metadata_output:
        Path(args.metadata_output).expanduser().resolve().write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
