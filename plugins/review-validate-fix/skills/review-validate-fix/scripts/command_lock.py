#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, start_run


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run_git_root(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return repo.resolve()


def default_lock_root() -> Path:
    return Path(os.environ.get("RVF_LOCK_DIR", str(Path(tempfile.gettempdir()) / "review-validate-fix-locks")))


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-")[:80] or "command"


def repo_lock_dir(repo: Path, lock_root: Path) -> Path:
    root = run_git_root(repo)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return lock_root / f"{sanitize_name(root.name)}-{digest}"


def lock_paths(repo: Path, lock_root: Path, name: str) -> tuple[Path, Path]:
    directory = repo_lock_dir(repo, lock_root)
    safe_name = sanitize_name(name)
    return directory / f"{safe_name}.lock", directory / f"{safe_name}.json"


def acquire(handle: Any, timeout: float, poll_interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if timeout <= 0 or time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval)


def lock_event_fields(
    *,
    repo: Path,
    name: str,
    command: list[str],
    lock_path: Path,
    metadata_path: Path,
    timeout: float,
    poll_interval: float,
    wait_duration_ms: int | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "lock_name": name,
        "command": command,
        "timeout_seconds": timeout,
        "poll_interval_seconds": poll_interval,
        "paths": {
            "lock": str(lock_path),
            "metadata": str(metadata_path),
        },
        "repo": str(run_git_root(repo)),
        "cwd": str(repo),
    }
    if wait_duration_ms is not None:
        fields["wait_duration_ms"] = wait_duration_ms
    return fields


def write_metadata(path: Path, repo: Path, name: str, command: list[str]) -> None:
    payload = {
        "repo": str(run_git_root(repo)),
        "name": name,
        "pid": os.getpid(),
        "command": command,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command under a repo-scoped review-validate-fix lock.")
    parser.add_argument("--repo", default=".", help="Repository or working directory used to scope the lock.")
    parser.add_argument("--name", required=True, help="Stable lock name, for example npm-test or playwright-server.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the lock.")
    parser.add_argument("--poll-interval", type=float, default=0.25, help="Seconds between lock acquisition attempts.")
    parser.add_argument("--lock-dir", default=str(default_lock_root()), help="Directory where lock files are stored.")
    parser.add_argument("--print-path", action="store_true", help="Print the lock file path and exit without running a command.")
    parser.add_argument("--rvf-run-id", help="Use an existing RVF run id instead of creating a new one.")
    parser.add_argument("--rvf-run-dir", help="Use this RVF run directory instead of resolving state/runs/<run_id>.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --, for example -- npm test.")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    root = run_git_root(repo)
    lock_root = Path(args.lock_dir).expanduser().resolve()
    lock_path, metadata_path = lock_paths(repo, lock_root, args.name)
    ledger: RunLedger | None = None

    if args.print_path:
        print(lock_path)
        return 0

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return fail("missing command; pass it after --", 2)

    ledger = start_run(
        "command-lock",
        repo=str(root),
        cwd=str(repo),
        run_id=args.rvf_run_id,
        run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
    )
    common_fields = lock_event_fields(
        repo=repo,
        name=args.name,
        command=command,
        lock_path=lock_path,
        metadata_path=metadata_path,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_started = time.monotonic()
        ledger.event(
            phase="review",
            event="lock_wait_started",
            status="started",
            reason_code="lock_wait_started",
            **common_fields,
        )
        if not acquire(handle, args.timeout, args.poll_interval):
            wait_duration_ms = int((time.monotonic() - wait_started) * 1000)
            holder = ""
            if metadata_path.exists():
                holder = metadata_path.read_text(encoding="utf-8").strip()
            detail = f"\ncurrent holder metadata:\n{holder}" if holder else ""
            ledger.event(
                phase="review",
                event="lock_timeout",
                status="failed",
                reason_code="lock_timeout",
                wait_duration_ms=wait_duration_ms,
                holder_metadata=holder or None,
                **common_fields,
            )
            ledger.summary(
                status="failed",
                reason_code="lock_timeout",
                message=f"timed out waiting for RVF lock {args.name}",
                wait_duration_ms=wait_duration_ms,
                holder_metadata=holder or None,
                **common_fields,
            )
            return fail(f"timed out waiting for RVF lock {args.name}: {lock_path}{detail}", 75)

        wait_duration_ms = int((time.monotonic() - wait_started) * 1000)
        write_metadata(metadata_path, repo, args.name, command)
        ledger.event(
            phase="review",
            event="lock_acquired",
            status="completed",
            reason_code="lock_acquired",
            wait_duration_ms=wait_duration_ms,
            **common_fields,
        )
        returncode = 1
        try:
            completed = subprocess.run(command, cwd=repo, check=False)
            returncode = completed.returncode
            return completed.returncode
        finally:
            try:
                metadata_path.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            ledger.event(
                phase="review",
                event="lock_released",
                status="completed" if returncode == 0 else "failed",
                reason_code="lock_released",
                returncode=returncode,
                **common_fields,
            )
            ledger.summary(
                status="completed" if returncode == 0 else "failed",
                reason_code="lock_released",
                message="RVF command lock released",
                returncode=returncode,
                **common_fields,
            )


if __name__ == "__main__":
    raise SystemExit(main())
