"""已提交候选的中性证据契约；Kanban 私有状态只由适配层读取。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping


COMMITTED_CANDIDATE_SCOPE_EVIDENCE_SCHEMA = "rvf-committed-candidate-scope-evidence/v1"


def git_read(repo: Path, *arguments: str, input_text: str | None = None) -> str:
    environment = {key: value for key, value in os.environ.items() if key not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
    }}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo,
        input=input_text, capture_output=True, text=True, check=False, timeout=20, env=environment,
    )
    if result.returncode:
        raise ValueError("提交证据的 Git 核验失败：" + " ".join(arguments[:2]))
    return result.stdout.strip()


def commit_is_ancestor(repo: Path, first: str, second: str) -> bool:
    try:
        git_read(repo, "merge-base", "--is-ancestor", first, second)
        return True
    except ValueError:
        return False


def scope_evidence_digest(evidence: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_committed_candidate_scope_evidence(
    repo: Path, evidence: Mapping[str, Any], *, task_id: str | None = None,
) -> None:
    """冻结输入仍须核验身份与可达性；绝不重新读取变化中的 live base。"""
    if evidence.get("schema") != COMMITTED_CANDIDATE_SCOPE_EVIDENCE_SCHEMA:
        raise ValueError("已提交候选证据 schema 不匹配")
    if (Path(str(evidence.get("worktree") or "")).resolve() != repo.resolve()
            or (task_id is not None and evidence.get("task_id") != task_id)
            or evidence.get("git_common_directory") != git_read(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        raise ValueError("已提交候选证据的仓库、task 或 HEAD 不匹配")
    if evidence.get("status") == "unborn_head":
        branch = git_read(repo, "symbolic-ref", "--quiet", "HEAD")
        if branch != evidence.get("unborn_head_ref") or git_read(repo, "for-each-ref", "--format=%(refname)", branch):
            raise ValueError("此前没有首个提交的分支已经改变")
        return
    if evidence.get("head_commit") != git_read(repo, "rev-parse", "HEAD"):
        raise ValueError("已提交候选证据的 HEAD 不匹配")
    if evidence.get("status") != "verified":
        return
    initial = evidence.get("initial_base_commit")
    if not isinstance(initial, str) or not commit_is_ancestor(repo, initial, "HEAD"):
        raise ValueError("已提交候选证据的稳定起点不可达")
    base_tip = evidence.get("base_tip_commit")
    if not isinstance(base_tip, str) or git_read(repo, "rev-parse", "--verify", base_tip + "^{commit}") != base_tip:
        raise ValueError("已提交候选证据没有固定 base SHA")
    eligible = set(git_read(repo, "rev-list", "--no-merges", f"{initial}..HEAD").splitlines())
    for commit in evidence.get("task_owned_commit_shas", []):
        if not isinstance(commit, str) or commit not in eligible:
            raise ValueError("自有提交不在稳定窗口内，或不是非 merge 提交")


def task_owned_commits_in_review_window(
    repo: Path, baseline: str, evidence: Mapping[str, Any] | None,
) -> list[str]:
    if not evidence or evidence.get("status") != "verified":
        return []
    validate_committed_candidate_scope_evidence(repo, evidence)
    window = set(git_read(repo, "rev-list", f"{baseline}..HEAD").splitlines())
    return [commit for commit in evidence["task_owned_commit_shas"] if commit in window]
