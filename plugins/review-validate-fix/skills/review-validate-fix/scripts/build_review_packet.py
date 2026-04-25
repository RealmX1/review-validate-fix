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


def untracked_files(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
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


def build_packet(
    repo: Path,
    session_context: Path | None,
    max_file_bytes: int,
    primary_files: list[str],
    background_files: list[str],
    allow_missing_session_context: bool = False,
) -> tuple[str, dict[str, Any]]:
    root = git_root(repo)
    generated = datetime.now(timezone.utc).isoformat()
    status = run_git(root, ["status", "--short", "-uall"]).rstrip()
    diff = run_git(root, ["diff", "--find-renames", "HEAD", "--"]).rstrip()
    untracked = untracked_files(root)
    session_context_text = ""
    if session_context is None:
        if not allow_missing_session_context:
            raise ValueError(
                "session context is required: write a main-agent work summary and pass "
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
        "status_bytes": len(status.encode("utf-8")),
        "diff_bytes": len(diff.encode("utf-8")),
        "untracked_count": len(untracked),
        "session_context_provided": bool(session_context_text),
        "session_context_bytes": len(session_context_text.encode("utf-8")),
        "primary_files": primary_files,
        "background_files": background_files,
        "untracked_files": [],
    }

    lines: list[str] = [
        "# Review Packet",
        "",
        f"Generated: {generated}",
        "",
        "All paths below are relative to the repository root. This packet is the review input; reviewers should not need the original working tree.",
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

    lines.extend(
        [
            "## Packet Stats",
            "",
            f"- tracked diff bytes: {metadata['diff_bytes']}",
            f"- untracked files: {metadata['untracked_count']}",
            f"- max inline file bytes: {max_file_bytes}",
            "",
        ]
    )
    lines.extend(["## Git Status", "", "```text", status or "(clean)", "```", ""])
    lines.extend(["## Git Diff HEAD", "", "```diff", diff or "(no tracked diff)", "```", ""])
    lines.extend(["## Untracked Files", ""])

    if not untracked:
        lines.extend(["(none)", ""])
    else:
        for rel in untracked:
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
    parser.add_argument("--output", help="Write packet to this file instead of stdout.")
    parser.add_argument("--metadata-output", help="Write packet metadata JSON to this file.")
    parser.add_argument("--max-file-bytes", type=int, default=200_000, help="Max untracked file bytes to inline.")
    parser.add_argument("--max-packet-bytes", type=int, default=0, help="Fail if the generated packet exceeds this many bytes. 0 disables the check.")
    parser.add_argument("--primary-file", action="append", default=[], help="Path known to be primary work for this turn. May be repeated.")
    parser.add_argument("--background-file", action="append", default=[], help="Path known to be pre-existing background WIP. May be repeated.")
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
        packet, metadata = build_packet(
            repo,
            session_context,
            args.max_file_bytes,
            args.primary_file,
            args.background_file,
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
