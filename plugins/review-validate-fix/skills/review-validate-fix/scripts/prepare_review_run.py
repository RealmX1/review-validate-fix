#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
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
from rvf_logging import normalize_rvf_backend, rvf_state_fields, start_run


SKILL_DIR = Path(__file__).resolve().parents[1]
BUILD_PACKET = SKILL_DIR / "scripts" / "build_review_packet.py"
WORKSPACE_SNAPSHOT = SKILL_DIR / "scripts" / "workspace_snapshot.py"
SESSION_MANIFEST = SKILL_DIR / "scripts" / "session_manifest.py"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
WRITE_REVIEW_RESULT = SKILL_DIR / "scripts" / "write_review_result.py"
CHECK_REVIEW_RESULT = SKILL_DIR / "scripts" / "check_review_result.py"
SCOPE_CONTRACT_VERSION = 1


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
    log_root: Path,
    artifacts_dir: Path,
    inputs_dir: Path,
    scope_contract_path: Path,
    scope_of_work_path: Path | None,
    session_manifest_path: Path | None,
    packet_path: Path,
    metadata_path: Path,
    snapshot_path: Path,
    bootstrap_metadata_path: Path | None,
    rvf_backend: str | None = None,
) -> tuple[dict[str, str], str]:
    env: dict[str, str] = {
        "RVF_REPO": str(repo),
        "RVF_RUN_ID": run_id,
        "RVF_RUN_DIR": str(run_dir),
        "RVF_ARTIFACTS_DIR": str(artifacts_dir),
        "RVF_INPUTS_DIR": str(inputs_dir),
        "RVF_SCOPE_CONTRACT": str(scope_contract_path),
        "RVF_REVIEW_PACKET": str(packet_path),
        "RVF_REVIEW_PACKET_METADATA": str(metadata_path),
        "RVF_BEFORE_WORKSPACE_SNAPSHOT": str(snapshot_path),
        "RVF_COMMAND_LOCK": str(COMMAND_LOCK),
        "RVF_WRITE_REVIEW_RESULT": str(WRITE_REVIEW_RESULT),
        "RVF_CHECK_REVIEW_RESULT": str(CHECK_REVIEW_RESULT),
        "RVF_REVIEW_RESULT": str(artifacts_dir / "reviewers" / "reviewer" / "review-result.json"),
        "CODEX_RVF_LOG_ROOT": str(log_root),
        "CODEX_RVF_RUN_ID": run_id,
        "CODEX_RVF_RUN_DIR": str(run_dir),
    }
    canonical_backend = normalize_rvf_backend(rvf_backend)
    if canonical_backend is not None:
        env["RVF_BACKEND"] = canonical_backend
    if bootstrap_metadata_path is not None:
        env["RVF_WORKTREE_BOOTSTRAP"] = str(bootstrap_metadata_path)
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
        f"export CODEX_RVF_LOG_ROOT={shlex.quote(env['CODEX_RVF_LOG_ROOT'])}",
        'export CODEX_RVF_RUN_ID="$RVF_RUN_ID"',
        'export CODEX_RVF_RUN_DIR="$RVF_RUN_DIR"',
    ]
    if artifacts_dir == run_dir / "artifacts":
        lines.append('export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"')
    else:
        lines.append(f"export RVF_ARTIFACTS_DIR={shlex.quote(env['RVF_ARTIFACTS_DIR'])}")
    if inputs_dir == artifacts_dir / "inputs":
        lines.append('export RVF_INPUTS_DIR="$RVF_ARTIFACTS_DIR/inputs"')
    else:
        lines.append(f"export RVF_INPUTS_DIR={shlex.quote(env['RVF_INPUTS_DIR'])}")
    if scope_contract_path == inputs_dir / "scope.contract.json":
        lines.append('export RVF_SCOPE_CONTRACT="$RVF_INPUTS_DIR/scope.contract.json"')
    else:
        lines.append(f"export RVF_SCOPE_CONTRACT={shlex.quote(env['RVF_SCOPE_CONTRACT'])}")
    if scope_of_work_path is not None:
        lines.append('export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"')
        lines.append('export RVF_SESSION_CONTEXT="$RVF_SCOPE_OF_WORK"')
    if session_manifest_path is not None:
        lines.append('export RVF_SESSION_MANIFEST="$RVF_ARTIFACTS_DIR/session-manifest.json"')
    if canonical_backend is not None:
        lines.append(f"export RVF_BACKEND={shlex.quote(canonical_backend)}")
    lines.extend(
        [
            'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"',
            'export RVF_REVIEW_PACKET_METADATA="$RVF_ARTIFACTS_DIR/review-packet.metadata.json"',
            'export RVF_BEFORE_WORKSPACE_SNAPSHOT="$RVF_ARTIFACTS_DIR/before-workspace-snapshot.json"',
            'export RVF_WORKTREE_BOOTSTRAP="$RVF_ARTIFACTS_DIR/worktree-bootstrap.json"',
            f"export RVF_COMMAND_LOCK={shlex.quote(env['RVF_COMMAND_LOCK'])}",
            f"export RVF_WRITE_REVIEW_RESULT={shlex.quote(env['RVF_WRITE_REVIEW_RESULT'])}",
            f"export RVF_CHECK_REVIEW_RESULT={shlex.quote(env['RVF_CHECK_REVIEW_RESULT'])}",
            'export RVF_REVIEW_RESULT="$RVF_ARTIFACTS_DIR/reviewers/${RVF_REVIEWER_ID:-reviewer}/review-result.json"',
            "",
        ]
    )
    return env, "\n".join(lines)


def review_agent_context_text(
    *,
    repo: Path,
    review_env_path: Path,
    scope_of_work_path: Path | None,
    session_manifest_path: Path | None,
) -> str:
    lines = [
        "## RVF Generated Reviewer Context",
        "",
        "This block was generated by `prepare_review_run.py`; do not rewrite artifact paths by hand.",
        f"- Target repo: `{repo}`",
        f"- Review env file: `{review_env_path}`",
        "",
        "For shell commands, load the review session variables first:",
        "",
        "```sh",
        f". {shlex.quote(str(review_env_path))}",
        "```",
        "",
        "Entry files:",
        "- scope contract: `$RVF_SCOPE_CONTRACT`",
    ]
    if scope_of_work_path is not None:
        lines.append("- scope-of-work: `$RVF_SCOPE_OF_WORK`")
    if session_manifest_path is not None:
        lines.append("- session manifest: `$RVF_SESSION_MANIFEST`")
    lines.extend(
        [
            "- review packet: `$RVF_REVIEW_PACKET`",
            "- command lock wrapper: `$RVF_COMMAND_LOCK`",
            "- review result writer: `$RVF_WRITE_REVIEW_RESULT`",
            "- review result checker: `$RVF_CHECK_REVIEW_RESULT`",
            "- reviewer result artifact: `$RVF_REVIEW_RESULT` (set `RVF_REVIEWER_ID` before sourcing when launching multiple reviewers)",
            "",
            "Use the variables above in commands and notes instead of expanding the run artifacts directory. The result artifact is protocol output and is the only file a review-only agent may write intentionally.",
        ]
    )
    return "\n".join(lines) + "\n"


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


def is_tracked_path(repo: Path, rel_path: str) -> bool:
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel_path],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def safe_stored_name(rel_path: str) -> str:
    normalized = rel_path.strip().replace("\\", "/").strip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    leaf = Path(normalized).name or "path"
    return f"{digest}-{leaf}"


def normalized_scope_list(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        path = value.strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path:
            normalized.append(path)
    return sorted(set(normalized))


def dirty_paths(repo: Path, exclude_prefixes: list[str]) -> list[str]:
    args = ["status", "--porcelain", "-uall"]
    if exclude_prefixes:
        args.extend(["--", ".", *[f":(exclude){prefix}" for prefix in exclude_prefixes]])
    status = run_git(repo, args)
    paths: list[str] = []
    for raw_line in str(status).splitlines():
        if len(raw_line) < 4:
            continue
        path = raw_line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().replace("\\", "/")
        if path:
            paths.append(path)
    return normalized_scope_list(paths)


def metadata_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_scope_contract(
    *,
    path: Path,
    run_id: str,
    repo: Path,
    created_at: str,
    scope_mode: str,
    primary_files: list[str],
    background_files: list[str],
    protected_files: list[str],
    canonical_issues: list[dict[str, Any]],
    fix_allowlist: list[str],
    excluded_path_prefixes: list[str],
    start_snapshot_path: Path,
    review_packet_path: Path,
    session_manifest_path: Path | None,
    scope_of_work_path: Path | None,
    review_packet_metadata_path: Path,
) -> dict[str, Any]:
    canonical_scope = {
        "version": SCOPE_CONTRACT_VERSION,
        "repo": str(repo),
        "scope_mode": scope_mode,
        "primary_files": primary_files,
        "background_files": background_files,
        "protected_files": protected_files,
        "canonical_issues": canonical_issues,
        "fix_allowlist": fix_allowlist,
        "excluded_path_prefixes": excluded_path_prefixes,
    }
    scope_hash = hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest()
    contract: dict[str, Any] = {
        "version": SCOPE_CONTRACT_VERSION,
        "run_id": run_id,
        "repo": str(repo),
        "created_at": created_at,
        "scope_mode": scope_mode,
        "scope_hash": scope_hash,
        "primary_files": primary_files,
        "background_files": background_files,
        "protected_files": protected_files,
        "canonical_issues": canonical_issues,
        "fix_allowlist": fix_allowlist,
        "start_snapshot_path": str(start_snapshot_path),
        "review_packet_path": str(review_packet_path),
        "session_manifest_path": str(session_manifest_path) if session_manifest_path is not None else None,
        "scope_of_work_path": str(scope_of_work_path) if scope_of_work_path is not None else None,
        "review_packet_metadata_path": str(review_packet_metadata_path),
        "canonical_scope": canonical_scope,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract


def copy_bootstrap_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def build_worktree_bootstrap(
    *,
    repo: Path,
    artifact_dir: Path,
    packet_metadata: dict[str, Any],
) -> dict[str, Any]:
    owned_dirty = [
        path
        for path in packet_metadata.get("session_owned_dirty_paths", [])
        if isinstance(path, str) and path.strip()
    ]
    patch_path = artifact_dir / "worktree-bootstrap.patch"
    files_dir = artifact_dir / "worktree-bootstrap-files"
    bootstrap_path = artifact_dir / "worktree-bootstrap.json"
    files_dir.mkdir(parents=True, exist_ok=True)

    tracked_paths = [path for path in owned_dirty if is_tracked_path(repo, path)]
    patch_text = ""
    if tracked_paths:
        diff_output = run_git(repo, ["diff", "--binary", "--find-renames", "HEAD", "--", *tracked_paths])
        patch_text = diff_output if isinstance(diff_output, str) else diff_output.decode("utf-8", "replace")
    patch_path.write_text(patch_text, encoding="utf-8")

    untracked_files: list[dict[str, str]] = []
    for rel_path in owned_dirty:
        if rel_path in tracked_paths:
            continue
        source = repo / rel_path
        if not source.exists() and not source.is_symlink():
            continue
        stored_name = safe_stored_name(rel_path)
        stored_path = files_dir / stored_name
        copy_bootstrap_path(source, stored_path)
        untracked_files.append({"path": rel_path, "stored_path": str(stored_path)})

    head = str(run_git(repo, ["rev-parse", "HEAD"])).strip()
    payload = {
        "repo": str(repo),
        "base_ref": head,
        "patch_file": str(patch_path),
        "files_dir": str(files_dir),
        "owned_dirty_paths": owned_dirty,
        "tracked_paths": tracked_paths,
        "untracked_files": untracked_files,
        "apply_helper": str(SKILL_DIR / "scripts" / "apply_worktree_bootstrap.py"),
    }
    bootstrap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "metadata": payload,
        "metadata_path": bootstrap_path,
        "patch_path": patch_path,
        "files_dir": files_dir,
    }


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
    rvf_backend: str | None = None,
) -> dict[str, Any]:
    root = git_root(repo)
    canonical_backend = normalize_rvf_backend(rvf_backend) or "manual"
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

    inputs_dir = artifact_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    packet_path = artifact_dir / "review-packet.md"
    metadata_path = artifact_dir / "review-packet.metadata.json"
    snapshot_path = artifact_dir / "before-workspace-snapshot.json"
    scope_of_work_path = artifact_dir / "scope-of-work.md"
    session_manifest_path = artifact_dir / "session-manifest.json"
    input_packet_path = inputs_dir / "review-packet.md"
    input_metadata_path = inputs_dir / "review-packet.metadata.json"
    input_snapshot_path = inputs_dir / "before-workspace-snapshot.json"
    input_scope_of_work_path = inputs_dir / "scope-of-work.md"
    input_session_manifest_path = inputs_dir / "session-manifest.json"
    scope_contract_path = inputs_dir / "scope.contract.json"
    review_env_path = artifact_dir / "review-env.sh"
    review_agent_context_path = artifact_dir / "review-agent-context.md"

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
    shutil.copyfile(packet_path, input_packet_path)
    shutil.copyfile(metadata_path, input_metadata_path)
    shutil.copyfile(snapshot_path, input_snapshot_path)
    if scope_of_work_path.exists():
        shutil.copyfile(scope_of_work_path, input_scope_of_work_path)
    if session_manifest_path.exists():
        shutil.copyfile(session_manifest_path, input_session_manifest_path)
    resolved_input_packet_path = input_packet_path.resolve()
    resolved_input_metadata_path = input_metadata_path.resolve()
    resolved_input_snapshot_path = input_snapshot_path.resolve()
    resolved_input_scope_of_work_path = input_scope_of_work_path.resolve() if input_scope_of_work_path.exists() else None
    resolved_input_session_manifest_path = (
        input_session_manifest_path.resolve() if input_session_manifest_path.exists() else None
    )
    if metadata.get("session_manifest_provided"):
        scope_mode = "session-owned"
    elif primary_files or background_files or exclude_path_prefixes:
        scope_mode = "custom"
    else:
        scope_mode = "manual-all-uncommitted"
    manual_dirty_paths = dirty_paths(root, metadata_list(metadata.get("excluded_path_prefixes"))) if scope_mode == "manual-all-uncommitted" else []
    primary_scope_files = normalized_scope_list(
        primary_files + metadata_list(metadata.get("session_owned_paths")) + manual_dirty_paths
    )
    background_scope_files = normalized_scope_list(
        background_files + metadata_list(metadata.get("unattributed_dirty_paths"))
    )
    protected_files = background_scope_files
    created_at = datetime.now(timezone.utc).isoformat()
    scope_contract = write_scope_contract(
        path=scope_contract_path,
        run_id=ledger.run_id,
        repo=root,
        created_at=created_at,
        scope_mode=scope_mode,
        primary_files=primary_scope_files,
        background_files=background_scope_files,
        protected_files=protected_files,
        canonical_issues=[],
        fix_allowlist=primary_scope_files,
        excluded_path_prefixes=normalized_scope_list(metadata_list(metadata.get("excluded_path_prefixes"))),
        start_snapshot_path=resolved_input_snapshot_path,
        review_packet_path=resolved_input_packet_path,
        session_manifest_path=resolved_input_session_manifest_path,
        scope_of_work_path=resolved_input_scope_of_work_path,
        review_packet_metadata_path=resolved_input_metadata_path,
    )
    bootstrap = build_worktree_bootstrap(
        repo=root,
        artifact_dir=artifact_dir,
        packet_metadata=metadata,
    )
    scope_path = scope_of_work_path.resolve() if session_context is not None else None
    manifest_path = session_manifest_path.resolve() if packet_session_manifest is not None else None
    review_env, review_env_text = review_env_exports(
        repo=root,
        run_id=ledger.run_id,
        run_dir=ledger.run_dir,
        log_root=ledger.root,
        artifacts_dir=artifact_dir,
        inputs_dir=inputs_dir,
        scope_contract_path=scope_contract_path,
        scope_of_work_path=scope_path,
        session_manifest_path=manifest_path,
        packet_path=packet_path,
        metadata_path=metadata_path,
        snapshot_path=snapshot_path,
        bootstrap_metadata_path=bootstrap["metadata_path"],
        rvf_backend=canonical_backend,
    )
    review_env_path.write_text(review_env_text, encoding="utf-8")
    review_agent_context = review_agent_context_text(
        repo=root,
        review_env_path=review_env_path,
        scope_of_work_path=scope_path,
        session_manifest_path=manifest_path,
    )
    review_agent_context_path.write_text(review_agent_context, encoding="utf-8")
    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "events_path": str(ledger.events_path),
        "summary_path": str(ledger.summary_path),
        "artifacts_dir": str(artifact_dir),
        "inputs_dir": str(inputs_dir),
        "scope_contract": str(scope_contract_path),
        "scope_contract_payload": scope_contract,
        "review_packet": str(packet_path),
        "review_packet_metadata": str(metadata_path),
        "before_workspace_snapshot": str(snapshot_path),
        "input_review_packet": str(resolved_input_packet_path),
        "input_review_packet_metadata": str(resolved_input_metadata_path),
        "input_before_workspace_snapshot": str(resolved_input_snapshot_path),
        "input_scope_of_work_file": str(resolved_input_scope_of_work_path)
        if resolved_input_scope_of_work_path is not None
        else None,
        "input_session_manifest_file": str(resolved_input_session_manifest_path)
        if resolved_input_session_manifest_path is not None
        else None,
        "worktree_bootstrap": str(bootstrap["metadata_path"]),
        "worktree_bootstrap_patch": str(bootstrap["patch_path"]),
        "worktree_bootstrap_files_dir": str(bootstrap["files_dir"]),
        "worktree_bootstrap_metadata": bootstrap["metadata"],
        "scope_of_work_file": str(scope_path) if scope_path is not None else None,
        "session_manifest_file": str(manifest_path) if manifest_path is not None else None,
        "review_env_file": str(review_env_path),
        "review_env": review_env,
        "review_agent_context_file": str(review_agent_context_path),
        "review_agent_context": review_agent_context,
        "source_session_context": str(session_context) if session_context is not None else None,
        "source_session_manifest": source_session_manifest,
        "packet_bytes": metadata.get("packet_bytes"),
        "untracked_count": metadata.get("untracked_count"),
        "inlined_untracked_count": metadata.get("inlined_untracked_count"),
        "omitted_untracked_count": metadata.get("omitted_untracked_count"),
        "session_context": str(scope_path) if scope_path is not None else None,
        "session_context_provided": metadata.get("session_context_provided"),
        "session_context_bytes": metadata.get("session_context_bytes"),
        "session_manifest": str(manifest_path) if manifest_path is not None else None,
        "session_manifest_provided": metadata.get("session_manifest_provided"),
        "session_owned_path_count": metadata.get("session_owned_path_count"),
        "unattributed_dirty_paths": metadata.get("unattributed_dirty_paths"),
        "primary_files": primary_files,
        "background_files": background_files,
        "excluded_path_prefixes": metadata.get("excluded_path_prefixes"),
        **rvf_state_fields(
            phase="prepare",
            backend=canonical_backend,
            backend_raw=rvf_backend or canonical_backend,
            scope_contract_path=scope_contract_path,
            scope_of_work_path=scope_path,
            review_packet_path=packet_path,
            session_manifest_path=manifest_path,
        ),
    }
    ledger.event(
        phase="prepare",
        event="completed",
        status="completed",
        reason_code="prepare_completed",
        repo=str(root),
        cwd=str(root),
        paths={
            "inputs": str(inputs_dir),
            "scope_contract": str(scope_contract_path),
            "review_packet": str(packet_path),
            "metadata": str(metadata_path),
            "snapshot": str(snapshot_path),
            "worktree_bootstrap": str(bootstrap["metadata_path"]),
            "scope_of_work": str(scope_of_work_path) if session_context is not None else None,
            "session_manifest": str(session_manifest_path) if packet_session_manifest is not None else None,
            "review_env": str(review_env_path),
            "review_agent_context": str(review_agent_context_path),
        },
        packet_bytes=metadata.get("packet_bytes"),
        **rvf_state_fields(
            phase="prepare",
            backend=canonical_backend,
            backend_raw=rvf_backend or canonical_backend,
            scope_contract_path=scope_contract_path,
            scope_of_work_path=scope_path,
            review_packet_path=packet_path,
            session_manifest_path=manifest_path,
        ),
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
    parser.add_argument("--rvf-backend", help="RVF entry backend: manual, kanban-followup, or kanban-task.")
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
            rvf_backend=args.rvf_backend,
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
