#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import start_run


SKILL_DIR = Path(__file__).resolve().parents[1]
BUILD_PACKET = SKILL_DIR / "scripts" / "build_review_packet.py"
WORKSPACE_SNAPSHOT = SKILL_DIR / "scripts" / "workspace_snapshot.py"
SESSION_MANIFEST = SKILL_DIR / "scripts" / "session_manifest.py"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run(cmd: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], cwd=repo).strip()).resolve()


def safe_repo_name(repo: Path) -> str:
    name = repo.name or "repo"
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in name)[:80] or "repo"


def default_base_dir() -> Path:
    return Path(tempfile.gettempdir()) / "review-validate-fix-runs"


def review_env_exports(
    *,
    repo: Path,
    run_id: str,
    run_dir: Path,
    artifacts_dir: Path,
    scope_of_work_path: Path | None,
    session_manifest_path: Path | None,
    packet_path: Path,
    metadata_path: Path,
    snapshot_path: Path,
) -> tuple[dict[str, str], str]:
    env: dict[str, str] = {
        "RVF_REPO": str(repo),
        "RVF_RUN_ID": run_id,
        "RVF_RUN_DIR": str(run_dir),
        "RVF_ARTIFACTS_DIR": str(artifacts_dir),
        "RVF_REVIEW_PACKET": str(packet_path),
        "RVF_REVIEW_PACKET_METADATA": str(metadata_path),
        "RVF_BEFORE_WORKSPACE_SNAPSHOT": str(snapshot_path),
        "RVF_COMMAND_LOCK": str(COMMAND_LOCK),
    }
    if scope_of_work_path is not None:
        env["RVF_SCOPE_OF_WORK"] = str(scope_of_work_path)
        env["RVF_SESSION_CONTEXT"] = str(scope_of_work_path)
    if session_manifest_path is not None:
        env["RVF_SESSION_MANIFEST"] = str(session_manifest_path)

    lines = [
        "# Source this file in review subprocesses to avoid repeating long RVF paths.",
        f"export RVF_REPO={shlex.quote(env['RVF_REPO'])}",
        f"export RVF_RUN_ID={shlex.quote(env['RVF_RUN_ID'])}",
        f"export RVF_RUN_DIR={shlex.quote(env['RVF_RUN_DIR'])}",
    ]
    if artifacts_dir == run_dir / "artifacts":
        lines.append('export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"')
    else:
        lines.append(f"export RVF_ARTIFACTS_DIR={shlex.quote(env['RVF_ARTIFACTS_DIR'])}")
    if scope_of_work_path is not None:
        lines.append('export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"')
        lines.append('export RVF_SESSION_CONTEXT="$RVF_SCOPE_OF_WORK"')
    if session_manifest_path is not None:
        lines.append('export RVF_SESSION_MANIFEST="$RVF_ARTIFACTS_DIR/session-manifest.json"')
    lines.extend(
        [
            'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"',
            'export RVF_REVIEW_PACKET_METADATA="$RVF_ARTIFACTS_DIR/review-packet.metadata.json"',
            'export RVF_BEFORE_WORKSPACE_SNAPSHOT="$RVF_ARTIFACTS_DIR/before-workspace-snapshot.json"',
            f"export RVF_COMMAND_LOCK={shlex.quote(env['RVF_COMMAND_LOCK'])}",
            "",
        ]
    )
    return env, "\n".join(lines)


def prepare_run(
    *,
    repo: Path,
    session_context: Path | None,
    session_manifest: Path | None,
    transcript: Path | None,
    base_dir: Path,
    max_file_bytes: int,
    max_packet_bytes: int,
    primary_files: list[str],
    background_files: list[str],
    exclude_path_prefixes: list[str],
    allow_missing_session_context: bool = False,
    rvf_run_id: str | None = None,
    rvf_run_dir: Path | None = None,
) -> dict[str, Any]:
    root = git_root(repo)
    ledger = start_run(
        "prepare-run",
        repo=str(root),
        cwd=str(root),
        run_id=rvf_run_id,
        run_dir=rvf_run_dir,
    )
    ledger.event(
        phase="prepare",
        event="started",
        status="started",
        reason_code="prepare_started",
        repo=str(root),
        cwd=str(root),
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    if ledger.available:
        artifact_dir = ledger.artifacts_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_dir = Path(tempfile.mkdtemp(prefix=f"{timestamp}-{safe_repo_name(root)}-", dir=base_dir))

    packet_path = artifact_dir / "review-packet.md"
    metadata_path = artifact_dir / "review-packet.metadata.json"
    snapshot_path = artifact_dir / "before-workspace-snapshot.json"
    scope_of_work_path = artifact_dir / "scope-of-work.md"
    session_manifest_path = artifact_dir / "session-manifest.json"
    review_env_path = artifact_dir / "review-env.sh"

    packet_session_context = session_context
    if session_context is not None:
        shutil.copyfile(session_context, scope_of_work_path)
        packet_session_context = scope_of_work_path

    packet_session_manifest: Path | None = None
    source_session_manifest: str | None = None
    if transcript is not None:
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(root),
                "--transcript",
                str(transcript),
                "--output",
                str(session_manifest_path),
            ]
        )
        packet_session_manifest = session_manifest_path
        source_session_manifest = f"transcript:{transcript}"
    elif session_manifest is not None:
        shutil.copyfile(session_manifest, session_manifest_path)
        packet_session_manifest = session_manifest_path
        source_session_manifest = str(session_manifest)

    packet_cmd = [
        sys.executable,
        str(BUILD_PACKET),
        "--repo",
        str(root),
        "--output",
        str(packet_path),
        "--metadata-output",
        str(metadata_path),
        "--max-file-bytes",
        str(max_file_bytes),
    ]
    if max_packet_bytes:
        packet_cmd.extend(["--max-packet-bytes", str(max_packet_bytes)])
    if packet_session_context is not None:
        packet_cmd.extend(["--session-context", str(packet_session_context)])
    if packet_session_manifest is not None:
        packet_cmd.extend(["--session-manifest", str(packet_session_manifest)])
    if allow_missing_session_context:
        packet_cmd.append("--allow-missing-session-context")
    for path in primary_files:
        packet_cmd.extend(["--primary-file", path])
    for path in background_files:
        packet_cmd.extend(["--background-file", path])
    for prefix in exclude_path_prefixes:
        packet_cmd.extend(["--exclude-path-prefix", prefix])
    run(packet_cmd)

    run(
        [
            sys.executable,
            str(WORKSPACE_SNAPSHOT),
            "capture",
            "--repo",
            str(root),
            "--output",
            str(snapshot_path),
        ]
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    scope_path = scope_of_work_path if session_context is not None else None
    manifest_path = session_manifest_path if packet_session_manifest is not None else None
    review_env, review_env_text = review_env_exports(
        repo=root,
        run_id=ledger.run_id,
        run_dir=ledger.run_dir,
        artifacts_dir=artifact_dir,
        scope_of_work_path=scope_path,
        session_manifest_path=manifest_path,
        packet_path=packet_path,
        metadata_path=metadata_path,
        snapshot_path=snapshot_path,
    )
    review_env_path.write_text(review_env_text, encoding="utf-8")
    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "events_path": str(ledger.events_path),
        "summary_path": str(ledger.summary_path),
        "artifacts_dir": str(artifact_dir),
        "review_packet": str(packet_path),
        "review_packet_metadata": str(metadata_path),
        "before_workspace_snapshot": str(snapshot_path),
        "scope_of_work_file": str(scope_of_work_path) if session_context is not None else None,
        "session_manifest_file": str(session_manifest_path) if packet_session_manifest is not None else None,
        "review_env_file": str(review_env_path),
        "review_env": review_env,
        "source_session_context": str(session_context) if session_context is not None else None,
        "source_session_manifest": source_session_manifest,
        "packet_bytes": metadata.get("packet_bytes"),
        "untracked_count": metadata.get("untracked_count"),
        "inlined_untracked_count": metadata.get("inlined_untracked_count"),
        "omitted_untracked_count": metadata.get("omitted_untracked_count"),
        "session_context": str(scope_of_work_path) if session_context is not None else None,
        "session_context_provided": metadata.get("session_context_provided"),
        "session_context_bytes": metadata.get("session_context_bytes"),
        "session_manifest": str(session_manifest_path) if packet_session_manifest is not None else None,
        "session_manifest_provided": metadata.get("session_manifest_provided"),
        "session_owned_path_count": metadata.get("session_owned_path_count"),
        "unattributed_dirty_paths": metadata.get("unattributed_dirty_paths"),
        "primary_files": primary_files,
        "background_files": background_files,
        "excluded_path_prefixes": metadata.get("excluded_path_prefixes"),
    }
    ledger.event(
        phase="prepare",
        event="completed",
        status="completed",
        reason_code="prepare_completed",
        repo=str(root),
        cwd=str(root),
        paths={
            "review_packet": str(packet_path),
            "metadata": str(metadata_path),
            "snapshot": str(snapshot_path),
            "scope_of_work": str(scope_of_work_path) if session_context is not None else None,
            "session_manifest": str(session_manifest_path) if packet_session_manifest is not None else None,
            "review_env": str(review_env_path),
        },
        packet_bytes=metadata.get("packet_bytes"),
    )
    ledger.summary(
        status="completed",
        reason_code="prepare_completed",
        message="review-validate-fix run prepared",
        **result,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an auditable review-validate-fix run directory.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--session-context", help="Required file containing the main-agent work summary.")
    parser.add_argument("--session-manifest", help="Optional prebuilt session ownership manifest JSON.")
    parser.add_argument("--transcript", help="Optional Codex JSONL transcript used to build session-manifest.json.")
    parser.add_argument("--base-dir", default=str(default_base_dir()), help="Directory where a unique run directory will be created.")
    parser.add_argument("--output-json", help="Write run metadata JSON to this path. Prints JSON to stdout when omitted.")
    parser.add_argument("--rvf-run-id", help="Use an existing RVF run id instead of creating a new one.")
    parser.add_argument("--rvf-run-dir", help="Use this RVF run directory instead of resolving state/runs/<run_id>.")
    parser.add_argument("--max-file-bytes", type=int, default=200_000, help="Max untracked file bytes to inline.")
    parser.add_argument("--max-packet-bytes", type=int, default=0, help="Fail if the generated packet exceeds this many bytes. 0 disables the check.")
    parser.add_argument("--primary-file", action="append", default=[], help="Path known to be primary work for this turn. May be repeated.")
    parser.add_argument("--background-file", action="append", default=[], help="Path known to be pre-existing background WIP. May be repeated.")
    parser.add_argument("--exclude-path-prefix", action="append", default=[], help="Path prefix to omit from status, diff, and untracked packet sections. May be repeated.")
    parser.add_argument(
        "--allow-missing-session-context",
        action="store_true",
        help="Debug-only escape hatch. Normal review runs must pass --session-context.",
    )
    args = parser.parse_args()

    try:
        session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
        if session_context is None and not args.allow_missing_session_context:
            raise ValueError(
                "session context is required: write a main-agent scope-of-work summary and pass "
                "--session-context <file>; use --allow-missing-session-context only for debug"
            )
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        if (
            session_context is not None
            and not session_context.read_text(encoding="utf-8").strip()
            and not args.allow_missing_session_context
        ):
            raise ValueError(f"session context file is empty: {session_context}")
        session_manifest = Path(args.session_manifest).expanduser().resolve() if args.session_manifest else None
        transcript = Path(args.transcript).expanduser().resolve() if args.transcript else None
        if session_manifest is not None and transcript is not None:
            raise ValueError("pass either --session-manifest or --transcript, not both")
        if session_manifest is not None and not session_manifest.exists():
            raise ValueError(f"session manifest file not found: {session_manifest}")
        if transcript is not None and not transcript.exists():
            raise ValueError(f"transcript file not found: {transcript}")
        result = prepare_run(
            repo=Path(args.repo).expanduser().resolve(),
            session_context=session_context,
            session_manifest=session_manifest,
            transcript=transcript,
            base_dir=Path(args.base_dir).expanduser().resolve(),
            max_file_bytes=args.max_file_bytes,
            max_packet_bytes=args.max_packet_bytes,
            primary_files=args.primary_file,
            background_files=args.background_file,
            exclude_path_prefixes=args.exclude_path_prefix,
            allow_missing_session_context=args.allow_missing_session_context,
            rvf_run_id=args.rvf_run_id,
            rvf_run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
        )
    except Exception as exc:
        return fail(str(exc), 2)

    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output_json:
        Path(args.output_json).expanduser().resolve().write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
