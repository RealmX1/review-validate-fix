#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
import rvf_fix_issue  # noqa: E402
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402
from review_scope_excluded_path_prefixes import (  # noqa: E402
    BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE,
)

FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG = (
    reviewable_unit_diff_tracker.RVF_ATTEMPT_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG
)
FIX_ATTEMPT_STOP_STATUSES = (
    "auto",
    "fixed",
    "false_positive",
    "elevated",
    "failed",
    FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG,
)
# validate-only attempt（范围外问题）只允许这几种收尾：误报、验证属实并提交 backlog、升级、失败。
VALIDATE_ONLY_ATTEMPT_ALLOWED_STOP_STATUSES = (
    "false_positive",
    FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG,
    "elevated",
    "failed",
)
# attempt 终态 → tracker issue 终态（只有 backlog 这一对名字不同）。
ISSUE_STATE_BY_FIX_ATTEMPT_STOP_STATUS = {
    FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG: (
        reviewable_unit_diff_tracker.RVF_ISSUE_STATE_SUBMITTED_TO_KANBAN_BACKLOG
    ),
}
# 范围外发现没能留下持久事实（卡片 / pending）⇒ stop 不收尾、attempt 保持可重试，以此退出码区分于参数错误（2）。
FIX_ATTEMPT_STOP_EXIT_CODE_KANBAN_BACKLOG_SUBMISSION_FAILED_ATTEMPT_STILL_RETRYABLE = 3
# EXPERIMENT_NODE: attempt 全量物化的单文件体积上限——太小则大生成物缺失、测试跑不起来；太大则每个 attempt 复制成本与对象库膨胀
FULL_WORKING_TREE_MATERIALIZATION_MAXIMUM_FILE_BYTES = 20 * 1024 * 1024


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


def _working_tree_status_paths(repo: Path) -> list[str]:
    """``git status --porcelain=v1 -z`` 里出现的全部路径（rename / copy 两端都算）。"""
    raw = _run(
        repo,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"],
        check=True,
    ).stdout
    return _porcelain_v1_z_status_paths_including_both_rename_and_copy_ends(raw)


def _porcelain_v1_z_status_paths_including_both_rename_and_copy_ends(raw: str) -> list[str]:
    """解析 ``git status --porcelain=v1 -z``：rename / copy 的旧路径是紧随条目之后的独立 NUL 字段（没有
    `` -> ``），两端都算。历史缺陷：把旧路径字段当成新条目、切掉前 3 个字符，得到不存在的残缺路径。"""
    fields = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status_code, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status_code or "C" in status_code:
            if index < len(fields) and fields[index]:
                paths.append(fields[index])
            index += 1
    return list(dict.fromkeys(paths))


def _clear_layout_obstructions_for_destination(worktree: Path, destination: Path, *, remove_directory_at_destination: bool) -> None:
    """让 destination 能以主工作区的布局落地：父路径上的文件 / symlink 先 unlink，目标处残留的目录外壳先 rmtree。

    attempt worktree 是从 base HEAD 检出的，主工作区若把「被跟踪目录 ↔ 同路径文件」互换，
    检出的旧形态会挡路（目录外壳让 copy2 写成 ``a/a``；父路径是文件让 mkdir 抛 NotADirectoryError）。"""
    relative_parts = destination.relative_to(worktree).parts
    current = worktree
    for part in relative_parts[:-1]:
        current = current / part
        if current.is_symlink() or current.is_file():
            current.unlink()
            break
    if remove_directory_at_destination and destination.is_dir() and not destination.is_symlink():
        shutil.rmtree(destination)


def _materialize_full_working_tree_state(repo: Path, worktree: Path) -> dict[str, list[Any]]:
    """把主工作区相对 HEAD 的全部改动（tracked 修改 / 删除 / rename、untracked 文件）逐文件复制进 attempt。

    逐文件复制而不是打 patch：patch 在 rename 只露一端、二进制、子模块时会失败或缺一半
    （历史缺陷：pathspec 截断的 baseline 只捕获 rename 的新文件、漏掉旧文件的删除）。
    跳过内嵌仓库（status 里以 ``/`` 结尾的条目）与超体积上限的文件，跳过清单如实返回。"""
    materialized: list[str] = []
    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    for path in _working_tree_status_paths(repo):
        if path.endswith("/"):
            skipped.append({"path": path, "reason": "embedded_repository_or_directory_entry"})
            continue
        if not _is_safe_relative_path(path):
            skipped.append({"path": path, "reason": "unsafe_relative_path"})
            continue
        source = repo / path
        destination = worktree / path
        if source.is_symlink() or source.is_file():
            _clear_layout_obstructions_for_destination(worktree, destination, remove_directory_at_destination=True)
        if source.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() or destination.exists():
                destination.unlink()
            os.symlink(os.readlink(source), destination)
            materialized.append(path)
        elif source.is_file():
            if source.stat().st_size > FULL_WORKING_TREE_MATERIALIZATION_MAXIMUM_FILE_BYTES:
                skipped.append({"path": path, "reason": "exceeds_full_working_tree_materialization_maximum_file_bytes"})
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                destination.unlink()
            shutil.copy2(source, destination)
            materialized.append(path)
        elif source.is_dir():
            skipped.append({"path": path, "reason": "embedded_repository_or_directory_entry"})
        else:
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
                removed.append(path)
    return {"materialized_paths": materialized, "removed_paths": removed, "skipped_paths": skipped}


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
    # 观察面 = 真实工作区：全部 dirty / untracked 都物化进 attempt（不再只叠 boundary 内的），
    # 修改权仍由 stop 时的 boundary / 声明 / 确认三档控制。baseline 提交含全部 WIP，
    # 所以 stop 时 diff baseline 只剩 fixer 自己的改动。
    materialization = _materialize_full_working_tree_state(repo, worktree)
    baseline_commit = _commit_all(worktree, f"rvf baseline for {issue_id}")
    baseline_patch = attempt_dir / "baseline.patch"
    baseline_patch.write_text(
        _run(worktree, ["diff", "--binary", base_head, baseline_commit], check=True).stdout, encoding="utf-8"
    )
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
        "fix_issue_handling_mode": rvf_fix_issue.fix_issue_handling_mode(issue),
        "materialized_working_tree_paths": materialization["materialized_paths"],
        "removed_working_tree_paths": materialization["removed_paths"],
        "skipped_working_tree_paths": materialization["skipped_paths"],
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
    return [
        path
        for path in _porcelain_v1_z_status_paths_including_both_rename_and_copy_ends(raw)
        if _is_safe_relative_path(path)
    ]


def _changed_paths(repo: Path, paths: list[str]) -> list[dict[str, str]]:
    """``git diff --name-status HEAD`` 的改动路径（越界三档分类、patch 路径与改动记录都以它为准）。

    rename（``R*``，带旧、新两端）：旧路径按 ``deleted``、新路径按 ``added`` 双双计入。历史缺陷：只取新路径
    ⇒ 把 protected / background 文件 ``git mv`` 到已声明的普通外扩路径后旧路径隐身，删除它绕过 fixer 确认与
    主会话确认（旧路径在 boundary 内时 patch 照样删它；在 boundary 外时 rename 只合回一半）。
    copy（``C*``）只计新路径：源路径内容未变，不是本 attempt 的改动；源若也被改过，git 会另给它一条 ``M``。"""
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
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            if status.startswith("R"):
                out.append({"path": parts[1], "op": "deleted"})
            out.append({"path": parts[2], "op": "added"})
            continue
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
    acknowledged_protected_or_background_paths: list[str],
    reason: str | None,
    scope_contract: dict[str, Any],
) -> dict[str, Any]:
    """把 attempt 的改动按「护栏而非闸门」三档分类。

    - boundary 内的普通路径：直接改；
    - boundary 外的普通路径：stop 时声明（``--scope-expansion-path``）+ 理由，记 warning、apply 照常；
    - background / protected / 用户声明的 excluded（**含 boundary 内**——issue 路径恰好落在别的会话
      WIP 上时同样算）：fixer 显式确认（``--acknowledged-protected-or-background-path-modification``）+
      理由后 stop 接受，apply 前还要主会话核对后确认；
    - 内置工具托管前缀（``.serena/``）：编辑工具生成的状态从来不是有效修复，保持硬拒。"""
    boundary = set(boundary_paths)
    changed_paths = list(dict.fromkeys(item["path"] for item in changed))
    builtin_managed_prefixes = list(BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE)
    user_declared_excluded_prefixes = [
        prefix for prefix in _scope_list(scope_contract, "excluded_path_prefixes")
        if prefix.rstrip("/") + "/" not in builtin_managed_prefixes
    ]
    protected_patterns = _scope_list(scope_contract, "protected_files") + _scope_list(scope_contract, "background_files")
    builtin_tool_managed_paths_modified = [path for path in changed_paths if _prefix_matches(path, builtin_managed_prefixes)]
    protected_or_background_paths_modified = [
        path
        for path in changed_paths
        if path not in builtin_tool_managed_paths_modified
        and (
            path in set(protected_patterns)
            or _prefix_matches(path, protected_patterns)
            or _prefix_matches(path, user_declared_excluded_prefixes)
        )
    ]
    acknowledged = set(acknowledged_protected_or_background_paths)
    declared = set(declared_paths)
    ordinary_paths_outside_boundary = [
        path
        for path in changed_paths
        if path not in boundary
        and path not in builtin_tool_managed_paths_modified
        and path not in protected_or_background_paths_modified
    ]
    return {
        "expanded_paths": [path for path in ordinary_paths_outside_boundary if path in declared],
        "declared_paths": declared_paths,
        "undeclared_paths": [path for path in ordinary_paths_outside_boundary if path not in declared],
        "acknowledged_protected_or_background_paths": [
            path for path in protected_or_background_paths_modified if path in acknowledged
        ],
        "unacknowledged_protected_or_background_paths": [
            path for path in protected_or_background_paths_modified if path not in acknowledged
        ],
        # 只剩内置工具托管前缀仍是硬拒；键名沿用 blocked_paths。
        "blocked_paths": builtin_tool_managed_paths_modified,
        "reason": (reason or "").strip(),
    }


def _validate_scope_expansion(status: str, metadata: dict[str, Any], *, attempt_id: str) -> None:
    if status != "fixed":
        return
    expanded_paths = metadata["expanded_paths"]
    acknowledged_paths = metadata["acknowledged_protected_or_background_paths"]
    undeclared_paths = metadata["undeclared_paths"]
    unacknowledged_paths = metadata["unacknowledged_protected_or_background_paths"]
    blocked_paths = metadata["blocked_paths"]
    reason = metadata["reason"]
    if blocked_paths:
        raise ValueError(
            "fixed attempt modifies developer-tool-managed paths (e.g. .serena/); editing generated tool state is "
            f"never a valid fix — revert them: {', '.join(blocked_paths)}"
        )
    if undeclared_paths:
        flags = " ".join(f"--scope-expansion-path {shlex.quote(path)}" for path in undeclared_paths)
        raise ValueError(
            "fixed attempt has undeclared allowlist-external changes; if they are part of the minimal fix, rerun "
            f"`rvf fix stop --attempt-id {attempt_id} ... {flags} --scope-expansion-reason <why>`, "
            "otherwise revert them"
        )
    if unacknowledged_paths:
        flags = " ".join(
            f"--acknowledged-protected-or-background-path-modification {shlex.quote(path)}"
            for path in unacknowledged_paths
        )
        raise ValueError(
            "fixed attempt modifies protected / background / excluded paths (other sessions' uncommitted work or "
            "user-declared exclusions). If this is required by the minimal fix, acknowledge it explicitly — the main "
            f"agent will verify and confirm before apply: `rvf fix stop --attempt-id {attempt_id} ... {flags} "
            "--scope-expansion-reason <why>`; otherwise revert them"
        )
    if (expanded_paths or acknowledged_paths) and not reason:
        joined = ", ".join([*expanded_paths, *acknowledged_paths])
        raise ValueError(
            "fixed attempt expands validate/fix scope but no reason was provided; "
            f"pass --scope-expansion-reason: {joined}"
        )


def _blob_id_at_commit(worktree: Path, commit: str, path: str) -> str | None:
    completed = _run(worktree, ["rev-parse", "--verify", "--quiet", f"{commit}:{path}"], check=False)
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _current_working_tree_blob_id(repo: Path, path: str) -> str | None:
    target = repo / path
    if not (target.exists() or target.is_symlink()):
        return None
    completed = _run(repo, ["hash-object", f"--path={path}", "--", path], check=False)
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


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
        # 镜像已剥掉来源字段；None = 保留 tracker 里由合并处置派生的 source_refs。
        source_refs=None,
        state=state,
        log_root_override=log_root,
    )


def _load_kanban_backlog_findings_before_any_write(args: argparse.Namespace) -> list[dict[str, Any]]:
    """在写任何东西之前校验 finding 文件：不合法就让 stop 整体失败（exit 2），什么都不落盘。"""
    raw = getattr(args, "kanban_backlog_finding_file", None)
    if not raw:
        return []
    import rvf_out_of_scope_findings_kanban_backlog_submission as backlog_submission  # noqa: PLC0415

    return backlog_submission.load_out_of_scope_findings_file(Path(raw).expanduser().resolve())


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
    handling_mode = attempt.get("fix_issue_handling_mode") or rvf_fix_issue.FIX_ISSUE_HANDLING_MODE_VALIDATE_THEN_FIX
    validate_only = handling_mode == rvf_fix_issue.FIX_ISSUE_HANDLING_MODE_VALIDATE_ONLY_THEN_SUBMIT_TO_KANBAN_BACKLOG
    kanban_backlog_findings = _load_kanban_backlog_findings_before_any_write(args)
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
        acknowledged_protected_or_background_paths=_safe_relative_paths(
            args.acknowledged_protected_or_background_path_modification or []
        ),
        reason=args.scope_expansion_reason,
        scope_contract=_scope_contract(run_dir),
    )
    patch_paths = list(
        dict.fromkeys(
            [
                *boundary_paths,
                *scope_expansion["expanded_paths"],
                *scope_expansion["acknowledged_protected_or_background_paths"],
            ]
        )
    )
    fix_patch = _attempt_dir(run_dir, args.attempt_id) / "fix.patch"
    patch_args = ["diff", "--binary", "HEAD"]
    if patch_paths:
        patch_args.extend(["--", *patch_paths])
    patch_text = _run(worktree, patch_args, check=True).stdout
    status = args.status
    if validate_only:
        # 开发者明确要求：范围外问题只验证、不修。这是硬性约束。
        if status not in VALIDATE_ONLY_ATTEMPT_ALLOWED_STOP_STATUSES:
            raise ValueError(
                f"attempt {args.attempt_id} is validate-only ({handling_mode}): pass --status one of "
                f"{list(VALIDATE_ONLY_ATTEMPT_ALLOWED_STOP_STATUSES)}; it must not be fixed in this round"
            )
        if all_changed:
            raise ValueError(
                f"attempt {args.attempt_id} is validate-only ({handling_mode}) and must not modify files; revert: "
                + ", ".join(item["path"] for item in all_changed)
            )
        if status == FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG and not kanban_backlog_findings:
            raise ValueError(
                f"--status {status} requires --kanban-backlog-finding-file describing the validated problem"
            )
    else:
        if status == FIX_ATTEMPT_STOP_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG:
            raise ValueError(
                f"--status {status} only applies to validate-only attempts; if you judge this issue to be outside the "
                "current round, stop with --status elevated and explain why (the main agent decides whether to "
                "re-prepare it as validate-only)"
            )
        if status == "auto":
            if patch_text.strip():
                status = "fixed"
            elif all_changed:
                # 历史缺陷：改动全在 boundary 外且未声明时 patch 为空，被静默判成 false_positive、校验整体跳过。
                raise ValueError(
                    "--status auto found changes outside the attempt boundary but an empty boundary patch; declare them "
                    "(--scope-expansion-path / --acknowledged-protected-or-background-path-modification) or revert "
                    "them, and pass an explicit --status"
                )
            else:
                status = "false_positive"
    _validate_scope_expansion(status, scope_expansion, attempt_id=args.attempt_id)
    result_payload = {}
    if args.result_file:
        result_payload = _read_json(Path(args.result_file).expanduser().resolve())
    # 先提交范围外发现、后落任何 stop 工件：提交没能为每条发现留下持久事实（卡片 / pending）就不得把 attempt /
    # issue 推进到终态（否则既无卡片也无 pending、又不会再重试 = 放跑）。此处之前的校验全部通过才提交，
    # 免得一次注定被拒的 stop 先建了卡。
    kanban_backlog_submission: dict[str, Any] | None = None
    if kanban_backlog_findings:
        try:
            kanban_backlog_submission = _submit_kanban_backlog_findings_from_attempt(
                repo=repo, run_dir=run_dir, attempt=attempt, findings=kanban_backlog_findings, validate_only=validate_only
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都不得静默成终态
            print(
                _render_kanban_backlog_submission_failure_with_retry_command(
                    args=args, exc=exc, status=status, worktree=worktree, run_dir=run_dir
                ),
                file=sys.stderr,
            )
            return FIX_ATTEMPT_STOP_EXIT_CODE_KANBAN_BACKLOG_SUBMISSION_FAILED_ATTEMPT_STILL_RETRYABLE
    fix_patch.write_text(patch_text, encoding="utf-8")
    changed = _changed_paths(worktree, patch_paths)
    baseline_commit = str(attempt.get("baseline_commit") or "HEAD")
    baseline_git_blob_id_by_expansion_path = {
        path: _blob_id_at_commit(worktree, baseline_commit, path)
        for path in [*scope_expansion["expanded_paths"], *scope_expansion["acknowledged_protected_or_background_paths"]]
    }
    warnings: list[str] = []
    if status in {"false_positive", "elevated", "failed"} and all_changed:
        warnings.append(
            f"attempt stopped as {status} but modified files; these changes are not applied: "
            + ", ".join(item["path"] for item in all_changed)
        )
    if scope_expansion["expanded_paths"]:
        warnings.append("declared allowlist-external changes: " + ", ".join(scope_expansion["expanded_paths"]))
    result = {
        "schema_version": 1,
        "attempt_id": args.attempt_id,
        "issue_id": attempt["issue_id"],
        "run_id": attempt["run_id"],
        "status": status,
        "fix_issue_handling_mode": handling_mode,
        "fix_patch_path": str(fix_patch),
        "changed_paths": changed,
        "all_changed_paths": all_changed,
        "patch_paths": patch_paths,
        "scope_expansion": scope_expansion,
        "baseline_git_blob_id_by_expansion_path": baseline_git_blob_id_by_expansion_path,
        "requires_main_agent_confirmation_before_apply": bool(
            scope_expansion["acknowledged_protected_or_background_paths"]
        ),
        "warnings": warnings,
        "result_payload": result_payload,
        "kanban_backlog_submission": kanban_backlog_submission,
    }
    result_path = _attempt_dir(run_dir, args.attempt_id) / "result.json"
    _write_json(result_path, result)
    attempt.update(
        {
            "status": status,
            "fix_patch_path": str(fix_patch),
            "changed_paths": changed,
            "all_changed_paths": all_changed,
            "patch_paths": patch_paths,
            "scope_expansion": scope_expansion,
            "baseline_git_blob_id_by_expansion_path": baseline_git_blob_id_by_expansion_path,
            "requires_main_agent_confirmation_before_apply": result["requires_main_agent_confirmation_before_apply"],
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
        state=ISSUE_STATE_BY_FIX_ATTEMPT_STOP_STATUS.get(status, status),
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


def _submit_kanban_backlog_findings_from_attempt(
    *, repo: Path, run_dir: Path, attempt: dict[str, Any], findings: list[dict[str, Any]], validate_only: bool
) -> dict[str, Any]:
    """Kanban 不可用已在提交模块里转成持久 pending（不抛）；本地落盘失败照常抛出，由 command_stop 拒绝收尾。"""
    import rvf_out_of_scope_findings_kanban_backlog_submission as backlog_submission  # noqa: PLC0415

    return backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo,
        findings=findings,
        finding_source=(
            backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR
            if validate_only
            else backlog_submission.FINDING_SOURCE_VALIDATE_THEN_FIX_ATTEMPT_FIXER
        ),
        run_dir=run_dir,
        run_id=str(attempt.get("run_id") or ""),
        attempt_id=str(attempt.get("attempt_id") or ""),
        issue_id=str(attempt.get("issue_id") or ""),
    )


def _render_kanban_backlog_submission_failure_with_retry_command(
    *, args: argparse.Namespace, exc: BaseException, status: str, worktree: Path, run_dir: Path
) -> str:
    """失败说明 + 可照抄的整行重试命令（自带 cd：子代理每次 Bash 调用都会重置 cwd）。"""
    command = ["rvf", "fix", "stop", "--attempt-id", args.attempt_id, "--run-dir", str(run_dir), "--status", status]
    if args.repo:
        command.extend(["--repo", args.repo])
    command.extend(
        ["--kanban-backlog-finding-file", str(Path(args.kanban_backlog_finding_file).expanduser().resolve())]
    )
    if args.result_file:
        command.extend(["--result-file", str(Path(args.result_file).expanduser().resolve())])
    for path in args.scope_expansion_path or []:
        command.extend(["--scope-expansion-path", path])
    for path in args.acknowledged_protected_or_background_path_modification or []:
        command.extend(["--acknowledged-protected-or-background-path-modification", path])
    if args.scope_expansion_reason:
        command.extend(["--scope-expansion-reason", args.scope_expansion_reason])
    if args.log_root:
        command.extend(["--log-root", args.log_root])
    return (
        f"范围外发现提交 Kanban backlog 失败，没能为每条发现留下持久记录（卡片 / pending）：{type(exc).__name__}: {exc}\n"
        f"attempt {args.attempt_id} 未收尾：没有写 fix.patch / result.json / attempt 状态 / tracker 终态，仍可重试；"
        "已落下的 pending / ledger 记录会在重跑时先被重试、并按指纹去重。\n"
        "排除原因后照抄重跑：\n"
        f"cd {shlex.quote(str(worktree))} && {shlex.join(command)}"
    )


FIX_ATTEMPT_APPLY_STATUS_APPLIED = "applied"
FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT = "merge_conflict"
FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT = "scope_expansion_conflict"
FIX_ATTEMPT_APPLY_STATUS_REQUIRES_MAIN_AGENT_CONFIRMATION = (
    "requires_main_agent_confirmation_for_acknowledged_protected_or_background_path_modification"
)
# 只有 fixed（以及它在 apply 时落成的冲突 / 待确认中间态）可以合回；误报 / 升级 / 失败 / 已提交
# backlog 的 attempt 即使留下了改动也不得合回（历史缺陷：``--attempt-id`` 点名时不查状态）。
FIX_ATTEMPT_STATUSES_ELIGIBLE_FOR_APPLY = frozenset(
    {
        "fixed",
        FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT,
        FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT,
        FIX_ATTEMPT_APPLY_STATUS_REQUIRES_MAIN_AGENT_CONFIRMATION,
    }
)


def _ownership_classification_for_acknowledged_path(path: str, scope_contract: dict[str, Any]) -> str:
    background = _scope_list(scope_contract, "protected_files") + _scope_list(scope_contract, "background_files")
    if path in set(background) or _prefix_matches(path, background):
        return "其他会话或归属不明的未提交改动（background / protected）"
    if _prefix_matches(path, _scope_list(scope_contract, "excluded_path_prefixes")):
        return "用户声明的排除区（excluded_path_prefixes）"
    return "合同未登记"


def apply_fix_attempt(
    *,
    run_dir: Path,
    attempt_id: str,
    target_repo_override: str | None = None,
    log_root: Path | None = None,
    confirm_acknowledged_protected_or_background_path_modification: bool = False,
) -> dict[str, Any]:
    """把 attempt 的 ``fix.patch`` 合回主 RVF worktree，返回 ``{attempt_id, status, stderr, ...}``。

    冲突判据按内容漂移：stop 时记录了每条外扩 / 已确认越界路径在 attempt baseline 里的 blob；主工作区
    该路径当前 blob 与之不同，说明 prepare 之后有人改过它，合回会覆盖并发改动 ⇒ ``scope_expansion_conflict``。
    （attempt 已物化全部 WIP，所以 prepare 前就脏、之后没再变的路径可以正常合回。）"""
    attempt = _load_attempt(run_dir, attempt_id)
    current_status = attempt.get("status")
    if current_status not in FIX_ATTEMPT_STATUSES_ELIGIBLE_FOR_APPLY:
        raise ValueError(
            f"attempt {attempt_id} has status {current_status!r}; only fixed attempts are applied "
            "(false_positive / elevated / failed / validated_and_submitted_to_kanban_backlog never are)"
        )
    repo = _optional_repo(target_repo_override, attempt)
    fix_patch = Path(attempt.get("fix_patch_path") or _attempt_dir(run_dir, attempt_id) / "fix.patch").resolve()
    if not fix_patch.is_file():
        raise ValueError(f"fix patch not found: {fix_patch}")
    scope_expansion = attempt.get("scope_expansion") if isinstance(attempt.get("scope_expansion"), dict) else {}
    expanded_paths = _safe_relative_paths(scope_expansion.get("expanded_paths", []))
    acknowledged_paths = _safe_relative_paths(scope_expansion.get("acknowledged_protected_or_background_paths", []))
    recorded_blobs = attempt.get("baseline_git_blob_id_by_expansion_path")
    guarded_paths = [*expanded_paths, *acknowledged_paths]
    if isinstance(recorded_blobs, dict):
        drifted_paths = [
            path for path in guarded_paths if _current_working_tree_blob_id(repo, path) != recorded_blobs.get(path)
        ]
    else:
        drifted_paths = _dirty_paths(repo, guarded_paths) if guarded_paths else []
    confirmation_checks: list[dict[str, Any]] = []
    if drifted_paths:
        apply_result = subprocess.CompletedProcess(
            args=["git", "apply", "--whitespace=nowarn", str(fix_patch)],
            returncode=4,
            stdout="",
            stderr="scope expansion paths changed in target repo since the attempt baseline: " + ", ".join(drifted_paths),
        )
        status = FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT
    elif acknowledged_paths and not confirm_acknowledged_protected_or_background_path_modification:
        scope_contract = _scope_contract(run_dir)
        confirmation_checks = [
            {
                "path": path,
                "unchanged_since_attempt_baseline": True,
                "ownership": _ownership_classification_for_acknowledged_path(path, scope_contract),
            }
            for path in acknowledged_paths
        ]
        apply_result = subprocess.CompletedProcess(
            args=["git", "apply", "--whitespace=nowarn", str(fix_patch)],
            returncode=5,
            stdout="",
            stderr=(
                "attempt modifies protected / background / excluded paths acknowledged by the fixer: "
                + ", ".join(acknowledged_paths)
                + f"; reason: {scope_expansion.get('reason') or '（未写）'}. Verify, then rerun with "
                "--confirm-acknowledged-protected-or-background-path-modification"
            ),
        )
        status = FIX_ATTEMPT_APPLY_STATUS_REQUIRES_MAIN_AGENT_CONFIRMATION
    else:
        apply_result = _run(repo, ["apply", "--whitespace=nowarn", str(fix_patch)], check=False)
        status = (
            FIX_ATTEMPT_APPLY_STATUS_APPLIED
            if apply_result.returncode == 0
            else FIX_ATTEMPT_APPLY_STATUS_MERGE_CONFLICT
        )
    attempt["status"] = status
    if status == FIX_ATTEMPT_APPLY_STATUS_APPLIED and acknowledged_paths:
        attempt["confirmed_acknowledged_protected_or_background_paths"] = acknowledged_paths
    _write_json(_attempt_dir(run_dir, attempt_id) / "attempt.json", attempt)
    if status != FIX_ATTEMPT_APPLY_STATUS_REQUIRES_MAIN_AGENT_CONFIRMATION:
        # 待确认是 apply 内部的中间态：tracker 里该 attempt 仍是 fixed，不记账。
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
                "confirmed_acknowledged_protected_or_background_paths": (
                    acknowledged_paths if status == FIX_ATTEMPT_APPLY_STATUS_APPLIED else []
                ),
            },
            log_root_override=log_root,
        )
    return {
        "attempt_id": attempt_id,
        "status": status,
        "stderr": apply_result.stderr,
        "confirmation_checks": confirmation_checks,
    }


def command_apply(args: argparse.Namespace) -> int:
    payload = apply_fix_attempt(
        run_dir=_required_path(args.run_dir, "RVF_RUN_DIR", "--run-dir"),
        attempt_id=args.attempt_id,
        target_repo_override=args.target_repo,
        log_root=_log_root_arg(args.log_root),
        confirm_acknowledged_protected_or_background_path_modification=(
            args.confirm_acknowledged_protected_or_background_path_modification
        ),
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    status = payload["status"]
    if status == FIX_ATTEMPT_APPLY_STATUS_APPLIED:
        return 0
    if status == FIX_ATTEMPT_APPLY_STATUS_SCOPE_EXPANSION_CONFLICT:
        return 4
    if status == FIX_ATTEMPT_APPLY_STATUS_REQUIRES_MAIN_AGENT_CONFIRMATION:
        return 5
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
        choices=FIX_ATTEMPT_STOP_STATUSES,
        default="auto",
        help=(
            "validate-only attempt（范围外问题）只能用 false_positive / validated_and_submitted_to_kanban_backlog / "
            "elevated / failed，且不得改任何文件。"
        ),
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
        help=(
            "Required with --status fixed when allowlist-external or acknowledged protected/background paths are "
            "included in the patch."
        ),
    )
    stop.add_argument(
        "--acknowledged-protected-or-background-path-modification",
        action="append",
        default=[],
        help=(
            "显式确认本次最小修复必须改动的 protected / background / 用户声明 excluded 路径（其他会话的未提交改动"
            "或排除区）。可重复。stop 接受后，apply 前还需主会话核对并确认。内置工具托管前缀（.serena/）不在此列。"
        ),
    )
    stop.add_argument(
        "--kanban-backlog-finding-file",
        default=None,
        help=(
            "JSON：{\"findings\": [{title, path, line?, severity?, problem_statement, evidence?, group_id?}]}。"
            "validate-only attempt 以 validated_and_submitted_to_kanban_backlog 收尾时必填；其他 attempt 修复途中"
            "发现的、与本轮无关的真实问题也可附带提交（不当场修）。"
        ),
    )
    stop.add_argument("--log-root", default=None)
    stop.set_defaults(func=command_stop)

    apply = sub.add_parser("apply")
    apply.add_argument("--attempt-id", required=True)
    apply.add_argument("--target-repo", default=None)
    apply.add_argument("--run-dir", default=None)
    apply.add_argument("--log-root", default=None)
    apply.add_argument(
        "--confirm-acknowledged-protected-or-background-path-modification",
        action="store_true",
        help="主会话核对 fixer 已确认的越界路径（内容自 baseline 未变、归属可接受）后放行合回。",
    )
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
