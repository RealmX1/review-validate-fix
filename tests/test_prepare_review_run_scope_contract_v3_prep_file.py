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
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import prepare_review_run  # noqa: E402
import rvf_prep_file  # noqa: E402


def clean_test_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    )
    return environment


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=clean_test_environment(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_canonical_artifact(path: Path, payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    Path(f"{path}.sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def test_prepare_run_from_prep_file_forwards_scope_contract_v3_extensions(tmp_path: Path, monkeypatch) -> None:
    # 此处只验证 v3 字段透传；分界真实性由专用宿主/Git 集成套件验证。
    monkeypatch.setattr(prepare_review_run, "require_review_start_boundary_for_prepare", lambda **kw: {"test_scope": "v3-field-forwarding"})
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "RVF Prep v3 Test")
    git(repo, "config", "user.email", "rvf-prep-v3@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-q", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    bootstrap_path = git_dir / "base-branch-sync/local-base-into-task-worktree-bootstrap-receipt.json"
    candidate_path = git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json"
    integration_path = (
        git_dir
        / "base-branch-sync/reviewed-task-handback-integration-candidate.json"
    )
    bootstrap_digest = write_canonical_artifact(bootstrap_path, {"schema_version": 1})
    candidate_digest = write_canonical_artifact(candidate_path, {"schema_version": 1})
    integration_digest = write_canonical_artifact(
        integration_path, {"schema_version": 1}
    )
    run_dir = tmp_path / "state/runs/rvf-prep-v3"
    record = rvf_prep_file.write_prep_file(
        {
            "origin_session_id": "session-v3",
            "origin_repo": str(repo),
            "origin_cwd": str(repo),
            "target_worktree": str(repo),
            "target_flow": "flow-manual",
            "review_kind": "base_drift_integration",
            "base_ref": "main",
            "remote_name": "origin",
            "comparison_base_tip": head,
            "reviewed_base_tip": head,
            "reviewed_task_tip": head,
            "candidate_task_tip": head,
            "base_delta_paths": ["base.txt"],
            "task_change_paths": ["tracked.txt"],
            "interaction_primary_paths": ["shared.txt"],
            "base_delta_protected_paths": ["package.json"],
            "bootstrap_receipt_path": str(bootstrap_path),
            "bootstrap_receipt_digest": bootstrap_digest,
            "candidate_evidence_path": str(candidate_path),
            "candidate_evidence_digest": candidate_digest,
            "integration_candidate_receipt_path": str(integration_path),
            "integration_candidate_receipt_digest": integration_digest,
            "rvf_run": {"run_id": "rvf-prep-v3", "run_dir": str(run_dir)},
        },
        root=tmp_path / "prep",
        token="1234567890abcdef",
        now=rvf_prep_file.parse_timestamp("2026-07-21T00:00:00Z"),
        ttl_seconds=3600,
    )
    captured: dict[str, object] = {}
    original_prepare_run = prepare_review_run.prepare_run

    def fake_prepare_run(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {"run_id": "rvf-prep-v3", "run_dir": str(run_dir)}

    prepare_review_run.prepare_run = fake_prepare_run
    try:
        state = prepare_review_run.prepare_run_from_prep_file(
            record, timeout_seconds=5, base_dir=tmp_path / "runs"
        )
    finally:
        prepare_review_run.prepare_run = original_prepare_run

    assert state["status"] == "completed"
    extensions = captured["scope_contract_extensions"]
    assert isinstance(extensions, dict)
    assert extensions["review_kind"] == "base_drift_integration"
    assert extensions["base_ref"] == "main"
    assert extensions["remote_name"] == "origin"
    assert extensions["comparison_base_tip"] == head
    assert extensions["interaction_primary_paths"] == ["shared.txt"]
    assert extensions["bootstrap_receipt_digest"] == bootstrap_digest
    assert extensions["candidate_evidence_digest"] == candidate_digest
    assert extensions["integration_candidate_receipt_digest"] == integration_digest


def test_integration_candidate_receipt_recomputes_fixed_tip_delta_facts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "receipt-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "RVF Integration Receipt Test")
    git(repo, "config", "user.email", "rvf-integration-receipt@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-q", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    common_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    candidate_path = git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json"
    candidate_digest = write_canonical_artifact(candidate_path, {"schema_version": 1})
    receipt_path = (
        git_dir
        / "base-branch-sync/reviewed-task-handback-integration-candidate.json"
    )
    receipt = {
        "schema_name": "reviewed-task-handback-integration-candidate",
        "schema_version": 1,
        "status": "INTEGRATION_DELTA_RVF_REQUIRED",
        "risk_classification": "MEDIUM",
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "base_ref": "main",
        "remote_name": "origin",
        "reviewed_candidate_evidence_path": str(candidate_path),
        "reviewed_candidate_evidence_sha256": candidate_digest,
        "reviewed_base_tip": head,
        "base_tip_at_candidate": head,
        "reviewed_task_tip": head,
        "candidate_task_tip": head,
        "base_delta_paths": [],
        "base_delta_protected_paths": [],
        "task_change_paths": [],
        "interaction_primary_paths": [],
    }
    write_canonical_artifact(receipt_path, receipt)
    assert prepare_review_run.validated_integration_candidate_receipt(
        repo.resolve(), receipt_path
    )["candidate_task_tip"] == head

    receipt["base_delta_paths"] = ["forged.txt"]
    write_canonical_artifact(receipt_path, receipt)
    with pytest.raises(ValueError, match="delta facts"):
        prepare_review_run.validated_integration_candidate_receipt(
            repo.resolve(), receipt_path
        )
