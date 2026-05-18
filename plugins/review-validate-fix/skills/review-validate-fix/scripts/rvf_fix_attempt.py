#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import diff_tracker  # noqa: E402


def _run(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=text,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
    return completed


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} root must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _required_path(raw: str | None, env_key: str, flag: str) -> Path:
    value = raw or os.environ.get(env_key)
    if not value:
        raise ValueError(f"{flag} or {env_key} is required")
    return Path(value).expanduser().resolve()


def _optional_repo(raw: str | None, attempt: dict[str, Any] | None = None) -> Path:
    value = raw or os.environ.get("RVF_REPO")
    if not value and attempt is not None:
        repo_value = attempt.get("repo")
        if isinstance(repo_value, str) and repo_value:
            value = repo_value
    if not value:
        raise ValueError("--repo/--target-repo, RVF_REPO, or attempt repo is required")
    return Path(value).expanduser().resolve()


def _run_id(run_dir: Path, override: str | None) -> str:
    if override:
        return override
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        value = summary.get("run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("run_id missing; pass --run-id or use a run_dir with summary.json")


def _issue_path(run_dir: Path, issue_id: str) -> Path:
    return run_dir / "artifacts" / "fix-issues" / f"{diff_tracker.safe_token(issue_id)}.json"


def _issue_payload(run_dir: Path, issue_id: str) -> dict[str, Any]:
    path = _issue_path(run_dir, issue_id)
    if not path.is_file():
        raise ValueError(f"issue artifact not found: {path}")
    return _read_json(path)


def _is_safe_relative_path(path: str) -> bool:
    rel = Path(path)
    return bool(path.strip()) and not rel.is_absolute() and ".." not in rel.parts


def _safe_relative_paths(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(path.strip() for path in paths if isinstance(path, str) and _is_safe_relative_path(path)))


def _issue_paths(issue: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("path", "file"):
        value = issue.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    raw_paths = issue.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(item.strip() for item in raw_paths if isinstance(item, str) and item.strip())
    refs = issue.get("artifact_refs")
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict) and isinstance(ref.get("path"), str) and ref["path"].strip():
                paths.append(ref["path"].strip())
    return _safe_relative_paths(paths)


def _issue_source_refs(issue: dict[str, Any]) -> list[dict[str, Any]]:
    refs = issue.get("source_refs")
    if isinstance(refs, list):
        return [item for item in refs if isinstance(item, dict)]
    reviewer_id = issue.get("reviewer_id")
    if isinstance(reviewer_id, str) and reviewer_id:
        return [{"reviewer_id": reviewer_id}]
    return []


def _scope_contract_path(run_dir: Path) -> Path | None:
    env_value = os.environ.get("RVF_SCOPE_CONTRACT")
    if env_value:
        path = Path(env_value).expanduser().resolve()
        if path.is_file():
            return path
    for candidate in (
        run_dir / "artifacts" / "inputs" / "scope.contract.json",
        run_dir / "inputs" / "scope.contract.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _scope_fix_allowlist(run_dir: Path) -> list[str]:
    path = _scope_contract_path(run_dir)
    if path is None:
        return []
    contract = _read_json(path)
    raw = contract.get("fix_allowlist")
    if not isinstance(raw, list):
        canonical = contract.get("canonical_scope")
        if isinstance(canonical, dict):
            raw = canonical.get("fix_allowlist")
    if not isinstance(raw, list):
        return []
    return _safe_relative_paths([item for item in raw if isinstance(item, str)])


def _scope_contract(run_dir: Path) -> dict[str, Any]:
    path = _scope_contract_path(run_dir)
    if path is None:
        return {}
    return _read_json(path)


def _scope_list(contract: dict[str, Any], key: str) -> list[str]:
    raw = contract.get(key)
    if not isinstance(raw, list):
        canonical = contract.get("canonical_scope")
        if isinstance(canonical, dict):
            raw = canonical.get(key)
    if not isinstance(raw, list):
        return []
    return _safe_relative_paths([item for item in raw if isinstance(item, str)])


def _prefix_matches(path: str, prefixes: list[str]) -> bool:
    normalized = path.strip().replace("\\", "/").lstrip("/")
    for raw in prefixes:
        if not isinstance(raw, str) or not raw.strip():
            continue
        prefix = raw.strip().replace("\\", "/").lstrip("/")
        if prefix.endswith("/"):
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                return True
        elif normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _attempt_boundary(issue_paths: list[str], fix_allowlist: list[str]) -> list[str]:
    return list(dict.fromkeys([*issue_paths, *fix_allowlist]))


def _attempt_dir(run_dir: Path, attempt_id: str) -> Path:
    return run_dir / "artifacts" / "fix-attempts" / attempt_id


def _load_attempt(run_dir: Path, attempt_id: str) -> dict[str, Any]:
    path = _attempt_dir(run_dir, attempt_id) / "attempt.json"
    if not path.is_file():
        raise ValueError(f"attempt artifact not found: {path}")
    return _read_json(path)


def _attempt_id(run_id: str, issue_id: str) -> str:
    return f"rvf-attempt-{diff_tracker.safe_token(run_id)[:24]}-{diff_tracker.safe_token(issue_id)}-{secrets.token_hex(4)}"


def _dirty_patch(repo: Path, paths: list[str], out_path: Path) -> bool:
    args = ["diff", "--binary", "HEAD"]
    if paths:
        args.extend(["--", *paths])
    completed = _run(repo, args, check=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(completed.stdout, encoding="utf-8")
    return bool(completed.stdout)


def _copy_untracked(repo: Path, worktree: Path, paths: list[str]) -> list[str]:
    copied: list[str] = []
    for path in paths:
        status = _run(repo, ["ls-files", "--others", "--exclude-standard", "--", path], check=True).stdout.splitlines()
        for item in status:
            if not _is_safe_relative_path(item):
                continue
            src = repo / item
            dst = worktree / item
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            copied.append(item)
    return copied


def _commit_all(repo: Path, message: str) -> str:
    _run(repo, ["add", "-A"])
    status = _run(repo, ["status", "--porcelain"], check=True).stdout
    if status.strip():
        _run(
            repo,
            [
                "-c",
                "user.email=rvf@example.invalid",
                "-c",
                "user.name=RVF Fix Attempt",
                "commit",
                "-q",
                "-m",
                message,
            ],
        )
    return _run(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()


def command_prepare(args: argparse.Namespace) -> int:
    run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
    repo = _required_path(args.repo, "RVF_REPO", "--repo")
    run_id = _run_id(run_dir, args.run_id)
    issue = _issue_payload(run_dir, args.issue_id)
    paths = _issue_paths(issue)
    if not paths:
        raise ValueError("canonical issue must contain at least one relative path")
    fix_allowlist = _scope_fix_allowlist(run_dir)
    boundary_paths = _attempt_boundary(paths, fix_allowlist)
    attempt_id = args.attempt_id or _attempt_id(run_id, args.issue_id)
    attempt_dir = _attempt_dir(run_dir, attempt_id)
    worktree = attempt_dir / "worktree"
    base_head = _run(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()
    if worktree.exists():
        raise ValueError(f"attempt worktree already exists: {worktree}")
    attempt_dir.mkdir(parents=True, exist_ok=True)
    _run(repo, ["worktree", "add", "--detach", str(worktree), base_head])
    baseline_patch = attempt_dir / "baseline.patch"
    had_tracked_overlay = _dirty_patch(repo, boundary_paths, baseline_patch)
    if had_tracked_overlay:
        apply_result = _run(worktree, ["apply", "--whitespace=nowarn", str(baseline_patch)], check=False)
        if apply_result.returncode != 0:
            raise RuntimeError(apply_result.stderr.strip() or "failed to apply baseline overlay")
    copied_untracked = _copy_untracked(repo, worktree, boundary_paths)
    baseline_commit = _commit_all(worktree, f"rvf baseline for {args.issue_id}")
    attempt = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "issue_id": args.issue_id,
        "run_id": run_id,
        "repo": str(repo),
        "worktree_path": str(worktree),
        "base_head": base_head,
        "baseline_overlay_path": str(baseline_patch),
        "baseline_commit": baseline_commit,
        "issue_paths": paths,
        "fix_allowlist": fix_allowlist,
        "boundary_paths": boundary_paths,
        "copied_untracked": copied_untracked,
        "status": "prepared",
    }
    _write_json(attempt_dir / "attempt.json", attempt)
    diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=run_id,
        issue_id=args.issue_id,
        attempt_id=attempt_id,
        worktree_path=worktree,
        base_head=base_head,
        baseline_overlay_path=baseline_patch,
        baseline_commit=baseline_commit,
        status="prepared",
        result_payload=attempt,
        log_root_override=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    print(json.dumps(attempt, ensure_ascii=False, sort_keys=True))
    return 0


def command_start(args: argparse.Namespace) -> int:
    run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
    attempt = _load_attempt(run_dir, args.attempt_id)
    repo = _optional_repo(args.repo, attempt)
    attempt["status"] = "started"
    _write_json(_attempt_dir(run_dir, args.attempt_id) / "attempt.json", attempt)
    result = diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=attempt["run_id"],
        issue_id=attempt["issue_id"],
        attempt_id=args.attempt_id,
        worktree_path=attempt["worktree_path"],
        status="started",
        result_payload=attempt,
        log_root_override=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _intent_to_add_untracked(repo: Path, paths: list[str]) -> None:
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    if paths:
        args.extend(["--", *paths])
    raw = _run(repo, args, check=True).stdout
    paths = [item for item in raw.split("\0") if item]
    if paths:
        _run(repo, ["add", "-N", "--", *paths], check=True)


def _dirty_paths(repo: Path, paths: list[str]) -> list[str]:
    args = ["status", "--porcelain", "-z"]
    if paths:
        args.extend(["--", *paths])
    raw = _run(repo, args, check=True).stdout
    dirty: list[str] = []
    entries = [item for item in raw.split("\0") if item]
    for entry in entries:
        if len(entry) < 4:
            continue
        path = entry[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if _is_safe_relative_path(path):
            dirty.append(path)
    return list(dict.fromkeys(dirty))


def _changed_paths(repo: Path, paths: list[str]) -> list[dict[str, str]]:
    args = ["diff", "--name-status", "HEAD"]
    if paths:
        args.extend(["--", *paths])
    raw = _run(repo, args, check=True).stdout
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        if status.startswith("A"):
            op = "added"
        elif status.startswith("D"):
            op = "deleted"
        else:
            op = "modified"
        out.append({"path": path, "op": op})
    return out


def _scope_expansion_metadata(
    *,
    changed: list[dict[str, str]],
    boundary_paths: list[str],
    declared_paths: list[str],
    reason: str | None,
    scope_contract: dict[str, Any],
) -> dict[str, Any]:
    boundary = set(boundary_paths)
    changed_outside = [item["path"] for item in changed if item["path"] not in boundary]
    declared = set(declared_paths)
    expanded_paths = [path for path in changed_outside if path in declared]
    undeclared_paths = [path for path in changed_outside if path not in declared]
    blocked_prefixes = _scope_list(scope_contract, "excluded_path_prefixes")
    protected_patterns = _scope_list(scope_contract, "protected_files") + _scope_list(scope_contract, "background_files")
    protected_paths = set(protected_patterns)
    blocked_paths = [
        path
        for path in expanded_paths
        if path in protected_paths or _prefix_matches(path, protected_patterns) or _prefix_matches(path, blocked_prefixes)
    ]
    return {
        "expanded_paths": expanded_paths,
        "declared_paths": declared_paths,
        "undeclared_paths": undeclared_paths,
        "blocked_paths": blocked_paths,
        "reason": (reason or "").strip(),
    }


def _validate_scope_expansion(status: str, metadata: dict[str, Any]) -> None:
    if status != "fixed":
        return
    expanded_paths = metadata["expanded_paths"]
    undeclared_paths = metadata["undeclared_paths"]
    blocked_paths = metadata["blocked_paths"]
    reason = metadata["reason"]
    if undeclared_paths:
        joined = ", ".join(undeclared_paths)
        raise ValueError(
            "fixed attempt has undeclared allowlist-external changes; "
            f"pass --scope-expansion-path for each intended path or clean them first: {joined}"
        )
    if expanded_paths and not reason:
        joined = ", ".join(expanded_paths)
        raise ValueError(
            "fixed attempt expands validate/fix scope but no reason was provided; "
            f"pass --scope-expansion-reason: {joined}"
        )
    if blocked_paths:
        joined = ", ".join(blocked_paths)
        raise ValueError(
            "fixed attempt expands into protected/background/excluded paths; "
            f"elevate instead of applying this patch: {joined}"
        )


def _sync_issue_state(
    *,
    repo: Path,
    run_dir: Path,
    issue_id: str,
    state: str,
    log_root: Path | None,
) -> None:
    issue_path = _issue_path(run_dir, issue_id)
    issue = _read_json(issue_path)
    diff_tracker.rvf_issue_upsert(
        repo=repo,
        run_id=str(issue.get("run_id") or _run_id(run_dir, None)),
        issue_id=issue_id,
        payload=issue,
        artifact_path=issue_path,
        source_refs=_issue_source_refs(issue),
        state=state,
        log_root_override=log_root,
    )


def command_stop(args: argparse.Namespace) -> int:
    run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
    attempt = _load_attempt(run_dir, args.attempt_id)
    repo = _optional_repo(args.repo, attempt)
    worktree = Path(attempt["worktree_path"]).expanduser().resolve()
    boundary_paths = _safe_relative_paths(attempt.get("boundary_paths", []))
    if not boundary_paths:
        boundary_paths = _attempt_boundary(
            _safe_relative_paths(attempt.get("issue_paths", [])),
            _safe_relative_paths(attempt.get("fix_allowlist", [])),
        )
    _intent_to_add_untracked(worktree, [])
    all_changed = _changed_paths(worktree, [])
    scope_expansion = _scope_expansion_metadata(
        changed=all_changed,
        boundary_paths=boundary_paths,
        declared_paths=_safe_relative_paths(args.scope_expansion_path or []),
        reason=args.scope_expansion_reason,
        scope_contract=_scope_contract(run_dir),
    )
    patch_paths = list(dict.fromkeys([*boundary_paths, *scope_expansion["expanded_paths"]]))
    fix_patch = _attempt_dir(run_dir, args.attempt_id) / "fix.patch"
    patch_args = ["diff", "--binary", "HEAD"]
    if patch_paths:
        patch_args.extend(["--", *patch_paths])
    patch_text = _run(worktree, patch_args, check=True).stdout
    status = args.status
    if status == "auto":
        status = "fixed" if patch_text.strip() else "false_positive"
    _validate_scope_expansion(status, scope_expansion)
    fix_patch.write_text(patch_text, encoding="utf-8")
    changed = _changed_paths(worktree, patch_paths)
    result_payload = {}
    if args.result_file:
        result_payload = _read_json(Path(args.result_file).expanduser().resolve())
    result = {
        "schema_version": 1,
        "attempt_id": args.attempt_id,
        "issue_id": attempt["issue_id"],
        "run_id": attempt["run_id"],
        "status": status,
        "fix_patch_path": str(fix_patch),
        "changed_paths": changed,
        "all_changed_paths": all_changed,
        "patch_paths": patch_paths,
        "scope_expansion": scope_expansion,
        "result_payload": result_payload,
    }
    _write_json(_attempt_dir(run_dir, args.attempt_id) / "result.json", result)
    attempt.update(
        {
            "status": status,
            "fix_patch_path": str(fix_patch),
            "changed_paths": changed,
            "all_changed_paths": all_changed,
            "patch_paths": patch_paths,
            "scope_expansion": scope_expansion,
        }
    )
    _write_json(_attempt_dir(run_dir, args.attempt_id) / "attempt.json", attempt)
    log_root = Path(args.log_root).expanduser().resolve() if args.log_root else None
    diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=attempt["run_id"],
        issue_id=attempt["issue_id"],
        attempt_id=args.attempt_id,
        worktree_path=attempt["worktree_path"],
        fix_patch_path=fix_patch,
        status=status,
        result_payload=result,
        log_root_override=log_root,
    )
    _sync_issue_state(
        repo=repo,
        run_dir=run_dir,
        issue_id=attempt["issue_id"],
        state=status,
        log_root=log_root,
    )
    diff_tracker.rvf_patch_events_replace(
        repo=repo,
        attempt_id=args.attempt_id,
        events=[
            {
                "path": item["path"],
                "op": item["op"],
                "diff_ref": {"fix_patch_path": str(fix_patch)},
            }
            for item in changed
        ],
        log_root_override=log_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
    attempt = _load_attempt(run_dir, args.attempt_id)
    repo = _optional_repo(args.target_repo, attempt)
    fix_patch = Path(attempt.get("fix_patch_path") or _attempt_dir(run_dir, args.attempt_id) / "fix.patch").resolve()
    if not fix_patch.is_file():
        raise ValueError(f"fix patch not found: {fix_patch}")
    scope_expansion = attempt.get("scope_expansion")
    expanded_paths = []
    if isinstance(scope_expansion, dict):
        expanded_paths = _safe_relative_paths(scope_expansion.get("expanded_paths", []))
    expanded_dirty_paths = _dirty_paths(repo, expanded_paths) if expanded_paths else []
    if expanded_dirty_paths:
        apply_result = subprocess.CompletedProcess(
            args=["git", "apply", "--whitespace=nowarn", str(fix_patch)],
            returncode=4,
            stdout="",
            stderr="scope expansion paths are dirty in target repo: " + ", ".join(expanded_dirty_paths),
        )
        status = "scope_expansion_conflict"
    else:
        apply_result = _run(repo, ["apply", "--whitespace=nowarn", str(fix_patch)], check=False)
        status = "applied" if apply_result.returncode == 0 else "merge_conflict"
    attempt["status"] = status
    _write_json(_attempt_dir(run_dir, args.attempt_id) / "attempt.json", attempt)
    tracker_status = "merge_conflict" if status == "scope_expansion_conflict" else status
    diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=attempt["run_id"],
        issue_id=attempt["issue_id"],
        attempt_id=args.attempt_id,
        worktree_path=attempt["worktree_path"],
        fix_patch_path=fix_patch,
        status=tracker_status,
        result_payload={
            "status": status,
            "apply_returncode": apply_result.returncode,
            "stdout": apply_result.stdout,
            "stderr": apply_result.stderr,
        },
        log_root_override=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    payload = {"attempt_id": args.attempt_id, "status": status, "stderr": apply_result.stderr}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if status == "applied":
        return 0
    if status == "scope_expansion_conflict":
        return 4
    return 3


def command_status(args: argparse.Namespace) -> int:
    run_id = args.run_id
    run_dir: Path | None = None
    if not run_id:
        run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
        run_id = _run_id(run_dir, None)
    repo = _optional_repo(args.repo)
    payload = diff_tracker.rvf_causality_for_run(
        repo=repo,
        run_id=run_id,
        log_root_override=Path(args.log_root).expanduser().resolve() if args.log_root else None,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage RVF issue-scoped validate/fix attempts.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo", default=None)
    prepare.add_argument("--run-dir", default=None)
    prepare.add_argument("--run-id", default=None)
    prepare.add_argument("--issue-id", required=True)
    prepare.add_argument("--attempt-id", default=None)
    prepare.add_argument("--log-root", default=None)
    prepare.set_defaults(func=command_prepare)

    start = sub.add_parser("start")
    start.add_argument("--attempt-id", required=True)
    start.add_argument("--repo", default=None)
    start.add_argument("--run-dir", default=None)
    start.add_argument("--log-root", default=None)
    start.set_defaults(func=command_start)

    stop = sub.add_parser("stop")
    stop.add_argument("--attempt-id", required=True)
    stop.add_argument("--repo", default=None)
    stop.add_argument("--run-dir", default=None)
    stop.add_argument(
        "--status",
        choices=("auto", "fixed", "false_positive", "elevated", "failed"),
        default="auto",
    )
    stop.add_argument("--result-file", default=None)
    stop.add_argument(
        "--scope-expansion-path",
        action="append",
        default=[],
        help="Allow one validate/fix patch path outside the original issue/fix_allowlist boundary. Repeat as needed.",
    )
    stop.add_argument(
        "--scope-expansion-reason",
        default=None,
        help="Required with --status fixed when allowlist-external paths are included in the patch.",
    )
    stop.add_argument("--log-root", default=None)
    stop.set_defaults(func=command_stop)

    apply = sub.add_parser("apply")
    apply.add_argument("--attempt-id", required=True)
    apply.add_argument("--target-repo", default=None)
    apply.add_argument("--run-dir", default=None)
    apply.add_argument("--log-root", default=None)
    apply.set_defaults(func=command_apply)

    status = sub.add_parser("status")
    status.add_argument("--repo", default=None)
    status.add_argument("--run-dir", default=None)
    status.add_argument("--run-id", default=None)
    status.add_argument("--log-root", default=None)
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
