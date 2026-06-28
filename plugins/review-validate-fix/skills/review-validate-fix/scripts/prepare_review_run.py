#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import
import concurrent.futures
import diff_tracker
import rvf_prep_file
from rvf_dispatch_prompts import dispatch_scope_of_work_text
from rvf_logging import normalize_rvf_backend, rvf_state_fields, start_run
from core.host_adapter.host_transcript_format_detection import HOST_CODEX, detect_transcript_format


# 合法的主 dispatch harness（与 dispatch_reviewers / reviewer-registry 对齐）。
# cursor 仅经显式 RVF_MAIN_HARNESS=cursor 覆盖可达，transcript 探测永不返回 cursor。
VALID_MAIN_HARNESSES = {"cursor", "claude_code", "codex"}

SHARED_WORKFLOW_DEFAULT_TIMEOUT_SECONDS = 60.0
TARGET_FLOW_BACKEND = {
    "flow-2-branch": "kanban-task",
    "flow-2-inplace": "kanban-task",
    "flow-1-self-rising": "kanban-followup",
    "flow-3-inplace": "manual",
    "flow-manual": "manual",
}


SKILL_DIR = Path(__file__).resolve().parents[1]
BUILD_PACKET = SKILL_DIR / "scripts" / "build_review_packet.py"
WORKSPACE_SNAPSHOT = SKILL_DIR / "scripts" / "workspace_snapshot.py"
SESSION_MANIFEST = SKILL_DIR / "scripts" / "session_manifest.py"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
WRITE_REVIEW_RESULT = SKILL_DIR / "scripts" / "write_review_result.py"
CHECK_REVIEW_RESULT = SKILL_DIR / "scripts" / "check_review_result.py"
SCOPE_CONTRACT_VERSION = 2


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
    tracker_dir: Path | None = None,
    tracker_repo_key: str | None = None,
    tracker_scope_path: Path | None = None,
    rvf_backend: str | None = None,
    parent_context_path: Path | None = None,
    main_harness: str | None = None,
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
    }
    if main_harness:
        env["RVF_MAIN_HARNESS"] = main_harness
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
    if tracker_dir is not None:
        env["RVF_TRACKER_DIR"] = str(tracker_dir)
    if tracker_repo_key:
        env["RVF_TRACKER_REPO_KEY"] = tracker_repo_key
    if tracker_scope_path is not None:
        env["RVF_TRACKER_SCOPE"] = str(tracker_scope_path)
    # 父会话对话 context（fail-open，可能缺失）。仅在 artifact 存在时导出，
    # 与 cline-kanban task prompt 的 RVF_PARENT_CONVERSATION_CONTEXT 标记一致。
    if parent_context_path is not None and parent_context_path.exists():
        env["RVF_PARENT_CONVERSATION_CONTEXT"] = str(parent_context_path)

    lines = [
        "# Source this file in review subprocesses to avoid repeating long RVF paths.",
        f"export RVF_REPO={shlex.quote(env['RVF_REPO'])}",
        f"export RVF_RUN_ID={shlex.quote(env['RVF_RUN_ID'])}",
        f"export RVF_RUN_DIR={shlex.quote(env['RVF_RUN_DIR'])}",
        f"export CODEX_RVF_LOG_ROOT={shlex.quote(env['CODEX_RVF_LOG_ROOT'])}",
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
    if main_harness:
        lines.append(f"export RVF_MAIN_HARNESS={shlex.quote(main_harness)}")
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
        ]
    )
    if tracker_dir is not None:
        lines.append(f"export RVF_TRACKER_DIR={shlex.quote(str(tracker_dir))}")
    if tracker_repo_key:
        lines.append(f"export RVF_TRACKER_REPO_KEY={shlex.quote(tracker_repo_key)}")
    if tracker_scope_path is not None:
        if tracker_scope_path == inputs_dir / "tracker-scope.json":
            lines.append('export RVF_TRACKER_SCOPE="$RVF_INPUTS_DIR/tracker-scope.json"')
        else:
            lines.append(f"export RVF_TRACKER_SCOPE={shlex.quote(str(tracker_scope_path))}")
    if "RVF_PARENT_CONVERSATION_CONTEXT" in env:
        if parent_context_path == artifacts_dir / "parent-conversation-context.md":
            lines.append('export RVF_PARENT_CONVERSATION_CONTEXT="$RVF_ARTIFACTS_DIR/parent-conversation-context.md"')
        else:
            lines.append(
                f"export RVF_PARENT_CONVERSATION_CONTEXT={shlex.quote(env['RVF_PARENT_CONVERSATION_CONTEXT'])}"
            )
    lines.append("")
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
            "Scope precedence: read `$RVF_SCOPE_CONTRACT` first. If `primary_units` is non-empty, review that tracker unit scope; otherwise use `primary_files` plus scope-of-work. `$RVF_SESSION_MANIFEST` is ownership evidence and tracker audit context, not the final scope contract.",
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


_HEX64_CHARS = set("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(ch in _HEX64_CHARS for ch in value.lower())


def load_tracker_scope(path: Path) -> dict[str, Any]:
    """Validate an allocator tracker_scope JSON file.

    Required keys: unit_ids (non-empty list of 64-hex sha256 strings), lease_id
    (non-empty string), scope_hash (64-hex or "sha256:<64-hex>"), paths (list of
    strings, may be empty).

    Optional keys: lease_ttl_seconds (int), hunks (list of dicts),
    source_session_id (str), takeover_from_session_id (str).

    Unknown keys are tolerated and passed through unchanged so future allocator
    versions can extend the payload without re-touching this consumer.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"tracker_scope: failed to read {path}: {exc}")
    if not isinstance(raw, dict):
        raise ValueError("tracker_scope: payload must be a JSON object")

    unit_ids_raw = raw.get("unit_ids")
    if not isinstance(unit_ids_raw, list) or not unit_ids_raw:
        raise ValueError("tracker_scope.unit_ids must be a non-empty list")
    unit_ids: list[str] = []
    for index, item in enumerate(unit_ids_raw):
        if not isinstance(item, str) or not _is_hex64(item):
            raise ValueError(
                f"tracker_scope.unit_ids[{index}] must be a 64-character hex string"
            )
        unit_ids.append(item.lower())

    lease_id = raw.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ValueError("tracker_scope.lease_id must be a non-empty string")

    scope_hash_raw = raw.get("scope_hash")
    if not isinstance(scope_hash_raw, str) or not scope_hash_raw.strip():
        raise ValueError("tracker_scope.scope_hash must be a non-empty string")
    bare = scope_hash_raw[len("sha256:") :] if scope_hash_raw.startswith("sha256:") else scope_hash_raw
    if not _is_hex64(bare):
        raise ValueError(
            "tracker_scope.scope_hash must be 64 hex characters or 'sha256:<64-hex>'"
        )
    scope_hash = f"sha256:{bare.lower()}"

    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, list):
        raise ValueError("tracker_scope.paths must be a list of strings")
    for index, item in enumerate(paths_raw):
        if not isinstance(item, str):
            raise ValueError(f"tracker_scope.paths[{index}] must be a string")
    paths = normalized_scope_list(paths_raw)

    if "hunks" in raw and not isinstance(raw.get("hunks"), list):
        raise ValueError("tracker_scope.hunks must be a list when present")
    if "source_session_id" in raw and raw["source_session_id"] is not None and not isinstance(raw["source_session_id"], str):
        raise ValueError("tracker_scope.source_session_id must be a string or null")
    if "takeover_from_session_id" in raw and raw["takeover_from_session_id"] is not None and not isinstance(raw["takeover_from_session_id"], str):
        raise ValueError("tracker_scope.takeover_from_session_id must be a string or null")

    payload: dict[str, Any] = dict(raw)
    payload["unit_ids"] = unit_ids
    payload["lease_id"] = lease_id
    payload["scope_hash"] = scope_hash
    payload["paths"] = paths
    return payload


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
    primary_units: list[str] | None = None,
    tracker_lease_id: str | None = None,
    tracker_scope_hash: str | None = None,
    tracker_transcript_max_line_number: int | None = None,
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
        "primary_units": primary_units,
        "tracker_lease_id": tracker_lease_id,
        "tracker_scope_hash": tracker_scope_hash,
        "tracker_transcript_max_line_number": tracker_transcript_max_line_number,
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
        shutil.copy2(source, target, follow_symlinks=False)


def _ignore_prefix_matches(path: str, exclude_prefixes: list[str]) -> bool:
    normalized = path.strip().replace("\\", "/").lstrip("/")
    for raw in exclude_prefixes:
        if not isinstance(raw, str) or not raw.strip():
            continue
        prefix = raw.strip().replace("\\", "/").lstrip("/")
        if prefix.endswith("/"):
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                return True
        else:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
    return False


def _path_bootstrap_bytes(repo: Path, rel_path: str) -> int:
    target = repo / rel_path
    try:
        if target.is_symlink():
            return 0
        if target.is_file():
            return target.stat().st_size
        if target.is_dir():
            total = 0
            for child in target.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        continue
            return total
    except OSError:
        return 0
    return 0


def build_worktree_bootstrap(
    *,
    repo: Path,
    artifact_dir: Path,
    packet_metadata: dict[str, Any],
) -> dict[str, Any]:
    if packet_metadata.get("tracker_scope_present"):
        session_owned_dirty = normalized_scope_list(metadata_list(packet_metadata.get("tracker_scope_paths")))
    else:
        session_owned_dirty = normalized_scope_list(
            metadata_list(packet_metadata.get("session_owned_dirty_paths"))
        )
    unattributed_dirty = normalized_scope_list(
        metadata_list(packet_metadata.get("unattributed_dirty_paths"))
    )
    exclude_prefixes = [
        prefix
        for prefix in metadata_list(packet_metadata.get("excluded_path_prefixes"))
        if isinstance(prefix, str) and prefix.strip()
    ]

    filtered_session_owned: list[str] = []
    filtered_unattributed: list[str] = []
    ignored_paths: list[str] = []
    for path in session_owned_dirty:
        if _ignore_prefix_matches(path, exclude_prefixes):
            ignored_paths.append(path)
        else:
            filtered_session_owned.append(path)
    for path in unattributed_dirty:
        if _ignore_prefix_matches(path, exclude_prefixes):
            ignored_paths.append(path)
            continue
        if path in filtered_session_owned:
            continue
        filtered_unattributed.append(path)

    owned_dirty = normalized_scope_list(filtered_session_owned + filtered_unattributed)
    bootstrap_kind = "full-dirty" if filtered_unattributed else "session-owned-only"

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

    unattributed_bytes = sum(_path_bootstrap_bytes(repo, path) for path in filtered_unattributed)
    total_bootstrap_bytes = sum(_path_bootstrap_bytes(repo, path) for path in owned_dirty)

    head = str(run_git(repo, ["rev-parse", "HEAD"])).strip()
    payload = {
        "repo": str(repo),
        "base_ref": head,
        "patch_file": str(patch_path),
        "files_dir": str(files_dir),
        "owned_dirty_paths": owned_dirty,
        "session_owned_dirty_paths": filtered_session_owned,
        "unattributed_dirty_paths": filtered_unattributed,
        "ignored_dirty_paths": sorted(set(ignored_paths)),
        "tracked_paths": tracked_paths,
        "untracked_files": untracked_files,
        "bootstrap_kind": bootstrap_kind,
        "unattributed_path_count": len(filtered_unattributed),
        "unattributed_bytes": unattributed_bytes,
        "total_bootstrap_bytes": total_bootstrap_bytes,
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
    tracker_scope: Path | None = None,
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
                "--tracker-run-id",
                ledger.run_id,
            ]
        )
        packet_session_manifest = session_manifest_path
        source_session_manifest = f"transcript:{transcript}"
    elif session_manifest is not None:
        shutil.copyfile(session_manifest, session_manifest_path)
        packet_session_manifest = session_manifest_path
        source_session_manifest = str(session_manifest)

    tracker_scope_payload: dict[str, Any] | None = None
    artifact_tracker_scope_path: Path | None = None
    if tracker_scope is not None:
        if packet_session_manifest is None:
            raise ValueError(
                "--tracker-scope requires --session-manifest or --transcript so the "
                "tracker_scope payload can be spliced into manifest.tracker.tracker_scope"
            )
        tracker_scope_payload = load_tracker_scope(tracker_scope)
        try:
            manifest_dict = json.loads(session_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"failed to read session manifest for tracker_scope splice: {exc}")
        if not isinstance(manifest_dict, dict):
            raise ValueError("session manifest is not a JSON object; cannot splice tracker_scope")
        manifest_tracker = manifest_dict.get("tracker")
        if not isinstance(manifest_tracker, dict):
            manifest_tracker = {}
            manifest_dict["tracker"] = manifest_tracker
        manifest_tracker["tracker_scope"] = tracker_scope_payload
        session_manifest_path.write_text(
            json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifact_tracker_scope_path = artifact_dir / "tracker-scope.json"
        if tracker_scope.resolve() != artifact_tracker_scope_path.resolve():
            shutil.copyfile(tracker_scope, artifact_tracker_scope_path)

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
    resolved_input_tracker_scope_path: Path | None = None
    resolved_artifact_tracker_scope_path: Path | None = None
    if artifact_tracker_scope_path is not None and artifact_tracker_scope_path.exists():
        input_tracker_scope_path = inputs_dir / "tracker-scope.json"
        shutil.copyfile(artifact_tracker_scope_path, input_tracker_scope_path)
        resolved_input_tracker_scope_path = input_tracker_scope_path.resolve()
        resolved_artifact_tracker_scope_path = artifact_tracker_scope_path.resolve()
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
    if tracker_scope_payload is not None:
        primary_scope_files = normalized_scope_list(tracker_scope_payload["paths"])
        non_allocated_dirty = [
            path
            for path in normalized_scope_list(
                metadata_list(metadata.get("session_owned_dirty_paths"))
                + metadata_list(metadata.get("unattributed_dirty_paths"))
            )
            if path not in set(primary_scope_files)
        ]
        background_scope_files = normalized_scope_list(background_files + non_allocated_dirty)
        contract_primary_units: list[str] | None = sorted(set(tracker_scope_payload["unit_ids"]))
        contract_tracker_lease_id: str | None = tracker_scope_payload["lease_id"]
        contract_tracker_lease_ttl_seconds: int | None = (
            tracker_scope_payload.get("lease_ttl_seconds")
            if isinstance(tracker_scope_payload.get("lease_ttl_seconds"), int)
            else None
        )
        contract_tracker_scope_hash: str | None = tracker_scope_payload["scope_hash"]
        contract_tracker_transcript_max_line_number: int | None = (
            tracker_scope_payload.get("transcript_max_line_number")
            if isinstance(tracker_scope_payload.get("transcript_max_line_number"), int)
            else None
        )
    else:
        primary_scope_files = normalized_scope_list(
            primary_files + metadata_list(metadata.get("session_owned_paths")) + manual_dirty_paths
        )
        background_scope_files = normalized_scope_list(
            background_files + metadata_list(metadata.get("unattributed_dirty_paths"))
        )
        contract_primary_units = None
        contract_tracker_lease_id = None
        contract_tracker_lease_ttl_seconds = None
        contract_tracker_scope_hash = None
        contract_tracker_transcript_max_line_number = None
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
        primary_units=contract_primary_units,
        tracker_lease_id=contract_tracker_lease_id,
        tracker_scope_hash=contract_tracker_scope_hash,
        tracker_transcript_max_line_number=contract_tracker_transcript_max_line_number,
    )
    bootstrap = build_worktree_bootstrap(
        repo=root,
        artifact_dir=artifact_dir,
        packet_metadata=metadata,
    )
    scope_path = scope_of_work_path.resolve() if session_context is not None else None
    manifest_path = session_manifest_path.resolve() if packet_session_manifest is not None else None

    tracker_dir_path: Path | None = None
    tracker_repo_key_value: str | None = None
    if manifest_path is not None and manifest_path.exists():
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest_payload = {}
        if isinstance(manifest_payload, dict):
            tracker_meta = manifest_payload.get("tracker") if isinstance(manifest_payload.get("tracker"), dict) else None
            if tracker_meta is not None:
                if isinstance(tracker_meta.get("tracker_dir"), str) and tracker_meta["tracker_dir"]:
                    tracker_dir_path = Path(tracker_meta["tracker_dir"]).expanduser()
                if isinstance(tracker_meta.get("repo_key"), str) and tracker_meta["repo_key"]:
                    tracker_repo_key_value = tracker_meta["repo_key"]
                heartbeat_session = tracker_meta.get("session_id") or manifest_payload.get("session_id")
                if isinstance(heartbeat_session, str) and heartbeat_session:
                    try:
                        diff_tracker.heartbeat(
                            root,
                            session_id=heartbeat_session,
                            run_id=ledger.run_id,
                            lease_id=contract_tracker_lease_id,
                            ttl_seconds=contract_tracker_lease_ttl_seconds,
                            rvf_state_phase="prepare",
                            rvf_backend=canonical_backend,
                        )
                    except Exception:
                        pass
    # 主 dispatch harness 解析（供 dispatch_reviewers.py 只读）。cursor 永远不会被
    # transcript 探测命中——只能经显式 RVF_MAIN_HARNESS 覆盖到达（Q3）。优先级与
    # dispatch_reviewers.resolve_main_harness 对齐：显式 env 覆盖 > transcript 探测 > 默认 codex。
    # 必须在这里 honor 显式 env，否则下面导出的 review-env.sh 会把用户设的
    # RVF_MAIN_HARNESS=cursor 覆盖回 codex，令 env 覆盖路径在端到端失效。
    override_main_harness = os.environ.get("RVF_MAIN_HARNESS")
    if override_main_harness not in VALID_MAIN_HARNESSES:
        override_main_harness = None
    detected_main_harness = detect_transcript_format(transcript) if transcript is not None else None
    main_harness = override_main_harness or detected_main_harness or HOST_CODEX
    main_harness_source = (
        "env-override" if override_main_harness else ("transcript" if detected_main_harness else "default")
    )
    main_harness_path = inputs_dir / "main-harness.json"
    main_harness_path.write_text(
        json.dumps(
            {
                "main_harness": main_harness,
                "detected": detected_main_harness,
                "override": override_main_harness,
                "source": main_harness_source,
                "transcript": str(transcript) if transcript is not None else None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
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
        tracker_dir=tracker_dir_path,
        tracker_repo_key=tracker_repo_key_value,
        tracker_scope_path=resolved_input_tracker_scope_path,
        rvf_backend=canonical_backend,
        parent_context_path=artifact_dir / "parent-conversation-context.md",
        main_harness=main_harness,
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
        "main_harness": main_harness,
        "main_harness_file": str(main_harness_path),
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
        "input_tracker_scope_file": str(resolved_input_tracker_scope_path)
        if resolved_input_tracker_scope_path is not None
        else None,
        "tracker_scope_file": str(resolved_artifact_tracker_scope_path)
        if resolved_artifact_tracker_scope_path is not None
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


def _shared_workflow_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_prep_target_repo(payload: dict[str, Any]) -> Path:
    target_worktree = payload.get("target_worktree")
    if isinstance(target_worktree, str) and target_worktree.strip():
        candidate = Path(target_worktree).expanduser()
        if candidate.exists() and (candidate / ".git").exists():
            return candidate.resolve()
    for key in ("origin_cwd", "origin_repo"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidate = Path(value).expanduser()
            if candidate.exists():
                return candidate.resolve()
    raise ValueError("prep payload missing target_worktree / origin_cwd / origin_repo")


def _shared_workflow_artifacts(result: dict[str, Any]) -> dict[str, str | None]:
    return {
        "scope_contract": result.get("scope_contract"),
        "review_packet": result.get("review_packet"),
        "review_packet_metadata": result.get("review_packet_metadata"),
        "review_env": result.get("review_env_file"),
        "review_agent_context": result.get("review_agent_context_file"),
        "worktree_bootstrap": result.get("worktree_bootstrap"),
        "session_manifest": result.get("session_manifest_file"),
        "scope_of_work": result.get("scope_of_work_file"),
    }


def prepare_run_from_prep_file(
    prep: rvf_prep_file.PrepFileRecord,
    *,
    transcript_override: Path | None = None,
    extra_primary_files: list[str] | None = None,
    extra_background_files: list[str] | None = None,
    timeout_seconds: float = SHARED_WORKFLOW_DEFAULT_TIMEOUT_SECONDS,
    base_dir: Path | None = None,
    user_prompt_excerpt: str | None = None,
) -> dict[str, Any]:
    """Run prepare_run driven by the dispatch prep file. Idempotent on shared_workflow_state.

    Returns the prep payload's shared_workflow_state dict (after possibly updating the prep
    file). Raises on prep schema problems; prepare_run errors are caught and recorded as
    status=failed without raising.
    """
    payload = dict(prep.payload)
    rvf_run = payload.get("rvf_run") if isinstance(payload.get("rvf_run"), dict) else {}
    existing_state = rvf_run.get("shared_workflow_state") if isinstance(rvf_run, dict) else None
    if isinstance(existing_state, dict) and existing_state.get("status") == "completed":
        return dict(existing_state)

    target_flow = str(payload.get("target_flow") or "")
    rvf_backend = TARGET_FLOW_BACKEND.get(target_flow, "manual")
    rvf_run_id = rvf_run.get("run_id") if isinstance(rvf_run, dict) else None
    rvf_run_dir_raw = rvf_run.get("run_dir") if isinstance(rvf_run, dict) else None
    rvf_run_dir = Path(rvf_run_dir_raw).expanduser() if isinstance(rvf_run_dir_raw, str) and rvf_run_dir_raw else None
    tracker_scope_raw = rvf_run.get("tracker_scope_path") if isinstance(rvf_run, dict) else None
    tracker_scope = Path(tracker_scope_raw).expanduser() if isinstance(tracker_scope_raw, str) and tracker_scope_raw else None

    target_repo = _resolve_prep_target_repo(payload)

    transcript_path: Path | None = None
    transcript_candidate = transcript_override
    if transcript_candidate is None:
        for key in ("origin_transcript_path", "parent_thread_path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                transcript_candidate = Path(value).expanduser()
                break
    if transcript_candidate is not None and transcript_candidate.exists():
        transcript_path = transcript_candidate.resolve()

    artifacts_dir = rvf_run_dir / "artifacts" if rvf_run_dir is not None else None
    scope_of_work_path: Path | None = None
    if artifacts_dir is not None:
        candidate = artifacts_dir / "startup-scope-of-work.md"
        if candidate.exists():
            scope_of_work_path = candidate
    if scope_of_work_path is None and artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        scope_text = dispatch_scope_of_work_text(
            target_flow=target_flow,
            cwd=str(target_repo),
            parent_session_id=str(payload.get("origin_session_id") or ""),
            parent_thread_path=transcript_path,
            prompt_path=None,
            run_id=str(rvf_run_id or ""),
            run_dir=rvf_run_dir,
            user_prompt_excerpt=user_prompt_excerpt,
        )
        candidate = artifacts_dir / "startup-scope-of-work.md"
        candidate.write_text(scope_text, encoding="utf-8")
        scope_of_work_path = candidate

    base_dir_path = base_dir or default_base_dir()
    started_at = _shared_workflow_now()
    state: dict[str, Any] = {
        "started_at": started_at,
        "status": "pending",
        "target_flow": target_flow,
        "target_repo": str(target_repo),
        "rvf_backend": rvf_backend,
    }

    def _runner() -> dict[str, Any]:
        return prepare_run(
            repo=target_repo,
            session_context=scope_of_work_path,
            session_manifest=None,
            transcript=transcript_path,
            base_dir=base_dir_path,
            max_file_bytes=200_000,
            max_packet_bytes=0,
            primary_files=list(extra_primary_files or []),
            background_files=list(extra_background_files or []),
            exclude_path_prefixes=[],
            allow_missing_session_context=False,
            rvf_run_id=rvf_run_id,
            rvf_run_dir=rvf_run_dir,
            rvf_backend=rvf_backend,
            tracker_scope=tracker_scope,
        )

    completed_state = state
    # Manage executor manually so a TimeoutError can return immediately. Using
    # `with ThreadPoolExecutor(...)` blocks at __exit__ until the worker thread
    # finishes, defeating the timeout. Cancelling pending futures and asking
    # shutdown not to wait lets the hook unblock; the background thread (which
    # may still be running prepare_run) is allowed to finish on its own.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_runner)
    try:
        result = future.result(timeout=timeout_seconds)
        completed_state = {
            **state,
            "status": "completed",
            "completed_at": _shared_workflow_now(),
            "artifacts": _shared_workflow_artifacts(result),
            "run_id": result.get("run_id"),
            "run_dir": result.get("run_dir"),
        }
        executor.shutdown(wait=True)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {
            **state,
            "status": "timeout",
            "completed_at": _shared_workflow_now(),
            "error": f"prepare_run exceeded {timeout_seconds:.0f}s timeout",
        }
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {
            **state,
            "status": "failed",
            "completed_at": _shared_workflow_now(),
            "error": f"{type(exc).__name__}: {exc}",
        }

    new_rvf_run = dict(rvf_run) if isinstance(rvf_run, dict) else {}
    new_rvf_run["shared_workflow_state"] = completed_state
    rvf_prep_file.update_prep_file(prep, {"rvf_run": new_rvf_run})
    return completed_state


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
        "--tracker-scope",
        help=(
            "Path to an allocator tracker_scope JSON object. Splices into "
            "manifest.tracker.tracker_scope and unlocks scope.contract.json v2 fields. "
            "Requires --session-manifest or --transcript."
        ),
    )
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
        tracker_scope = Path(args.tracker_scope).expanduser().resolve() if args.tracker_scope else None
        if tracker_scope is not None and session_manifest is None and transcript is None:
            raise ValueError(
                "--tracker-scope requires --session-manifest or --transcript so the "
                "tracker_scope payload can be spliced into manifest.tracker.tracker_scope"
            )
        if tracker_scope is not None and not tracker_scope.exists():
            raise ValueError(f"tracker scope file not found: {tracker_scope}")
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
            tracker_scope=tracker_scope,
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
