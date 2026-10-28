"""按精确工作树内容复用活动 run，外部新 hunk 仍形成新的发起决策。"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, repository_identity, write_json
from .task_snapshot import capture_worktree_tree


def _registry_path(identity: dict, task_id: str) -> Path:
    key = hashlib.sha256((identity["git_directory"] + "\0" + task_id).encode()).hexdigest()
    return resolved_rvf_user_local_data_paths().runtime_state_root / "task-active-review-runs" / f"{key}.json"


def register_exact_task_run(run_directory: Path, record: dict) -> None:
    task_id = os.environ.get("KANBAN_TASK_ID") or os.environ.get("CLINE_KANBAN_TASK_ID")
    if not task_id:
        return
    registry = _registry_path(record, task_id)
    with exclusive_lock(registry.with_suffix(".lock")):
        runs = read_json(registry).get("run_directories", []) if registry.exists() else []
        if str(run_directory) not in runs:
            write_json(registry, {"task_id": task_id, "git_directory": record["git_directory"],
                                  "run_directories": [*runs, str(run_directory)]})


def workspace_matches_registered_run(repo: Path, task_id: str) -> bool:
    identity = repository_identity(repo)
    registry = _registry_path(identity, task_id)
    if not registry.exists():
        return False
    for run_path in read_json(registry).get("run_directories", []):
        run = Path(run_path)
        try:
            record = read_json(run / "run.json")
            if any(record.get(key) != value for key, value in identity.items()) or not record.get("original_worktree_observation_commit"):
                continue
            # 已提交工作是否完整覆盖仍由 Kanban 起点证据判断，不能把新提交当成普通依赖前移。
            from .execution_lifecycle import execution_is_revoked
            revoked = execution_is_revoked(run)
            if record.get("status") == "completed" or (not revoked and not git(repo, "status", "--porcelain", "--untracked-files=all")):
                continue
            head = git_text(repo, "rev-parse", "HEAD")
            if head != record["original_head"] and record.get("starting_point_evidence"):
                from adapters.cline_kanban.task_review_starting_point import resolve_task_review_starting_point
                current_task = resolve_task_review_starting_point(repo, task_id)
                if current_task.get("status") != "verified":
                    continue
                if set(current_task.get("task_owned_commit_shas", [])) - set(record["starting_point_evidence"].get("task_owned_commit_shas", [])):
                    continue
            merged = git_text(repo, "merge-tree", "--write-tree", f"--merge-base={record['original_head']}",
                              record["original_worktree_observation_commit"], head).splitlines()[0]
            candidate = record.get("validated_candidate_commit", record["candidate_commit"])
            candidate_base = record.get("candidate_base_head", record["original_head"])
            if candidate_base != head:
                candidate = git_text(repo, "merge-tree", "--write-tree", f"--merge-base={candidate_base}", candidate, head).splitlines()[0]
            scope = record.get("effective_scope_paths", record["scope_paths"])
            index = run / f"hook-content-observation-{uuid.uuid4().hex}.index"
            try:
                environment = {"GIT_INDEX_FILE": str(index)}
                git(repo, "read-tree", merged, environment=environment)
                overlay = git(repo, "diff", "--binary", "--full-index", merged, candidate, "--", *scope)
                if overlay:
                    git(repo, "apply", "--cached", "-", input_bytes=overlay, environment=environment)
                expected = git_text(repo, "write-tree", environment=environment)
                if capture_worktree_tree(repo, ["."], run, head=head) == expected:
                    return True
            finally:
                index.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError, KeyError):
            continue
    return False
