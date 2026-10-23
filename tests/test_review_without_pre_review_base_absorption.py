"""RVF 在固定候选上开审，base 整合由后续同步负责。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))
from independent_review_start_boundary_fixture import environment_with_synthetic_independent_review_start_boundary


def git(repo, *arguments):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("local_base_branch_name", ["main", "integration-base", "master"])
def test_prepare_reviews_detached_candidate_without_absorbing_advanced_base(tmp_path, committed, local_base_branch_name):
    base = tmp_path / "base-repository"
    base.mkdir()
    git(base, "init", "-q", "-b", local_base_branch_name)
    git(base, "config", "user.name", "隔离开审测试")
    git(base, "config", "user.email", "review@example.invalid")
    (base / "implementation.py").write_text("value = 1\n")
    git(base, "add", ".")
    git(base, "commit", "-qm", "test: 初始实现")
    shared_ancestor = git(base, "rev-parse", "HEAD")
    task = tmp_path / "task-worktree"
    git(base, "worktree", "add", "--detach", str(task), shared_ancestor)
    (base / "AGENTS.md").write_text("RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n")
    git(base, "add", ".")
    git(base, "commit", "-qm", "test: 推进 base")
    base_tip = git(base, "rev-parse", "HEAD")
    (task / "implementation.py").write_text("value = 2\n")
    if committed:
        git(task, "add", ".")
        git(task, "commit", "-qm", "test: 任务实现")
    head = git(task, "rev-parse", "HEAD")
    context = tmp_path / "scope-of-work.md"
    context.write_text("## 用户需求\n审查当前任务实现。\n")
    environment_overrides = dict(os.environ)
    if local_base_branch_name == "master":
        environment_overrides.pop("CODEX_RVF_COMMITTED_ROUND_BASE_REF", None)
        git(base, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    else:
        environment_overrides["CODEX_RVF_COMMITTED_ROUND_BASE_REF"] = local_base_branch_name
    environment = environment_with_synthetic_independent_review_start_boundary(repo=task,
        environment=environment_overrides)
    git(base, "commit", "--allow-empty", "-qm", "test: 分界之后再次推进 base")
    base_tip = git(base, "rev-parse", "HEAD")
    command = [sys.executable, str(SCRIPTS / "prepare_review_run.py"), "--repo", str(task),
               "--session-context", str(context), "--primary-file", "implementation.py"]
    if committed:
        command.extend(["--base-ref", shared_ancestor, "--transcript", json.loads(Path(environment["RVF_REVIEW_START_BOUNDARY_RECEIPT"]).read_text())["transcript_path"]])
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    prepared = json.loads(result.stdout)
    contract = json.loads(Path(prepared["scope_contract"]).read_text())
    assert contract["base_ref"] == local_base_branch_name
    assert contract["comparison_base_tip"] == shared_ancestor
    assert contract["reviewed_base_tip"] == shared_ancestor
    assert contract["reviewed_task_tip"] == head
    assert contract["candidate_task_tip"] == head
    assert "bootstrap_receipt_path" not in contract
    metadata = json.loads(Path(prepared["review_packet_metadata"]).read_text())
    assert metadata["owned_diff_base_ref"] == shared_ancestor
    if committed:
        manifest = json.loads(Path(prepared["session_manifest_file"]).read_text())
        assert manifest["committed_round_baseline"] == shared_ancestor
    assert git(task, "rev-parse", "HEAD") == head
    assert git(base, "rev-parse", local_base_branch_name) == base_tip
    assert "value = 2" in Path(prepared["review_packet"]).read_text()
