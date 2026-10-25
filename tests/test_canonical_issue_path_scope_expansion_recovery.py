"""canonical issue 主路径的真实修复、冻结范围扩展与原事件保全回归。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_reviewed_candidate_completion_qualification import source_and_index_snapshot
from test_reviewed_task_candidate_evidence import (
    SCRIPT_DIRECTORY, canonical_bytes, create_completed_run, git, repository, rewrite_contract,
)
from test_rvf_land_single_step_prepare_and_commit import (
    commit, land_environment, prepare, record_validation, run_fix_ledger_script, run_land,
)

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import write_reviewed_task_candidate_evidence as evidence
from reviewed_candidate_completion_facts import actual_fix_patch_changed_paths


ISSUE_PRIMARY_PATH = "web-ui/src/components/board-card-session-activity.ts"
DIRECT_DEPENDENCY_PATHS = [
    "web-ui/src/components/board-card-session-activity.test.ts",
    "web-ui/src/components/detail-panels/agent-terminal-panel.test.tsx",
    "web-ui/src/components/detail-panels/agent-terminal-panel.tsx",
    "web-ui/src/components/task-card-body.visual-density.test.tsx",
]


def prepare_canonical_issue_outside_frozen_scope(
    tmp_path: Path, *, reviewed_dependency: bool = False, primary_contents: str | None = "原有内容\n",
) -> tuple[Path, Path, dict, list[str]]:
    repo = repository(tmp_path / "repo")
    for relative in [ISSUE_PRIMARY_PATH, *DIRECT_DEPENDENCY_PATHS]:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = primary_contents if relative == ISSUE_PRIMARY_PATH else "原有内容\n"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "test: 建立被审工作以外的既有文件")
    run_dir = create_completed_run(tmp_path, repo)
    if reviewed_dependency:
        rewrite_contract(run_dir, primary_files=["tracked.txt", DIRECT_DEPENDENCY_PATHS[0]])
    decisions = tmp_path / "canonical-main-agent-issue-dispositions.json"
    decisions.write_text(json.dumps({"non_reviewer_fix_issue_sources": [
        {"issue_id": "RVF-004", "origin": "main_agent_self_review"},
    ]}))
    run_fix_ledger_script(tmp_path, "rvf_merge_dispositions.py", "write", "--run-dir", str(run_dir),
                          "--decisions-file", str(decisions), "--json")
    issue_file = tmp_path / "canonical-primary-path-issue.json"
    issue_file.write_text(json.dumps({
        "run_id": "rvf-candidate-evidence-test", "issue_id": "RVF-004", "path": ISSUE_PRIMARY_PATH,
        "line": 1, "kind": "REAL", "severity": "medium", "fix_issue_handling_mode": "validate_then_fix",
        "problem_statement": "当前中断状态必须优先于陈旧活动，直接消费组件应保持一致。",
    }, ensure_ascii=False))
    common = ["--run-dir", str(run_dir), "--log-root", str(tmp_path / "isolated-fix-ledger-state")]
    run_fix_ledger_script(tmp_path, "rvf_fix_issue.py", "upsert", "--repo", str(repo),
                          "--issue-file", str(issue_file), *common)
    attempt = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "prepare", "--repo", str(repo),
                                    "--issue-id", "RVF-004", *common)
    run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "start", "--attempt-id", attempt["attempt_id"], *common)
    return repo, run_dir, attempt, common


def apply_canonical_issue_fix(
    tmp_path: Path, *, declare_primary: bool = False, include_dependencies: bool = False,
    recorded_against_attempt_boundary: bool = False,
) -> tuple[Path, Path, dict]:
    repo, run_dir, attempt, common = prepare_canonical_issue_outside_frozen_scope(tmp_path)
    paths = [ISSUE_PRIMARY_PATH, *(DIRECT_DEPENDENCY_PATHS if include_dependencies else [])]
    for relative in paths:
        (Path(attempt["worktree_path"]) / relative).write_text("正确的中断状态\n", encoding="utf-8")
    flags: list[str] = []
    for relative in paths if declare_primary else paths[1:]:
        flags.extend(["--scope-expansion-path", relative])
    if flags:
        flags.extend(["--scope-expansion-reason", "最小修复需要同步直接消费组件及其测试。"])
    stopped = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "stop", "--attempt-id", attempt["attempt_id"],
                                   "--status", "fixed", *flags, *common)
    if recorded_against_attempt_boundary:
        # 等价重放原事件形状：旧 stop 没把 boundary 内的主路径记入 expanded/declared。
        directory = run_dir / "artifacts/fix-attempts" / attempt["attempt_id"]
        for filename in ("attempt.json", "result.json"):
            path = directory / filename
            payload = json.loads(path.read_text())
            metadata = payload["scope_expansion"]
            for field in ("expanded_paths", "declared_paths", "canonical_issue_declared_paths"):
                if field in metadata:
                    metadata[field] = [entry for entry in metadata[field] if entry != ISSUE_PRIMARY_PATH]
            metadata.pop("canonical_issue_declared_paths", None)
            metadata.pop("declared_paths_outside_attempt_boundary", None)
            metadata.pop("requires_explicit_scope_expansion_reason", None)
            payload.get("baseline_git_blob_id_by_expansion_path", {}).pop(ISSUE_PRIMARY_PATH, None)
            path.write_text(json.dumps(payload, ensure_ascii=False))
    applied = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "apply", "--attempt-id", attempt["attempt_id"],
                                   "--target-repo", str(repo), *common)
    assert applied["status"] == "applied"
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n"
    )
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable,
                             "-c", "print('passed')").returncode == 0
    return repo, run_dir, stopped


@pytest.mark.parametrize("declare_primary,include_dependencies", [(False, False), (True, False), (False, True)])
def test_stop_and_formal_land_include_canonical_issue_primary_path_outside_frozen_scope(
    tmp_path: Path, declare_primary: bool, include_dependencies: bool,
) -> None:
    repo, run_dir, stopped = apply_canonical_issue_fix(
        tmp_path, declare_primary=declare_primary, include_dependencies=include_dependencies,
    )
    expected = {ISSUE_PRIMARY_PATH, *(DIRECT_DEPENDENCY_PATHS if include_dependencies else [])}
    assert set(stopped["scope_expansion"]["expanded_paths"]) == expected
    assert stopped["scope_expansion"]["canonical_issue_declared_paths"] == [ISSUE_PRIMARY_PATH]
    assert stopped["scope_expansion"]["reason"].strip()
    frozen = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    receipt = json.loads(Path(payload["scope"]["applied_scope_expansion_receipt_path"]).read_text())
    assert set(receipt["expansions"][0]["paths"]) == expected
    assert canonical_bytes(json.loads((run_dir / "artifacts/inputs/scope.contract.reviewed.json").read_text())) == frozen


@pytest.mark.parametrize("candidate_committed_before_check", [False, True])
def test_formal_land_recovers_old_primary_path_classification_without_rewriting_original_facts(
    tmp_path: Path, candidate_committed_before_check: bool,
) -> None:
    repo, run_dir, stopped = apply_canonical_issue_fix(
        tmp_path, include_dependencies=True, recorded_against_attempt_boundary=True,
    )
    # 重启后的恢复不依赖 fixer 的工作树；仅消费持久 issue、attempt/result 和 patch。
    attempt_directory = run_dir / "artifacts/fix-attempts" / stopped["attempt_id"]
    git(repo, "worktree", "remove", "--force", str(attempt_directory / "worktree"))
    if candidate_committed_before_check:
        git(repo, "add", "--all")
        git(repo, "commit", "-m", "test: 保留先前已完成人工 land 的候选")
    original_paths = [run_dir / "artifacts/fix-issues/RVF-004.json", run_dir / "artifacts/handoff.md",
                      *(attempt_directory / filename for filename in ("attempt.json", "result.json", "fix.patch"))]
    original_bytes = {path: path.read_bytes() for path in original_paths}
    frozen = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    snapshot = source_and_index_snapshot(repo)
    artifacts = {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["recovered_canonical_issue_scope_expansion_paths"] == [ISSUE_PRIMARY_PATH]
    assert source_and_index_snapshot(repo) == snapshot
    assert {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()} == artifacts
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    if candidate_committed_before_check:
        assert json.loads(landed.stdout)["status"] == "nothing_to_commit"
    payload, receipt_path, digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    expansion = json.loads(Path(payload["scope"]["applied_scope_expansion_receipt_path"]).read_text())["expansions"][0]
    assert set(expansion["paths"]) == {ISSUE_PRIMARY_PATH, *DIRECT_DEPENDENCY_PATHS}
    assert ISSUE_PRIMARY_PATH not in expansion["facts"]["result"]["scope_expansion"]["expanded_paths"]
    assert {path: path.read_bytes() for path in original_paths} == original_bytes
    assert canonical_bytes(json.loads((run_dir / "artifacts/inputs/scope.contract.reviewed.json").read_text())) == frozen
    head = git(repo, "rev-parse", "HEAD")
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    repeated = commit(tmp_path, repo, run_dir)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "nothing_to_commit"
    assert json.loads(repeated.stdout)["candidate_evidence"]["status"] == "reused"
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == digest
    assert git(repo, "rev-parse", "HEAD") == head
    assert {path: path.read_bytes() for path in original_paths} == original_bytes


@pytest.mark.parametrize("mutation", [
    "undeclared_dependency", "validate_only", "foreign_run", "missing_patch_path", "patch_without_primary",
    "unprepared_issue_path", "forged_declaration_source", "primary_rename_from_trailer", "primary_self_rename",
])
def test_primary_path_recovery_never_admits_unproven_expansion(tmp_path: Path, mutation: str) -> None:
    repo, run_dir, stopped = apply_canonical_issue_fix(
        tmp_path, include_dependencies=True, recorded_against_attempt_boundary=True,
    )
    directory = run_dir / "artifacts/fix-attempts" / stopped["attempt_id"]
    if mutation == "missing_patch_path":
        (directory / "fix.patch").write_text("不能证明主路径的补丁\n")
    elif mutation in {"patch_without_primary", "primary_rename_from_trailer", "primary_self_rename"}:
        patch = git(repo, "diff", "--binary", "--", DIRECT_DEPENDENCY_PATHS[0]) + "\n"
        if mutation == "primary_rename_from_trailer":
            patch += f"rename from {ISSUE_PRIMARY_PATH}\n"
        elif mutation == "primary_self_rename":
            patch += (
                f"diff --git a/{ISSUE_PRIMARY_PATH} b/{ISSUE_PRIMARY_PATH}\n"
                f"similarity index 100%\nrename from {ISSUE_PRIMARY_PATH}\nrename to {ISSUE_PRIMARY_PATH}\n"
            )
        (directory / "fix.patch").write_text(patch)
    else:
        for filename in ("attempt.json", "result.json"):
            path = directory / filename
            payload = json.loads(path.read_text())
            if mutation == "undeclared_dependency":
                metadata = payload["scope_expansion"]
                metadata["expanded_paths"].remove(DIRECT_DEPENDENCY_PATHS[0])
                metadata["declared_paths"].remove(DIRECT_DEPENDENCY_PATHS[0])
            elif mutation == "validate_only":
                payload["fix_issue_handling_mode"] = "validate_only_then_submit_to_kanban_backlog"
            elif mutation == "unprepared_issue_path":
                payload["issue_paths"] = []
            elif mutation == "forged_declaration_source":
                payload["scope_expansion"]["canonical_issue_declared_paths"] = [DIRECT_DEPENDENCY_PATHS[0]]
            else:
                payload["run_id"] = "foreign-run"
            path.write_text(json.dumps(payload))
    before = source_and_index_snapshot(repo)
    artifact_bytes = {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 28, checked.stdout + checked.stderr
    assert source_and_index_snapshot(repo) == before
    assert {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()} == artifact_bytes
    assert not (run_dir / "artifacts/applied-scope-expansion-qualification-receipts").exists()


@pytest.mark.parametrize("dependency_changed_after_stop", [False, True])
def test_reviewed_dependency_outside_attempt_authority_still_requires_declaration_and_reason(
    tmp_path: Path, dependency_changed_after_stop: bool,
) -> None:
    repo, run_dir, attempt, common = prepare_canonical_issue_outside_frozen_scope(tmp_path, reviewed_dependency=True)
    worktree = Path(attempt["worktree_path"])
    for relative in (ISSUE_PRIMARY_PATH, DIRECT_DEPENDENCY_PATHS[0]):
        (worktree / relative).write_text("正确的中断状态\n", encoding="utf-8")
    command = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_fix_attempt.py"), "stop", "--attempt-id",
               attempt["attempt_id"], "--status", "fixed", *common]
    undeclared = subprocess.run(command, env=land_environment(tmp_path), capture_output=True, text=True)
    assert undeclared.returncode == 2
    assert "undeclared" in undeclared.stderr
    declared = [*command, "--scope-expansion-path", DIRECT_DEPENDENCY_PATHS[0]]
    unexplained = subprocess.run(declared, env=land_environment(tmp_path), capture_output=True, text=True)
    assert unexplained.returncode == 2
    assert "reason" in unexplained.stderr
    stopped = subprocess.run([*declared, "--scope-expansion-reason", "最小修复需要更新已审依赖"],
                             env=land_environment(tmp_path), capture_output=True, text=True)
    assert stopped.returncode == 0, stopped.stderr
    result = json.loads(stopped.stdout)
    assert result["scope_expansion"]["expanded_paths"] == [ISSUE_PRIMARY_PATH]
    assert {entry["path"] for entry in result["changed_paths"]} == {ISSUE_PRIMARY_PATH, DIRECT_DEPENDENCY_PATHS[0]}
    assert set(result["baseline_git_blob_id_by_expansion_path"]) == {ISSUE_PRIMARY_PATH, DIRECT_DEPENDENCY_PATHS[0]}
    assert any(DIRECT_DEPENDENCY_PATHS[0] in warning for warning in result["warnings"])
    if dependency_changed_after_stop:
        (repo / DIRECT_DEPENDENCY_PATHS[0]).write_text("其他会话的新修改\n", encoding="utf-8")
    apply_result = subprocess.run(
        [sys.executable, str(SCRIPT_DIRECTORY / "rvf_fix_attempt.py"), "apply", "--attempt-id", attempt["attempt_id"],
         "--target-repo", str(repo), *common], env=land_environment(tmp_path), capture_output=True, text=True,
    )
    applied = json.loads(apply_result.stdout)
    if dependency_changed_after_stop:
        assert applied["status"] == "scope_expansion_conflict"
        assert (repo / DIRECT_DEPENDENCY_PATHS[0]).read_text() == "其他会话的新修改\n"
        assert (repo / ISSUE_PRIMARY_PATH).read_text() == "原有内容\n"
        return
    assert applied["status"] == "applied"
    assert (repo / DIRECT_DEPENDENCY_PATHS[0]).read_text() == "正确的中断状态\n"
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n"
    )
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable,
                             "-c", "print('passed')").returncode == 0
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr


def test_canonical_issue_rename_keeps_both_ends_in_applied_expansion_receipt(tmp_path: Path) -> None:
    repo, run_dir, attempt, common = prepare_canonical_issue_outside_frozen_scope(tmp_path)
    renamed = "web-ui/src/components/renamed-board-card-session-activity.ts"
    git(Path(attempt["worktree_path"]), "mv", ISSUE_PRIMARY_PATH, renamed)
    run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "stop", "--attempt-id", attempt["attempt_id"],
                          "--status", "fixed", "--scope-expansion-path", renamed,
                          "--scope-expansion-reason", "主路径修复包含重命名", *common)
    applied = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "apply", "--attempt-id", attempt["attempt_id"],
                                   "--target-repo", str(repo), *common)
    assert applied["status"] == "applied"
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n"
    )
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable,
                             "-c", "print('passed')").returncode == 0
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    receipt = json.loads(Path(payload["scope"]["applied_scope_expansion_receipt_path"]).read_text())
    assert set(receipt["expansions"][0]["paths"]) == {ISSUE_PRIMARY_PATH, renamed}
    patch = run_dir / "artifacts/fix-attempts" / attempt["attempt_id"] / "fix.patch"
    patch.write_bytes(patch.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="facts do not prove"):
        evidence.validated_reviewed_task_candidate_evidence(repo)


@pytest.mark.parametrize("operation", ["empty_file_created", "empty_file_deleted", "mode_changed", "copy_created"])
def test_zero_numstat_file_operations_keep_their_actual_paths_through_formal_land(
    tmp_path: Path, operation: str,
) -> None:
    primary_contents = None if operation in {"empty_file_created", "copy_created"} else ""
    repo, run_dir, attempt, common = prepare_canonical_issue_outside_frozen_scope(
        tmp_path, primary_contents=primary_contents,
    )
    worktree = Path(attempt["worktree_path"])
    primary = worktree / ISSUE_PRIMARY_PATH
    if operation == "empty_file_deleted":
        primary.unlink()
    elif operation == "mode_changed":
        primary.chmod(0o755)
    elif operation == "copy_created":
        primary.write_bytes((worktree / DIRECT_DEPENDENCY_PATHS[0]).read_bytes())
    else:
        primary.write_bytes(b"")
    git(worktree, "add", "--all")
    stopped = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "stop", "--attempt-id", attempt["attempt_id"],
                                   "--status", "fixed", *common)
    directory = run_dir / "artifacts/fix-attempts" / attempt["attempt_id"]
    if operation == "copy_created":
        # 等价的真实 Git copy 补丁只改目的路径；来源文件保持原样。
        copy_patch = git(worktree, "diff", "--binary", "--find-copies-harder", "--find-copies=100%", "HEAD") + "\n"
        assert "copy from " in copy_patch
        (directory / "fix.patch").write_text(copy_patch)
    applied = run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "apply", "--attempt-id", attempt["attempt_id"],
                                   "--target-repo", str(repo), *common)
    assert applied["status"] == "applied"
    git(repo, "worktree", "remove", "--force", str(worktree))
    assert actual_fix_patch_changed_paths((directory / "fix.patch").read_bytes(), repo) == {ISSUE_PRIMARY_PATH}
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n"
    )
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable,
                             "-c", "print('passed')").returncode == 0
    frozen_contract_bytes = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    original_bytes = {path: path.read_bytes() for path in (
        run_dir / "artifacts/handoff.md", run_dir / "artifacts/fix-issues/RVF-004.json",
        *(directory / filename for filename in ("attempt.json", "result.json", "fix.patch")),
    )}
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    receipt = json.loads(Path(payload["scope"]["applied_scope_expansion_receipt_path"]).read_text())
    assert receipt["expansions"][0]["paths"] == [ISSUE_PRIMARY_PATH]
    assert {entry["path"] for entry in stopped["changed_paths"]} == {ISSUE_PRIMARY_PATH}
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes
    assert canonical_bytes(json.loads((run_dir / "artifacts/inputs/scope.contract.reviewed.json").read_text())) == frozen_contract_bytes


def test_rename_and_copy_keep_paths_bound_to_each_parsed_diff(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    original = '原有 "带引号"\t文件.txt'
    renamed = '重命名 "带引号"\t文件.txt'
    copied = "复制目的文件.txt"
    (repo / original).write_text("保留相同内容\n", encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "test: 建立带引号的来源文件")
    git(repo, "mv", original, renamed)
    (repo / copied).write_bytes((repo / renamed).read_bytes())
    git(repo, "add", "--all")
    patch = git(repo, "diff", "--binary", "--find-copies-harder", "--find-copies=100%", "HEAD") + "\n"
    assert "copy from " in patch and "rename from " in patch
    assert actual_fix_patch_changed_paths(patch.encode(), repo) == {original, renamed, copied}
