from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
VALIDATION_RESULT_CACHE = SCRIPT_DIRECTORY / "validation_result_cache.py"
WRITE_CANDIDATE_EVIDENCE = SCRIPT_DIRECTORY / "write_reviewed_task_candidate_evidence.py"


def clean_test_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    # 同时剥掉 RVF_* / CODEX_RVF_*：被测脚本不得读到活 run 的环境。validation_result_cache 曾回退读
    # RVF_RUN_DIR、把测试记录写进活 run 的 validation index；源头已删，这里保留为纵深防御。
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GIT_", "RVF_", "CODEX_RVF_"))
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    environment.update(extra or {})
    return environment


def canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=clean_test_environment(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "RVF Candidate Evidence Test")
    git(path, "config", "user.email", "rvf-candidate@example.test")
    (path / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "candidate")
    return path


def create_completed_run(tmp_path: Path, repo: Path, run_name: str = "run") -> Path:
    run_dir = tmp_path / run_name
    artifacts = run_dir / "artifacts"
    inputs = artifacts / "inputs"
    reviewer_root = artifacts / "reviewers"
    reviewer = reviewer_root / "reviewer-a"
    second_reviewer = reviewer_root / "reviewer-b"
    reviewer.mkdir(parents=True)
    second_reviewer.mkdir(parents=True)
    inputs.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": "rvf-candidate-evidence-test", "repo": str(repo)}) + "\n",
        encoding="utf-8",
    )
    canonical_scope: dict[str, object] = {
        "version": 3,
        "repo": str(repo),
        "scope_mode": "custom",
        "primary_files": ["tracked.txt"],
        "background_files": [],
        "protected_files": [],
        "canonical_issues": [],
        "fix_allowlist": ["tracked.txt"],
        "excluded_path_prefixes": [],
        "review_kind": "implementation",
        "base_ref": "main",
        "remote_name": "origin",
        "comparison_base_tip": git(repo, "rev-parse", "HEAD"),
        "reviewed_base_tip": git(repo, "rev-parse", "HEAD"),
        "reviewed_task_tip": git(repo, "rev-parse", "HEAD"),
        "candidate_task_tip": git(repo, "rev-parse", "HEAD"),
        "base_delta_paths": [],
        "task_change_paths": [],
        "interaction_primary_paths": [],
        "base_delta_protected_paths": [],
        "candidate_evidence_path": None,
        "candidate_evidence_digest": None,
        "integration_candidate_receipt_path": None,
        "integration_candidate_receipt_digest": None,
    }
    contract = {
        **canonical_scope,
        "run_id": "rvf-candidate-evidence-test",
        "scope_hash": hashlib.sha256(canonical_bytes(canonical_scope)).hexdigest(),
        "canonical_scope": canonical_scope,
    }
    (inputs / "scope.contract.json").write_bytes(canonical_bytes(contract))
    review_result = {
        "schema_version": 1,
        "kind": "no_issues",
        "issues": [],
        "requests": [],
        "audit_summary": "candidate is clean",
    }
    for reviewer_directory in (reviewer, second_reviewer):
        (reviewer_directory / "review-result.json").write_text(
            json.dumps(review_result) + "\n", encoding="utf-8"
        )
    (reviewer_root / "reviewer-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "reviewers": [
                    {"reviewer_id": "reviewer-a", "returncode": 0},
                    {"reviewer_id": "reviewer-b", "returncode": 0},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifacts / "handoff.md").write_text(
        "# handoff\n\n- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n"
        "## 本次 review\n\n共 0 条，0 真实修复 / 0 误报 / 0 升级\n",
        encoding="utf-8",
    )
    environment = clean_test_environment(
        {"RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "user-data")}
    )
    validation = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            "targeted-contract-tests",
            "--rvf-run-dir",
            str(run_dir),
            "--",
            sys.executable,
            "-c",
            "print('passed')",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(validation.stdout)["status"] == "passed"
    return run_dir


def write_merge_dispositions_and_upsert_fix_issues(
    tmp_path: Path, repo: Path, run_dir: Path, decisions: dict[str, object], issue_ids: list[str]
) -> None:
    """经真实 ``rvf_merge_dispositions.py write`` + ``rvf_fix_issue.py upsert`` 落合并处置与 fix-issue 镜像。"""
    environment = clean_test_environment()
    written = subprocess.run(
        [sys.executable, str(SCRIPT_DIRECTORY / "rvf_merge_dispositions.py"), "write", "--run-dir", str(run_dir)],
        input=json.dumps(decisions),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    for issue_id in issue_ids:
        issue_file = tmp_path / f"{issue_id}-canonical-issue.json"
        issue_file.write_text(
            json.dumps({"issue_id": issue_id, "path": "tracked.txt", "line": 1, "message": "canonical issue"}) + "\n",
            encoding="utf-8",
        )
        upserted = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIRECTORY / "rvf_fix_issue.py"),
                "upsert",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-file",
                str(issue_file),
                "--log-root",
                str(tmp_path / "fix-issue-tracker-log-root"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert upserted.returncode == 0, upserted.stderr


def record_terminal_fix_attempt(run_dir: Path, issue_id: str, *, status: str = "fixed", applied: bool = True,
                                attempt_number: int = 1, changed_paths: list[str] | None = None,
                                scope_expansion: dict | None = None, confirmed_paths: list[str] | None = None) -> Path:
    """构造终态账本 fixture；真实 apply 流程另由 land 的隔离 Git 回归覆盖。"""
    run_id = json.loads((run_dir / "summary.json").read_text())["run_id"]
    attempt_id = f"attempt-{issue_id}-{attempt_number}"
    directory = run_dir / "artifacts/fix-attempts" / attempt_id
    directory.mkdir(parents=True)
    patch = directory / "fix.patch"
    patch.write_text("fixture patch\n")
    identity = {"schema_version": 1, "run_id": run_id, "issue_id": issue_id, "attempt_id": attempt_id}
    attempt = {**identity, "created_at": f"2026-10-24T00:00:{attempt_number:02d}+00:00",
               "repo": json.loads((run_dir / "summary.json").read_text())["repo"],
               "status": "applied" if applied and status == "fixed" else status,
               "scope_expansion": scope_expansion or {},
               "confirmed_acknowledged_protected_or_background_paths": confirmed_paths or []}
    result = {**identity, "status": status, "changed_paths": [{"path": path, "op": "modified"} for path in (changed_paths or [])],
              "scope_expansion": scope_expansion or {}, "fix_patch_path": str(patch)}
    if status == "validated_and_submitted_to_kanban_backlog":
        result["kanban_backlog_submission"] = {"issue_id": issue_id, "attempt_id": attempt_id,
            "groups": [{"outcome": "created_kanban_backlog_task", "kanban_task_id": "fixture-task"}]}
    (directory / "attempt.json").write_text(json.dumps(attempt))
    (directory / "result.json").write_text(json.dumps(result))
    return directory


def bind_scope_to_parent_evidence(
    run_dir: Path,
    repo: Path,
    parent_evidence_path: Path,
    parent_evidence_digest: str,
) -> None:
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    canonical_scope = contract["canonical_scope"]
    head = git(repo, "rev-parse", "HEAD")
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    common_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    integration_receipt_path = (
        git_dir
        / "base-branch-sync"
        / "reviewed-task-handback-integration-candidate.json"
    )
    integration_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    integration_receipt = {
        "schema_name": "reviewed-task-handback-integration-candidate",
        "schema_version": 1,
        "status": "INTEGRATION_DELTA_RVF_REQUIRED",
        "risk_classification": "MEDIUM",
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "base_ref": "main",
        "remote_name": "origin",
        "reviewed_candidate_evidence_path": str(parent_evidence_path),
        "reviewed_candidate_evidence_sha256": parent_evidence_digest,
        "reviewed_base_tip": head,
        "base_tip_at_candidate": head,
        "reviewed_task_tip": head,
        "candidate_task_tip": head,
        "base_delta_paths": [],
        "base_delta_protected_paths": [],
        "task_change_paths": ["tracked.txt"],
        "interaction_primary_paths": [],
    }
    integration_encoded = canonical_bytes(integration_receipt)
    integration_receipt_path.write_bytes(integration_encoded)
    integration_digest = hashlib.sha256(integration_encoded).hexdigest()
    Path(f"{integration_receipt_path}.sha256").write_text(
        integration_digest + "\n", encoding="ascii"
    )
    updates = {
        "review_kind": "base_drift_integration",
        "candidate_evidence_path": str(parent_evidence_path),
        "candidate_evidence_digest": parent_evidence_digest,
        "integration_candidate_receipt_path": str(integration_receipt_path),
        "integration_candidate_receipt_digest": integration_digest,
    }
    canonical_scope.update(updates)
    contract.update(updates)
    contract["scope_hash"] = hashlib.sha256(canonical_bytes(canonical_scope)).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))


def test_writes_completion_gated_candidate_evidence_for_current_content(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    completed = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(completed.stdout)
    evidence_path = Path(output["candidate_evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema"] == "rvf-reviewed-task-candidate-evidence"
    assert evidence["schema_version"] == 1
    assert evidence["subject"]["head_oid"] == git(repo, "rev-parse", "HEAD")
    assert evidence["subject"]["candidate_tree_oid"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert evidence["subject"]["candidate_content_manifest_algorithm"] == "git-ls-files-content-manifest-v1"
    assert evidence["review"] == {
        "run_id": "rvf-candidate-evidence-test",
        "review_kind": "implementation",
        "conclusion": "no_issues",
        "protocol_valid": True,
        "canonical_issue_count": 0,
        "completed_at": evidence["review"]["completed_at"],
    }
    assert len(evidence["validation_records"]) == 1
    assert evidence["validation_records"][0]["status"] == "passed"
    assert evidence["validation_records"][0]["candidate_content_manifest_sha256"] == (
        evidence["subject"]["candidate_content_manifest_sha256"]
    )
    assert evidence["receipts"] == {
        "parent_evidence_path": None,
        "parent_evidence_sha256": None,
        "integration_candidate_path": None,
        "integration_candidate_sha256": None,
    }
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert Path(f"{evidence_path}.sha256").read_text(encoding="ascii").strip() == digest
    reused = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(reused.stdout)["status"] == "reused"
    assert not (evidence_path.parent / "immutable").exists()


def test_unabsorbed_base_evidence_is_accepted_by_installed_handback_consumer(tmp_path: Path) -> None:
    consumer = Path(os.environ.get("RVF_TEST_HAND_BACK_CONSUMER_SCRIPT", str(Path.home() / ".codex/skills/base-branch-sync/scripts/base-branch-sync-evidence-aware-optimistic-handback.py")))
    if not consumer.is_file():
        pytest.skip("本机未安装跨仓 handback consumer")
    repo = repository(tmp_path / "repo")
    common_ancestor = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", "-q")
    (repo / "tracked.txt").write_text("reviewed task implementation\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "test: 任务候选")
    base_tip = git(repo, "commit-tree", git(repo, "rev-parse", f"{common_ancestor}^{{tree}}"),
                   "-p", common_ancestor, "-m", "test: base 前移")
    git(repo, "update-ref", "refs/heads/main", base_tip)
    run_dir = create_completed_run(tmp_path, repo)
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text())
    for target in (contract, contract["canonical_scope"]):
        target["comparison_base_tip"] = common_ancestor
        target["reviewed_base_tip"] = common_ancestor
    contract["scope_hash"] = hashlib.sha256(canonical_bytes(contract["canonical_scope"])).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))
    written = subprocess.run([sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
                             env=clean_test_environment(), capture_output=True, text=True)
    assert written.returncode == 0, written.stderr
    inspected = subprocess.run([sys.executable, str(consumer), "inspect-reviewed-candidate-evidence", "--task-worktree", str(repo)],
                               env=clean_test_environment(), capture_output=True, text=True)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert "VERIFIED" in inspected.stdout
    assert git(repo, "rev-parse", "main") == base_tip


def test_refuses_evidence_when_completion_or_validation_gate_is_not_satisfied(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    validation_index = run_dir / "artifacts" / "validation-records.json"
    empty_index = {"schema_version": 1, "validation_records": []}
    encoded_empty_index = canonical_bytes(empty_index)
    validation_index.write_bytes(encoded_empty_index)
    Path(f"{validation_index}.sha256").write_text(
        hashlib.sha256(encoded_empty_index).hexdigest() + "\n", encoding="ascii"
    )
    completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "at least one passed validation record" in completed.stderr
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    assert not (git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json").exists()


def test_refuses_partial_reviewer_plan_and_elevated_handoff(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    reviewer_plan = run_dir / "artifacts/reviewers/reviewer-plan.json"
    plan = json.loads(reviewer_plan.read_text(encoding="utf-8"))
    plan["status"] = "partial"
    reviewer_plan.write_text(json.dumps(plan) + "\n", encoding="utf-8")
    partial = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert partial.returncode == 2
    assert "reviewer plan is not completed" in partial.stderr

    plan["status"] = "completed"
    reviewer_plan.write_text(json.dumps(plan) + "\n", encoding="utf-8")
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n- **[elevated]** unresolved\n",
        encoding="utf-8",
    )
    elevated = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert elevated.returncode == 2
    assert "handoff contains unresolved elevated or failed outcomes" in elevated.stderr


def test_uses_handoff_canonical_issue_count_instead_of_raw_reviewer_issue_count(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    reviewer_root = run_dir / "artifacts/reviewers"
    for reviewer_id, message in (("reviewer-a", "first wording"), ("reviewer-b", "second wording")):
        result_path = reviewer_root / reviewer_id / "review-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["kind"] = "issues"
        result["issues"] = [
            {"path": "tracked.txt", "line": 1, "kind": "REAL", "severity": "medium", "message": message}
        ]
        result.pop("audit_summary", None)
        result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    # 两腿同根因：合并处置把它们并成一条 canonical fix-issue（真实 CLI 写处置 + upsert，不手抄形状）。
    write_merge_dispositions_and_upsert_fix_issues(
        tmp_path,
        repo,
        run_dir,
        {
            "dispositions": [
                {"reviewer_id": "reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
                {"reviewer_id": "reviewer-b", "index": 1, "disposition": "merged_into", "issue_id": "RVF-001"},
            ]
        },
        ["RVF-001"],
    )
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n"
        "## 本次 review\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n\n"
        "## Issue 处理结果\n\n- **[fixed]** `tracked.txt:1` - canonical issue\n",
        encoding="utf-8",
    )
    record_terminal_fix_attempt(run_dir, "RVF-001")
    completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(Path(json.loads(completed.stdout)["candidate_evidence_path"]).read_text())
    assert evidence["review"]["canonical_issue_count"] == 1
    assert evidence["review"]["conclusion"] == "fixes_applied_and_validated"


def test_issues_submitted_to_kanban_backlog_count_as_completed_outcomes(tmp_path: Path) -> None:
    """范围外问题只验证、不修、提交 backlog：它是已完成结论，计入 canonical 总数；计数与标签对不上仍拒绝。"""
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    reviewer_root = run_dir / "artifacts/reviewers"
    result_path = reviewer_root / "reviewer-a" / "review-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["kind"] = "issues"
    result["issues"] = [
        {"path": "tracked.txt", "line": 1, "kind": "REAL", "severity": "medium", "message": "in-round bug"},
        {"path": "tracked.txt", "line": 2, "kind": "REAL", "severity": "low", "message": "pre-existing bug",
         "scope_relation": "outside_current_round_scope"},
    ]
    result.pop("audit_summary", None)
    result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    write_merge_dispositions_and_upsert_fix_issues(
        tmp_path,
        repo,
        run_dir,
        {
            "dispositions": [
                {"reviewer_id": "reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
                {"reviewer_id": "reviewer-a", "index": 2, "disposition": "adopted", "issue_id": "RVF-002"},
            ]
        },
        ["RVF-001", "RVF-002"],
    )
    handoff = run_dir / "artifacts/handoff.md"
    body = (
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n"
        "## 本次 review\n\n共 2 条，1 真实修复 / 0 误报 / 1 转 backlog / 0 升级\n\n"
        "## Issue 处理结果\n\n- **[fixed]** `tracked.txt:1` - in-round\n"
    )
    handoff.write_text(body, encoding="utf-8")
    record_terminal_fix_attempt(run_dir, "RVF-001")
    record_terminal_fix_attempt(run_dir, "RVF-002", status="validated_and_submitted_to_kanban_backlog")
    refused = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "does not match completed outcomes" in refused.stderr
    handoff.write_text(
        body + "- **[submitted_to_kanban_backlog]** `tracked.txt:2` - pre-existing\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(Path(json.loads(completed.stdout)["candidate_evidence_path"]).read_text())
    assert evidence["review"]["canonical_issue_count"] == 2


def test_integration_delta_evidence_archives_and_binds_the_contracted_parent(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path / "repo")
    first_run = create_completed_run(tmp_path, repo, "first-run")
    first_completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(first_run)],
        capture_output=True,
        text=True,
        check=True,
    )
    first_output = json.loads(first_completed.stdout)
    parent_path = Path(first_output["candidate_evidence_path"])
    parent_digest = first_output["candidate_evidence_sha256"]
    integration_run = create_completed_run(tmp_path, repo, "integration-run")
    bind_scope_to_parent_evidence(
        integration_run, repo, parent_path, parent_digest
    )
    integrated = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--repo",
            str(repo),
            "--run-dir",
            str(integration_run),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(Path(json.loads(integrated.stdout)["candidate_evidence_path"]).read_text())
    archived_parent = Path(evidence["receipts"]["parent_evidence_path"])
    assert evidence["review"]["review_kind"] == "base_drift_integration"
    assert evidence["receipts"]["parent_evidence_sha256"] == parent_digest
    assert archived_parent.name.startswith(parent_digest)
    assert hashlib.sha256(archived_parent.read_bytes()).hexdigest() == parent_digest

    reused = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--repo",
            str(repo),
            "--run-dir",
            str(integration_run),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(reused.stdout)["status"] == "reused"


def test_refuses_dirty_tracked_or_untracked_content_without_overwriting_current_evidence(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    written = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence_path = Path(json.loads(written.stdout)["candidate_evidence_path"])
    original_evidence = evidence_path.read_bytes()

    (repo / "tracked.txt").write_text("dirty tracked\n", encoding="utf-8")
    tracked_dirty = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked_dirty.returncode == 2
    assert "task worktree must be clean" in tracked_dirty.stderr
    assert evidence_path.read_bytes() == original_evidence

    git(repo, "restore", "tracked.txt")
    (repo / "untracked.txt").write_text("dirty untracked\n", encoding="utf-8")
    untracked_dirty = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert untracked_dirty.returncode == 2
    assert "task worktree must be clean" in untracked_dirty.stderr
    assert evidence_path.read_bytes() == original_evidence




def test_non_opt_in_scope_is_skipped_only_in_explicit_if_eligible_mode(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    for name in (
        "comparison_base_tip",
        "reviewed_base_tip",
        "reviewed_task_tip",
        "candidate_task_tip",
    ):
        contract[name] = None
        contract["canonical_scope"][name] = None
    contract["scope_hash"] = hashlib.sha256(
        canonical_bytes(contract["canonical_scope"])
    ).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))

    strict = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert strict.returncode == 2
    eligible = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--if-eligible",
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(eligible.stdout)["status"] == "skipped_not_applicable"
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    assert not (git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json").exists()


def test_refuses_opted_in_scope_with_missing_base_identity_before_writing(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    for name in ("base_ref", "remote_name"):
        contract[name] = None
        contract["canonical_scope"][name] = None
    contract["scope_hash"] = hashlib.sha256(
        canonical_bytes(contract["canonical_scope"])
    ).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))

    refused = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--if-eligible",
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 2
    assert "scope contract base_ref is missing or invalid" in refused.stderr
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    evidence_path = git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json"
    assert not evidence_path.exists()
    assert not Path(f"{evidence_path}.sha256").exists()

    contract["base_ref"] = "main"
    contract["remote_name"] = "origin"
    contract["canonical_scope"]["base_ref"] = "main"
    contract["canonical_scope"]["remote_name"] = "origin"
    contract["scope_hash"] = hashlib.sha256(
        canonical_bytes(contract["canonical_scope"])
    ).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))

    written = subprocess.run(
        [
            sys.executable,
            str(WRITE_CANDIDATE_EVIDENCE),
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(written.stdout)["status"] == "written"


# ===========================================================================
# 单调祖先关系、land 后 restamp 与落地 delta 收敛。
# 用不同提交构造各个 tip，先断言它们不相等，再验证行为。
# ===========================================================================


def advance_local_base(repo: Path) -> str:
    """在 base ref 上造一个子提交并推进 ref（不切分支、不动工作树）。

    形状与真实场景一致：别的 worktree 往本地 base 落了提交 / `base-branch-sync` 做了 fast-forward。
    """
    tip = git(repo, "rev-parse", "main")
    tree = git(repo, "rev-parse", f"{tip}^{{tree}}")
    advanced = git(repo, "commit-tree", tree, "-p", tip, "-m", "local base advances")
    git(repo, "update-ref", "refs/heads/main", advanced)
    return advanced




def rewrite_contract(run_dir: Path, **updates: object) -> dict[str, object]:
    """改 contract 的字段并**同步**改 canonical_scope + 重算 scope_hash（生产同款三处一致）。"""
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    canonical_scope = contract["canonical_scope"]
    for name, value in updates.items():
        contract[name] = value
        if name in canonical_scope:
            canonical_scope[name] = value
    contract["scope_hash"] = hashlib.sha256(canonical_bytes(canonical_scope)).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))
    return contract


def run_writer(repo: Path, run_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def opted_in_repo_with_land_commit(
    tmp_path: Path, *, extra_base_files: dict[str, str] | None = None
) -> tuple[Path, Path, str, str]:
    """建「prepare 冻结在 head0 → land 提交推进到 head1」的现场，返回 (repo, run_dir, head0, head1)。"""
    repo = repository(tmp_path / "repo")
    for name, content in (extra_base_files or {}).items():
        (repo / name).write_text(content, encoding="utf-8")
        git(repo, "add", name)
    (repo / "AGENTS.md").write_text(
        "RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n",
        encoding="utf-8",
    )
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-q", "-m", "opt in")
    prepare_time_head = git(repo, "rev-parse", "HEAD")

    (repo / "tracked.txt").write_text("landed fix\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-q", "-m", "rvf-land fix")
    post_land_head = git(repo, "rev-parse", "HEAD")
    # 防恒等守卫：整族测试的前提就是这两个值必须不同。
    assert prepare_time_head != post_land_head

    # validation record 必须对应**落地后**的内容，故 run 在提交之后建。
    run_dir = create_completed_run(tmp_path, repo)
    return repo, run_dir, prepare_time_head, post_land_head










def test_land_commit_restamps_the_contract_and_records_landing_provenance(tmp_path: Path) -> None:
    """K5 / N7 + N7b：三重一致性 + provenance，一次断完。"""
    repo, run_dir, prepare_time_head, post_land_head = opted_in_repo_with_land_commit(tmp_path)
    rewrite_contract(
        run_dir,
        reviewed_task_tip=prepare_time_head,
        candidate_task_tip=prepare_time_head,
    )
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    reviewed_scope_hash = json.loads(contract_path.read_text(encoding="utf-8"))["scope_hash"]

    completed = run_writer(repo, run_dir)
    assert completed.returncode == 0, completed.stderr

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    # ① top-level 与 canonical_scope 同步推进到落地后的 HEAD
    assert contract["candidate_task_tip"] == post_land_head
    assert contract["canonical_scope"]["candidate_task_tip"] == post_land_head
    # ② scope_hash 与 canonical_scope 自洽
    assert contract["scope_hash"] == hashlib.sha256(
        canonical_bytes(contract["canonical_scope"])
    ).hexdigest()
    assert contract["scope_hash"] != reviewed_scope_hash
    # ③ provenance 保留了被评审那一版，且 landing_restamp 不进 canonical_scope
    assert "landing_restamp" not in contract["canonical_scope"]
    (restamp,) = contract["landing_restamp"]
    assert restamp["reviewed_candidate_task_tip"] == prepare_time_head
    assert restamp["landed_candidate_task_tip"] == post_land_head
    assert restamp["reviewed_scope_hash"] == reviewed_scope_hash
    assert restamp["landed_delta_paths"] == ["tracked.txt"]
    # 冻结的被评审契约存在，且其 digest 就是记录里的值
    reviewed_contract_path = Path(restamp["reviewed_contract_path"])
    assert reviewed_contract_path.is_file()
    assert (
        hashlib.sha256(reviewed_contract_path.read_bytes()).hexdigest()
        == restamp["reviewed_contract_sha256"]
    )
    assert json.loads(reviewed_contract_path.read_text(encoding="utf-8"))[
        "candidate_task_tip"
    ] == prepare_time_head

    evidence = json.loads(
        Path(json.loads(completed.stdout)["candidate_evidence_path"]).read_text(encoding="utf-8")
    )
    # 消费侧的四条等式在 restamp 之后同时成立
    assert evidence["subject"]["head_oid"] == post_land_head == contract["candidate_task_tip"]
    assert evidence["scope"]["scope_digest"] == contract["scope_hash"]
    assert evidence["scope"]["contract_sha256"] == hashlib.sha256(
        canonical_bytes(contract)
    ).hexdigest()
    # evidence 自身仍说得出「reviewer 当时看的是哪个 tip」
    assert evidence["subject"]["reviewed_task_tip"] == prepare_time_head


def test_restamp_is_a_no_op_when_rvf_land_committed_nothing(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    head = git(repo, "rev-parse", "HEAD")
    assert json.loads(
        (run_dir / "artifacts/inputs/scope.contract.json").read_text(encoding="utf-8")
    )["candidate_task_tip"] == head

    completed = run_writer(repo, run_dir)
    assert completed.returncode == 0, completed.stderr
    contract = json.loads(
        (run_dir / "artifacts/inputs/scope.contract.json").read_text(encoding="utf-8")
    )
    assert "landing_restamp" not in contract
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()


def test_landed_delta_outside_the_reviewed_scope_is_refused_without_half_writing(
    tmp_path: Path,
) -> None:
    """放开 tip 等号的**配套闸**：land 的最小修正不得落地 reviewer 没看过的路径。"""
    repo = repository(tmp_path / "repo")
    prepare_time_head = git(repo, "rev-parse", "HEAD")
    (repo / "outside.txt").write_text("never reviewed\n", encoding="utf-8")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-q", "-m", "land touches an unreviewed path")
    post_land_head = git(repo, "rev-parse", "HEAD")
    assert prepare_time_head != post_land_head

    run_dir = create_completed_run(tmp_path, repo)
    rewrite_contract(
        run_dir,
        reviewed_task_tip=prepare_time_head,
        candidate_task_tip=prepare_time_head,
    )
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    before_bytes = contract_path.read_bytes()

    completed = run_writer(repo, run_dir)
    assert completed.returncode != 0
    assert "landed delta escaped the reviewed scope" in completed.stderr
    assert "outside.txt" in completed.stderr
    # 不得留下半改的 contract
    assert contract_path.read_bytes() == before_bytes
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()


def test_rename_out_of_an_unreviewed_path_is_caught_because_renames_are_disabled(
    tmp_path: Path,
) -> None:
    """`--no-renames` 的存在理由，用一个开启改名检测就会漏掉的构造来钉住。

    构造：源路径 `outside.txt` **不在** scope，目的路径 `moved-into-scope.txt` **在** scope，
    且该轮只有这一次纯改名、没有别的改动。
    开启改名检测时 `--name-only` 只打印目的路径 ⇒ delta ⊆ scope ⇒ 放行（错的）。
    关掉之后源与目的都出现 ⇒ `outside.txt` 越界 ⇒ 拦住（对的）。
    """
    repo = repository(tmp_path / "repo")
    (repo / "outside.txt").write_text("x" * 400 + "\n", encoding="utf-8")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-q", "-m", "an unreviewed file exists at prepare time")
    prepare_time_head = git(repo, "rev-parse", "HEAD")

    git(repo, "mv", "outside.txt", "moved-into-scope.txt")
    git(repo, "commit", "-q", "-m", "land renames an unreviewed file into scope")
    post_land_head = git(repo, "rev-parse", "HEAD")
    assert prepare_time_head != post_land_head
    # 防恒等守卫：开启改名检测时该 delta **只有目的路径**，确实会被判成「全在 scope 内」。
    # 这条断言一旦不成立，下面的用例就不再证明 `--no-renames` 有意义。
    renames_detected = git(
        repo, "diff", "--name-only", "--find-renames", prepare_time_head, post_land_head
    ).split()
    assert renames_detected == ["moved-into-scope.txt"]

    run_dir = create_completed_run(tmp_path, repo)
    rewrite_contract(
        run_dir,
        primary_files=["tracked.txt", "moved-into-scope.txt"],
        fix_allowlist=["tracked.txt", "moved-into-scope.txt"],
        reviewed_task_tip=prepare_time_head,
        candidate_task_tip=prepare_time_head,
    )

    completed = run_writer(repo, run_dir)
    assert completed.returncode != 0
    assert "landed delta escaped the reviewed scope" in completed.stderr
    assert "outside.txt" in completed.stderr


def test_incomplete_review_refuses_before_restamping_anything(tmp_path: Path) -> None:
    """restamp 是本 writer 唯一的写操作，必须排在**全部只读闸之后**。

    构造：contract 已 opt-in、land 提交也真的发生了（两 tip 不等，restamp 有事可做），
    但 handoff 的 `review_status` 是 `PENDING`（双评审不成立时的正确记法）。
    断言 writer 拒绝，且 **`scope.contract.json` 一个字节都没变、没有落下冻结副本**——
    否则一轮未通过评审的 run 会先被改写契约再 fail-closed。
    """
    repo, run_dir, prepare_time_head, post_land_head = opted_in_repo_with_land_commit(tmp_path)
    assert prepare_time_head != post_land_head  # 防恒等守卫：restamp 必须有事可做
    rewrite_contract(
        run_dir,
        reviewed_task_tip=prepare_time_head,
        candidate_task_tip=prepare_time_head,
    )
    handoff_path = run_dir / "artifacts/handoff.md"
    handoff_path.write_text(
        handoff_path.read_text(encoding="utf-8").replace(
            "- review_status: COMPLETED", "- review_status: PENDING"
        ),
        encoding="utf-8",
    )

    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    before_bytes = contract_path.read_bytes()

    completed = run_writer(repo, run_dir)

    assert completed.returncode != 0
    assert contract_path.read_bytes() == before_bytes
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()
