"""用独立 Git index 冻结显式任务范围，不修改主工作树或其 index。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import time
from pathlib import Path, PurePosixPath

from .persistent_artifacts import git, git_text, repository_identity, write_bytes, write_json


def normalized_scope_paths(scope_paths: list[str]) -> list[str]:
    if not scope_paths:
        raise ValueError("必须显式提供任务范围 --scope")
    normalized = []
    for value in scope_paths:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ValueError(f"任务范围必须是仓库内的普通相对路径：{value}")
        normalized.append(str(path))
    return sorted(set(normalized))


def capture_worktree_tree(repo: Path, scope_paths: list[str], artifacts: Path, *, head: str | None = None,
                          excluded_preparation_paths: list[str] | None = None) -> str:
    repo = repo.resolve()
    capture_index = artifacts / f"capture-{uuid.uuid4().hex}.index"
    environment = {"GIT_INDEX_FILE": str(capture_index)}
    try:
        base = head or "HEAD"
        git(repo, "read-tree", base, environment=environment)
        explicit_files = []
        ordinary_paths = []
        for scope_path in scope_paths:
            candidate = repo / scope_path
            tree_entry = git(repo, "ls-tree", base, "--", scope_path)
            tracked_file = tree_entry and not any(line.split()[1:2] == [b"tree"] for line in tree_entry.splitlines())
            if candidate.is_dir() and not candidate.is_symlink():
                ordinary_paths.append(scope_path)
            elif candidate.is_file() or candidate.is_symlink() or tracked_file:
                explicit_files.append(scope_path)
            elif tree_entry:
                ordinary_paths.append(scope_path)
        if ordinary_paths:
            git(repo, "add", "-A", "--", *ordinary_paths, environment=environment)
        if explicit_files:
            git(repo, "add", "-A", "-f", "--", *explicit_files, environment=environment)
        if excluded_preparation_paths:
            git(repo, "update-index", "--force-remove", "--", *excluded_preparation_paths, environment=environment)
        return git_text(repo, "write-tree", environment=environment)
    finally:
        capture_index.unlink(missing_ok=True)
        capture_index.with_suffix(".index.lock").unlink(missing_ok=True)


def create_snapshot(repo: Path, *, base_ref: str, scope_paths: list[str], requirements: bytes, run_directory: Path,
                    reviewer_count: int = 2, starting_point_evidence: dict | None = None,
                    execution_plan: dict | None = None) -> dict:
    from .execution_lifecycle import normalized_execution_plan, runtime_environment_identity
    plan = normalized_execution_plan(execution_plan) if execution_plan is not None else None
    if type(reviewer_count) is not int or reviewer_count < 2:
        raise ValueError("reviewer 数量必须是至少为 2 的整数")
    scope_paths = normalized_scope_paths(scope_paths)
    identity = repository_identity(repo)
    repo = Path(identity["repo"])
    run_directory = run_directory.expanduser().resolve()
    if run_directory == repo or repo in run_directory.parents:
        raise ValueError("run 目录必须位于目标工作树之外，避免将运行材料纳入任务快照")
    run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    baseline = git_text(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}")
    original_head = git_text(repo, "rev-parse", "HEAD")
    observed_worktree_tree = capture_worktree_tree(repo, ["."], run_directory, head=original_head)
    if starting_point_evidence is not None:
        if (starting_point_evidence.get('status') != 'verified'
                or starting_point_evidence.get('base_commit') != baseline
                or starting_point_evidence.get('observed_head') != original_head
                or starting_point_evidence.get('scope_paths') != scope_paths
                or any(starting_point_evidence.get('binding', {}).get(key) != value for key, value in identity.items())):
            raise ValueError('起点证据与当前任务快照不一致；重新核验来源后创建快照')
    candidate_tree = capture_worktree_tree(repo, scope_paths, run_directory, head=original_head)
    if capture_worktree_tree(repo, scope_paths, run_directory, head=original_head) != candidate_tree or git_text(repo, "rev-parse", "HEAD") != original_head:
        raise RuntimeError("快照期间任务文件或 HEAD 发生变化，请在写入稳定后重新创建快照")
    candidate_commit = git_text(repo, "commit-tree", candidate_tree, "-p", original_head,
        input_bytes=b"RVF fixed task snapshot\n", environment={
            "GIT_AUTHOR_NAME": "RVF Snapshot", "GIT_AUTHOR_EMAIL": "snapshot@rvf.local",
            "GIT_COMMITTER_NAME": "RVF Snapshot", "GIT_COMMITTER_EMAIL": "snapshot@rvf.local",
        })
    run_id = run_directory.name
    snapshot_reference = f"refs/rvf-task-snapshots/{uuid.uuid4().hex}"
    git(repo, "update-ref", snapshot_reference, candidate_commit)
    observation_commit = git_text(repo, "commit-tree", observed_worktree_tree, "-p", original_head,
        input_bytes=b"RVF original workspace observation\n", environment={
            "GIT_AUTHOR_NAME": "RVF Snapshot", "GIT_AUTHOR_EMAIL": "snapshot@rvf.local",
            "GIT_COMMITTER_NAME": "RVF Snapshot", "GIT_COMMITTER_EMAIL": "snapshot@rvf.local"})
    git(repo, "update-ref", snapshot_reference + "-workspace-observation", observation_commit)
    patch = git(repo, "diff", "--binary", "--full-index", "--no-ext-diff", baseline, candidate_commit, "--", *scope_paths)
    write_bytes(run_directory / "requirements.md", requirements)
    write_bytes(run_directory / "task-changes.patch", patch)
    changed_paths = git(repo, "diff", "--name-only", "-z", baseline, candidate_commit, "--", *scope_paths).decode().split("\0")
    record = {
        "schema_version": 1, "run_id": run_id, **identity,
        "base_commit": baseline, "original_head": original_head,
        "candidate_commit": candidate_commit, "snapshot_reference": snapshot_reference,
        "original_worktree_observation_commit": observation_commit,
        "scope_paths": scope_paths, "changed_paths": [path for path in changed_paths if path],
        "reviewer_count": reviewer_count,
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
        "patch_sha256": hashlib.sha256(patch).hexdigest(), "status": "review_pending",
        "created_at": time.time(), "runtime_environment": runtime_environment_identity(),
    }
    source_root = Path(__file__).resolve().parents[2]
    manifest = source_root / "plugins/review-validate-fix/.claude-plugin/plugin.json"
    if not manifest.exists():
        manifest = source_root / ".claude-plugin/plugin.json"
    source_paths = [*Path(__file__).parent.glob("*.py"), *manifest.parent.parent.glob("skills/review-validate-fix/prompts/*.md")]
    record["source_version"] = {"plugin_version": json.loads(manifest.read_text()).get("version") if manifest.exists() else None,
        "source_files_sha256": {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(source_paths)},
        "git_commit": None}
    try:
        record["source_version"]["git_commit"] = git_text(source_root, "rev-parse", "HEAD")
    except RuntimeError:
        pass
    if plan is not None:
        write_json(run_directory / "execution-plan.json", plan)
        record["execution_plan_sha256"] = hashlib.sha256((run_directory / "execution-plan.json").read_bytes()).hexdigest()
        record["semantic_repair_rounds_used"] = 0
    if starting_point_evidence is not None:
        record['starting_point_evidence'] = json.loads(json.dumps(starting_point_evidence))
    write_json(run_directory / "run.json", record)
    packet = (
        "# 固定任务评审输入\n\n"
        f"基线：`{baseline}`\n候选：`{candidate_commit}`\n\n"
        "任务范围：\n" + "\n".join(f"- `{path}`" for path in scope_paths) + "\n\n"
        "需求原文读取同目录 `requirements.md`，完整任务改动读取同目录 `task-changes.patch`。"
        "当前目录是候选的独立工作树。超大文件、二进制及生成物可用 Git 的基线/候选对象按需读取，"
        "不截断正式范围；需求与 diff 不在其他输入中重复内联。\n"
    )
    write_bytes(run_directory / "review-input.md", packet.encode())
    record["review_input_sha256"] = hashlib.sha256(packet.encode()).hexdigest()
    write_json(run_directory / "run.json", record)
    from .active_run_observation import register_exact_task_run
    register_exact_task_run(run_directory, record)
    return record
