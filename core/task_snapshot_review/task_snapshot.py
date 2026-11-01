"""用独立 Git index 冻结显式任务范围，不修改主工作树或其 index。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .persistent_artifacts import git, git_text, repository_identity, write_bytes, write_json


TEMPORARY_MANDATORY_CODEX_REVIEWER_DEADLINE_AT = datetime(2026, 11, 2, 5, tzinfo=timezone.utc).timestamp()


def reviewer_allocation_for_new_run(reviewer_count: int) -> tuple[int, list[str]]:
    if type(reviewer_count) is not int or reviewer_count < 2:
        raise ValueError("reviewer 数量必须是至少为 2 的整数")
    if time.time() < TEMPORARY_MANDATORY_CODEX_REVIEWER_DEADLINE_AT:
        reviewer_count = max(3, reviewer_count)
        return reviewer_count, ["non_codex_external_cli"] * (reviewer_count - 1) + ["codex_external_cli"]
    return reviewer_count, ["any_external_cli"] * reviewer_count


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
                    decision_fact_starting_point_evidence: dict | None = None,
                    execution_plan: dict | None = None, reviewer_harness_constraints: list[str] | None = None) -> dict:
    from .execution_lifecycle import normalized_execution_plan, runtime_environment_identity
    plan = normalized_execution_plan(execution_plan) if execution_plan is not None else None
    if type(reviewer_count) is not int or reviewer_count < 2:
        raise ValueError("reviewer 数量必须是至少为 2 的整数")
    if reviewer_harness_constraints is not None and (len(reviewer_harness_constraints) != reviewer_count
            or any(constraint not in {"any_external_cli", "non_codex_external_cli", "codex_external_cli"}
                   for constraint in reviewer_harness_constraints)):
        raise ValueError("reviewer harness 约束必须逐席对应数量并使用已知的外部 CLI 约束")
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
    def ignored_files_forced_only_into_candidate() -> set[str]:
        # 完整目录观察遵守 gitignore；显式文件会用 -f 强制纳入未跟踪的忽略文件。
        # 这种差异是捕获规则本身，不是快照期间的内容竞争。已跟踪文件两边都会收录，不能排除。
        explicit_files = []
        for scope_path in scope_paths:
            candidate = repo / scope_path
            if candidate.is_dir() and not candidate.is_symlink():
                continue
            tree_entry = git(repo, "ls-tree", original_head, "--", scope_path)
            tracked_file = bool(tree_entry) and not any(line.split()[1:2] == [b"tree"] for line in tree_entry.splitlines())
            if candidate.is_file() or candidate.is_symlink() or tracked_file:
                explicit_files.append(scope_path)
        if not explicit_files:
            return set()
        # ls-files 与 git add 使用同一套排除规则；未跟踪的忽略文件只出现在显式 -f 捕获中。
        ignored = git(repo, "ls-files", "-o", "-i", "--exclude-standard", "-z", "--", *explicit_files)
        return {token.decode() for token in ignored.split(b"\0") if token}
    stable_ignored_additions = None
    def scope_content_differs(left: str, right: str) -> bool:
        nonlocal stable_ignored_additions
        difference = git(repo, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", left, right, "--", *scope_paths)
        paths = [token.decode() for token in difference.split(b"\0") if token]
        if not paths:
            return False
        if stable_ignored_additions is None:
            stable_ignored_additions = ignored_files_forced_only_into_candidate()
        return any(path not in stable_ignored_additions for path in paths)
    # 完整观察若早于已稳定的候选，改用与候选一致的冻结观察；覆盖不能沿用变化前的旧树。
    if scope_content_differs(observed_worktree_tree, candidate_tree):
        observed_worktree_tree = capture_worktree_tree(repo, ["."], run_directory, head=original_head)
        if git_text(repo, "rev-parse", "HEAD") != original_head or scope_content_differs(observed_worktree_tree, candidate_tree):
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
        "reviewer_harness_constraints": reviewer_harness_constraints or ["any_external_cli"] * reviewer_count,
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
    from .task_review_decision_facts import RECOVERY_CONDITION, collect_task_review_decision_facts
    fact_evidence = starting_point_evidence or decision_fact_starting_point_evidence
    try:
        decision_facts = collect_task_review_decision_facts(repo,
            task_id=(fact_evidence or {}).get('binding', {}).get('task_id')
                or os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID'),
            starting_point_evidence=fact_evidence, snapshot_record=record,
            observed_worktree_tree=observed_worktree_tree)
    except OSError as error:
        diagnostic = f'决策事实缓存不可用，正式快照继续：{error}'
        unavailable = {'status': 'unknown', 'diagnostic': diagnostic, 'recovery_condition': RECOVERY_CONDITION,
                       'source': 'snapshot_decision_fact_isolation'}
        decision_facts = {'schema_version': 1, 'observation': {'candidate_commit': candidate_commit,
            'head': original_head, 'worktree_tree': observed_worktree_tree, 'base_commit': baseline,
            'scope_paths': scope_paths},
            'facts': {name: dict(unavailable) for name in (
                'task_identity', 'fixed_start_and_scope', 'workspace_change_statistics',
                'task_commit_history_statistics', 'existing_review_evidence', 'command_guidance')},
            'summary': '共用事实暂不可用，按需核验；不能把缺证解释为无改动或已通过。正式快照已保留候选。'}
    write_json(run_directory / 'decision-facts.json', decision_facts)
    record['decision_facts_sha256'] = hashlib.sha256((run_directory / 'decision-facts.json').read_bytes()).hexdigest()
    write_json(run_directory / "run.json", record)
    packet = (
        "# 固定任务评审输入\n\n"
        f"基线：`{baseline}`\n候选：`{candidate_commit}`\n\n"
        "任务范围：\n" + "\n".join(f"- `{path}`" for path in scope_paths) + "\n\n"
        "需求原文读取同目录 `requirements.md`，完整任务改动读取同目录 `task-changes.patch`。"
        "共用决策事实读取同目录 `decision-facts.json`；它绑定本候选与原始完整工作树，不代替语义评审。"
        "当前目录是候选的独立工作树。超大文件、二进制及生成物可用 Git 的基线/候选对象按需读取，"
        "不截断正式范围；需求与 diff 不在其他输入中重复内联。\n\n" + decision_facts['summary'] + '\n'
    )
    write_bytes(run_directory / "review-input.md", packet.encode())
    record["review_input_sha256"] = hashlib.sha256(packet.encode()).hexdigest()
    write_json(run_directory / "run.json", record)
    from .active_run_observation import register_exact_task_run
    register_exact_task_run(run_directory, record)
    return record
