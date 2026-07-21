from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
VALIDATION_RESULT_CACHE = SCRIPT_DIRECTORY / "validation_result_cache.py"
WRITE_CANDIDATE_EVIDENCE = SCRIPT_DIRECTORY / "write_reviewed_task_candidate_evidence.py"


def clean_test_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
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
        "bootstrap_receipt_path": None,
        "bootstrap_receipt_digest": None,
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
        "bootstrap_path": None,
        "bootstrap_sha256": None,
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
        result["issues"] = [{"path": "tracked.txt", "line": 1, "message": message}]
        result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n"
        "## 本次 review\n\n共 1 条，1 真实修复 / 0 误报 / 0 升级\n\n"
        "## Issue 处理结果\n\n- **[fixed]** `tracked.txt:1` - canonical issue\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(Path(json.loads(completed.stdout)["candidate_evidence_path"]).read_text())
    assert evidence["review"]["canonical_issue_count"] == 1
    assert evidence["review"]["conclusion"] == "fixes_applied_and_validated"


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


def test_binds_bootstrap_receipt_only_from_the_task_git_directory(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    common_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    head = git(repo, "rev-parse", "HEAD")
    receipt_path = (
        git_dir
        / "base-branch-sync"
        / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt = {
        "schema_name": "local-base-into-task-worktree-bootstrap-receipt",
        "schema_version": 1,
        "absorbed_base_tip": head,
        "base_ref": "main",
        "remote_name": "origin",
        "task_tip_before_base_absorption": head,
        "task_tip_after_base_absorption": head,
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "boundary_status": "VERIFIED",
        "lock_status": "RELEASED",
        "target_repository_hooks_status": "DISABLED",
        "wip_status": "NOT_REQUIRED_TASK_WORKTREE_CLEAN",
    }
    receipt_encoded = canonical_bytes(receipt)
    receipt_path.write_bytes(receipt_encoded)
    receipt_digest = hashlib.sha256(receipt_encoded).hexdigest()
    Path(f"{receipt_path}.sha256").write_text(receipt_digest + "\n", encoding="ascii")
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    updates = {
        "bootstrap_receipt_path": str(receipt_path),
        "bootstrap_receipt_digest": receipt_digest,
    }
    contract.update(updates)
    contract["canonical_scope"].update(updates)
    contract["scope_hash"] = hashlib.sha256(
        canonical_bytes(contract["canonical_scope"])
    ).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))

    completed = subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(Path(json.loads(completed.stdout)["candidate_evidence_path"]).read_text())
    assert evidence["receipts"]["bootstrap_path"] == str(receipt_path)
    assert evidence["receipts"]["bootstrap_sha256"] == receipt_digest


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
