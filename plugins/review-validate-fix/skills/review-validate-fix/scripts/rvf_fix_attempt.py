#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402


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
    return run_dir / "artifacts" / "fix-issues" / f"{reviewable_unit_diff_tracker.safe_token(issue_id)}.json"


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
    return f"rvf-attempt-{reviewable_unit_diff_tracker.safe_token(run_id)[:24]}-{reviewable_unit_diff_tracker.safe_token(issue_id)}-{secrets.token_hex(4)}"


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


def prepare_fix_attempt(
    *,
    run_dir: Path,
    repo: Path,
    issue_id: str,
    run_id_override: str | None = None,
    attempt_id_override: str | None = None,
    log_root: Path | None = None,
) -> dict[str, Any]:
    """为一条 canonical issue 建隔离 attempt worktree 并落 ``attempt.json``（status=prepared）。"""
    run_id = _run_id(run_dir, run_id_override)
    issue = _issue_payload(run_dir, issue_id)
    paths = _issue_paths(issue)
    if not paths:
        raise ValueError("canonical issue must contain at least one relative path")
    fix_allowlist = _scope_fix_allowlist(run_dir)
    boundary_paths = _attempt_boundary(paths, fix_allowlist)
    attempt_id = attempt_id_override or _attempt_id(run_id, issue_id)
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
    baseline_commit = _commit_all(worktree, f"rvf baseline for {issue_id}")
    attempt = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "issue_id": issue_id,
        # ``apply --all-stopped`` 按它排序合回顺序；attempt.json 会被 stop/apply 反复重写，
        # 文件 mtime 不能当创建时间用。
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    reviewable_unit_diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=run_id,
        issue_id=issue_id,
        attempt_id=attempt_id,
        worktree_path=worktree,
        base_head=base_head,
        baseline_overlay_path=baseline_patch,
        baseline_commit=baseline_commit,
        status="prepared",
        result_payload=attempt,
        log_root_override=log_root,
    )
    return attempt


def _log_root_arg(raw: str | None) -> Path | None:
    return Path(raw).expanduser().resolve() if raw else None


def command_prepare(args: argparse.Namespace) -> int:
    attempt = prepare_fix_attempt(
        run_dir=_required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir"),
        repo=_required_path(args.repo, "RVF_REPO", "--repo"),
        issue_id=args.issue_id,
        run_id_override=args.run_id,
        attempt_id_override=args.attempt_id,
        log_root=_log_root_arg(args.log_root),
    )
    print(json.dumps(attempt, ensure_ascii=False, sort_keys=True))
    return 0


def mark_fix_attempt_started(
    *,
    run_dir: Path,
    attempt_id: str,
    repo_override: str | None = None,
    log_root: Path | None = None,
) -> dict[str, Any]:
    """纯记账：attempt → started。``rvf fix prepare`` 在派发前一并调用，子代理侧不再需要调它。"""
    attempt = _load_attempt(run_dir, attempt_id)
    repo = _optional_repo(repo_override, attempt)
    attempt["status"] = "started"
    _write_json(_attempt_dir(run_dir, attempt_id) / "attempt.json", attempt)
    return reviewable_unit_diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=attempt["run_id"],
        issue_id=attempt["issue_id"],
        attempt_id=attempt_id,
        worktree_path=attempt["worktree_path"],
        status="started",
        result_payload=attempt,
        log_root_override=log_root,
    )


# validate/fix 子代理在后台跑、主 agent 结束回合等它的完成通知时，这段等待期间 Stop 不应发续跑提示
# （读取方：Stop 的 ``awaiting_dispatched_agent_decision``）。``rvf fix prepare`` 按 attempt 登记，
# 子代理 ``stop`` 成功、或主会话 ``rvf apply`` 消费结果时清除。子代理可能静默死掉、根本不调 stop，
# 故 marker 以 TTL 兜底自释放。
# EXPERIMENT_NODE: fix 子代理 awaiting marker 的 TTL——太短则长修复期间误发续跑提示并耗掉预算，太长则子代理死掉后主 agent 真卡住时迟迟没有提示
FIX_ATTEMPT_SUBAGENT_AWAITING_MARKER_TTL_SECONDS = 60 * 60
FIX_ATTEMPT_SUBAGENT_AWAITING_MARKER_DISPATCHER = "rvf fix prepare（validate/fix 子代理）"
# 子代理的 shell 未必带主会话的 session id（Codex / Kimi 子代理、或 host 给子代理另发会话），
# clear 时不能靠环境反推，故 arm 时把主会话 id 记进 attempt.json。
ATTEMPT_FIELD_MAIN_AGENT_SESSION_ID_AWAITING_FIX_SUBAGENT = "main_agent_session_id_awaiting_fix_subagent"


def arm_fix_attempt_subagent_awaiting_marker(
    *, run_dir: Path, attempt_id: str, main_agent_session_id: str
) -> Path | None:
    """登记「主会话正在等这个 attempt 的子代理」，返回 marker 路径；并把主会话 id 记进 attempt.json。"""
    import rvf_awaiting_dispatched_agent_marker  # noqa: PLC0415 - 只在真要登记时加载 marker 族

    attempt_path = _attempt_dir(run_dir, attempt_id) / "attempt.json"
    attempt = _read_json(attempt_path)
    attempt[ATTEMPT_FIELD_MAIN_AGENT_SESSION_ID_AWAITING_FIX_SUBAGENT] = main_agent_session_id
    _write_json(attempt_path, attempt)
    return rvf_awaiting_dispatched_agent_marker.arm_awaiting_dispatched_agent(
        main_session_id=main_agent_session_id,
        dispatched_agent_id=attempt_id,
        dispatcher=FIX_ATTEMPT_SUBAGENT_AWAITING_MARKER_DISPATCHER,
        description=f"validate/fix 子代理处理 issue {attempt.get('issue_id')}",
        repo=attempt.get("repo"),
        cwd=attempt.get("worktree_path"),
        ttl_seconds_override=FIX_ATTEMPT_SUBAGENT_AWAITING_MARKER_TTL_SECONDS,
    )


def clear_fix_attempt_subagent_awaiting_marker(attempt: dict[str, Any]) -> list[str]:
    """清掉该 attempt 的子代理 awaiting marker（未登记过时是 no-op）。best-effort，从不抛。"""
    main_agent_session_id = attempt.get(ATTEMPT_FIELD_MAIN_AGENT_SESSION_ID_AWAITING_FIX_SUBAGENT)
    if not isinstance(main_agent_session_id, str) or not main_agent_session_id:
        return []
    try:
        import rvf_awaiting_dispatched_agent_marker  # noqa: PLC0415

        return rvf_awaiting_dispatched_agent_marker.clear_awaiting_dispatched_agent(
            main_session_id=main_agent_session_id, dispatched_agent_id=attempt.get("attempt_id")
        )
    except Exception:  # noqa: BLE001 - 清不掉只会让 TTL 兜底，不得打断 stop / apply
        return []


def command_start(args: argparse.Namespace) -> int:
    result = mark_fix_attempt_started(
        run_dir=_required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir"),
        attempt_id=args.attempt_id,
        repo_override=args.repo,
        log_root=_log_root_arg(args.log_root),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


ATTEMPT_WORKTREE_DIRECTORY_NAME = "worktree"
FIX_ATTEMPTS_DIRECTORY_NAME = "fix-attempts"


def infer_run_dir_and_attempt_id_from_attempt_worktree_path(path: Path) -> tuple[Path, str] | None:
    """cwd 落在 ``<run_dir>/artifacts/fix-attempts/<attempt_id>/worktree[/…]`` 里时反推两者。

    validate/fix 子代理的 cwd 就是 attempt worktree，路径本身已经编码了它属于哪个 run、哪个
    attempt——让子代理再手抄一遍 ``--attempt-id`` / ``RVF_RUN_DIR`` 只是多一处出错点。
    """
    resolved = Path(path).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        attempt_dir = candidate.parent
        if (
            candidate.name == ATTEMPT_WORKTREE_DIRECTORY_NAME
            and attempt_dir.parent.name == FIX_ATTEMPTS_DIRECTORY_NAME
            and attempt_dir.parent.parent.name == "artifacts"
            and (attempt_dir / "attempt.json").is_file()
        ):
            return attempt_dir.parent.parent.parent, attempt_dir.name
    return None


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
    reviewable_unit_diff_tracker.rvf_issue_upsert(
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
    inferred = infer_run_dir_and_attempt_id_from_attempt_worktree_path(Path.cwd())
    if args.attempt_id is None:
        if inferred is None:
            raise ValueError(
                "--attempt-id is required outside an attempt worktree "
                "(cwd is not <run_dir>/artifacts/fix-attempts/<attempt_id>/worktree)"
            )
        args.attempt_id = inferred[1]
    if args.run_dir or os.environ.get("RVF_RUN_DIR"):
        run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
    elif inferred is not None:
        run_dir = inferred[0]
    else:
        run_dir = _required_path(None, "RVF_RUN_DIR", "--run-dir")
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
    # 完成状态已落盘 = 子代理这一份工作已交付；此后主 agent 再停下就该照常收到续跑提示。
    # 放在 tracker 记账之前：记账抛错时 attempt 也已经是 stopped，marker 不能残留到 TTL。
    clear_fix_attempt_subagent_awaiting_marker(attempt)
    log_root = Path(args.log_root).expanduser().resolve() if args.log_root else None
    reviewable_unit_diff_tracker.rvf_attempt_upsert(
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
    reviewable_unit_diff_tracker.rvf_patch_events_replace(
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


FIX_ATTEMPT_APPLY_STATUS_APPLIED = "applied"
FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT = "merge_conflict"
FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT = "scope_expansion_conflict"


def apply_fix_attempt(
    *,
    run_dir: Path,
    attempt_id: str,
    target_repo_override: str | None = None,
    log_root: Path | None = None,
) -> dict[str, Any]:
    """把 attempt 的 ``fix.patch`` 合回主 RVF worktree，返回 ``{attempt_id, status, stderr}``。"""
    attempt = _load_attempt(run_dir, attempt_id)
    repo = _optional_repo(target_repo_override, attempt)
    fix_patch = Path(attempt.get("fix_patch_path") or _attempt_dir(run_dir, attempt_id) / "fix.patch").resolve()
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
        status = FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT
    else:
        apply_result = _run(repo, ["apply", "--whitespace=nowarn", str(fix_patch)], check=False)
        status = (
            FIX_ATTEMPT_APPLY_STATUS_APPLIED
            if apply_result.returncode == 0
            else FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT
        )
    attempt["status"] = status
    _write_json(_attempt_dir(run_dir, attempt_id) / "attempt.json", attempt)
    tracker_status = (
        FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT
        if status == FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT
        else status
    )
    reviewable_unit_diff_tracker.rvf_attempt_upsert(
        repo=repo,
        run_id=attempt["run_id"],
        issue_id=attempt["issue_id"],
        attempt_id=attempt_id,
        worktree_path=attempt["worktree_path"],
        fix_patch_path=fix_patch,
        status=tracker_status,
        result_payload={
            "status": status,
            "apply_returncode": apply_result.returncode,
            "stdout": apply_result.stdout,
            "stderr": apply_result.stderr,
        },
        log_root_override=log_root,
    )
    return {"attempt_id": attempt_id, "status": status, "stderr": apply_result.stderr}


def command_apply(args: argparse.Namespace) -> int:
    payload = apply_fix_attempt(
        run_dir=_required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir"),
        attempt_id=args.attempt_id,
        target_repo_override=args.target_repo,
        log_root=_log_root_arg(args.log_root),
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    status = payload["status"]
    if status == FIX_ATTEMPT_APPLY_STATUS_APPLIED:
        return 0
    if status == FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT:
        return 4
    return 3


def command_status(args: argparse.Namespace) -> int:
    run_id = args.run_id
    run_dir: Path | None = None
    if not run_id:
        run_dir = _required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir")
        run_id = _run_id(run_dir, None)
    repo = _optional_repo(args.repo)
    payload = reviewable_unit_diff_tracker.rvf_causality_for_run(
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
    stop.add_argument(
        "--attempt-id",
        default=None,
        help="Defaults to the attempt whose worktree contains the cwd.",
    )
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
