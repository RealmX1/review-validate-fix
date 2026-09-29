#!/usr/bin/env python3
"""prepare review run 与 worktree bootstrap 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_prepare_review_run_and_command_lock',
    'test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths',
    'test_prepare_review_run_can_build_session_manifest_from_transcript',
    'test_prepare_review_run_requires_session_context',
    'test_prepare_review_run_writes_worktree_bootstrap',
    'test_prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore',
    'test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide',
    'test_prepare_review_run_scope_file_matches_metadata_through_symlink_state',
    'test_prepare_review_run_scope_contract_v3_records_base_drift_evidence',
    'test_prepare_review_run_blocks_opted_in_repo_until_local_base_is_absorbed',
    'test_prepare_review_run_rejects_invalid_local_base_ref',
    'test_prepare_from_prep_file_waits_for_local_base_bootstrap_with_recoverable_gate',
    'test_bootstrap_gate_reconciles_receipt_conflict_and_tamper_fail_closed',
    'test_bootstrap_gate_reports_artifact_loss_and_opt_in_withdrawal_without_raising',
    'test_bootstrap_gate_never_carries_environment_or_credential_values',
    'test_agent_cli_start_parks_on_bootstrap_then_resumes_same_run',
    'test_ups_stamps_waiting_bootstrap_onto_armed_kanban_followup_lock',
    'test_bootstrap_replay_after_receipt_reuses_the_same_run_end_to_end',
    'test_held_bootstrap_gate_persists_verified_receipt_digest_so_swapped_receipt_is_tampered',
    'test_bootstrap_gate_g0_rejects_forged_origin_run_lineage_and_lost_run_artifacts',
    'test_bootstrap_gate_recovers_when_local_base_advances_during_the_wait',
    'test_agent_cli_start_retry_resumes_after_local_base_advances_during_the_wait',
    'test_agent_cli_concurrent_bootstrap_resume_claims_prepare_exactly_once',
    'test_bootstrap_replay_hard_bounds_a_stuck_prepare_in_a_killable_subprocess',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_prepare_review_run_and_command_lock(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared review run\n",
        encoding="utf-8",
    )
    (repo / "secret.txt").write_text("hidden\n", encoding="utf-8")
    result = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
            "--primary-file",
            "tracked.txt",
            "--exclude-path-prefix",
            "secret.txt",
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["review_packet"]).exists()
    assert Path(payload["review_packet_metadata"]).exists()
    assert Path(payload["before_workspace_snapshot"]).exists()
    assert Path(payload["scope_of_work_file"]).exists()
    assert Path(payload["inputs_dir"]).exists()
    assert Path(payload["scope_contract"]).exists()
    assert payload["scope_contract"].endswith("artifacts/inputs/scope.contract.json")
    assert Path(payload["review_env_file"]).exists()
    assert Path(payload["review_agent_context_file"]).exists()
    assert payload["session_context"] == payload["scope_of_work_file"]
    assert payload["source_session_context"] == str(context.resolve())
    assert payload["session_context_provided"] is True
    assert payload["excluded_path_prefixes"] == ["secret.txt"]
    assert payload["review_env"]["RVF_REPO"] == str(repo.resolve())
    assert payload["review_env"]["RVF_INPUTS_DIR"] == payload["inputs_dir"]
    assert payload["review_env"]["RVF_SCOPE_CONTRACT"] == payload["scope_contract"]
    assert payload["review_env"]["RVF_SCOPE_OF_WORK"] == payload["scope_of_work_file"]
    assert payload["review_env"]["RVF_REVIEW_PACKET"] == payload["review_packet"]
    assert payload["review_env"]["RVF_WRITE_REVIEW_RESULT"].endswith("scripts/write_review_result.py")
    assert payload["review_env"]["RVF_CHECK_REVIEW_RESULT"].endswith("scripts/check_review_result.py")
    assert payload["review_env"]["RVF_REVIEW_RESULT"].endswith("artifacts/reviewers/reviewer/review-result.json")
    assert "${" not in payload["review_env"]["RVF_REVIEW_RESULT"]
    assert payload["review_env"]["CODEX_RVF_LOG_ROOT"] == str(Path(payload["run_dir"]).parents[1])
    assert payload["review_env"]["RVF_RUN_ID"] == payload["run_id"]
    assert payload["review_env"]["RVF_RUN_DIR"] == payload["run_dir"]
    assert payload["review_env"]["RVF_BACKEND"] == "manual"
    assert payload["rvf_backend"] == "manual"
    assert payload["rvf_state_phase"] == "prepare"
    assert payload["rvf_scope_contract_path"] == payload["scope_contract"]
    assert payload["rvf_review_packet_path"] == payload["review_packet"]
    review_env_text = Path(payload["review_env_file"]).read_text(encoding="utf-8")
    assert "export RVF_RUN_DIR=" in review_env_text
    assert "export CODEX_RVF_LOG_ROOT=" in review_env_text
    assert "export RVF_RUN_ID=" in review_env_text
    assert "export RVF_BACKEND=manual" in review_env_text
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in review_env_text
    assert 'export RVF_INPUTS_DIR="$RVF_ARTIFACTS_DIR/inputs"' in review_env_text
    assert 'export RVF_SCOPE_CONTRACT="$RVF_INPUTS_DIR/scope.contract.json"' in review_env_text
    assert 'export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"' in review_env_text
    assert 'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"' in review_env_text
    assert 'export RVF_REVIEW_RESULT="$RVF_ARTIFACTS_DIR/reviewers/${RVF_REVIEWER_ID:-reviewer}/review-result.json"' in review_env_text
    review_agent_context_text = Path(payload["review_agent_context_file"]).read_text(encoding="utf-8")
    assert payload["review_agent_context"] == review_agent_context_text
    assert "## RVF Generated Reviewer Context" in review_agent_context_text
    assert f". {payload['review_env_file']}" in review_agent_context_text
    assert "- scope contract: `$RVF_SCOPE_CONTRACT`" in review_agent_context_text
    assert "- scope-of-work: `$RVF_SCOPE_OF_WORK`" in review_agent_context_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in review_agent_context_text
    assert "- command lock wrapper: `$RVF_COMMAND_LOCK`" in review_agent_context_text
    assert "- review result writer: `$RVF_WRITE_REVIEW_RESULT`" in review_agent_context_text
    assert "- reviewer result artifact: `$RVF_REVIEW_RESULT`" in review_agent_context_text
    assert "Scope precedence: read `$RVF_SCOPE_CONTRACT` first" in review_agent_context_text
    assert "`primary_units` is non-empty" in review_agent_context_text
    assert "not the final scope contract" in review_agent_context_text
    assert payload["scope_of_work_file"] not in review_agent_context_text
    assert payload["review_packet"] not in review_agent_context_text
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert metadata["excluded_path_prefixes"] == ["secret.txt"]
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert "## Excluded Paths" in packet_text
    assert "- secret.txt" in packet_text
    assert "### secret.txt" not in packet_text
    contract = json.loads(Path(payload["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["version"] == 3
    assert contract["run_id"] == payload["run_id"]
    assert contract["scope_mode"] == "custom"
    assert contract["canonical_issues"] == []
    assert contract["primary_files"] == ["tracked.txt"]
    assert contract["fix_allowlist"] == ["tracked.txt"]
    assert contract["review_packet_path"] == payload["input_review_packet"]
    assert contract["start_snapshot_path"] == payload["input_before_workspace_snapshot"]
    assert contract["scope_hash"] == payload["scope_contract_payload"]["scope_hash"]
    assert contract["primary_units"] is None
    assert contract["tracker_lease_id"] is None
    assert contract["tracker_scope_hash"] is None


def test_prepare_review_run_scope_contract_v3_records_base_drift_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("review base drift integration\n", encoding="utf-8")
    reviewed_base_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "switch", "-q", "-c", "base-drift"], cwd=repo)
    (repo / "src").mkdir()
    (repo / "src/base.ts").write_text("export const base = true;\n", encoding="utf-8")
    (repo / "src/shared.ts").write_text("export const shared = true;\n", encoding="utf-8")
    (repo / "package.json").write_text('{"private":true}\n', encoding="utf-8")
    run(["git", "add", "src/base.ts", "src/shared.ts", "package.json"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base drift"], cwd=repo)
    base_tip_at_candidate = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "switch", "-q", "-c", "task", reviewed_base_tip], cwd=repo)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src/task.ts").write_text("export const task = true;\n", encoding="utf-8")
    (repo / "src/shared.ts").write_text("export const shared = true;\n", encoding="utf-8")
    run(["git", "add", "src/task.ts", "src/shared.ts"], cwd=repo)
    run(["git", "commit", "-q", "-m", "task change"], cwd=repo)
    reviewed_task_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "merge", "-q", "--no-ff", "base-drift", "-m", "integrate base drift"], cwd=repo)
    candidate_task_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    git_dir = Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).stdout.strip())
    common_dir = Path(
        run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo,
        ).stdout.strip()
    ).resolve()
    bootstrap_receipt = tmp_path / "local-base-into-task-worktree-bootstrap-receipt.json"
    bootstrap_receipt.write_text('{"schema_version":1}', encoding="utf-8")
    Path(f"{bootstrap_receipt}.sha256").write_text(
        hashlib.sha256(b'{"schema_version":1}').hexdigest() + "\n", encoding="ascii"
    )
    candidate_evidence = git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json"
    candidate_evidence.parent.mkdir(parents=True)
    candidate_evidence.write_text('{"schema_version":1}', encoding="utf-8")
    Path(f"{candidate_evidence}.sha256").write_text(
        hashlib.sha256(b'{"schema_version":1}').hexdigest() + "\n", encoding="ascii"
    )
    integration_receipt = (
        git_dir
        / "base-branch-sync/reviewed-task-handback-integration-candidate.json"
    )
    integration_receipt.parent.mkdir(parents=True)
    integration_receipt_payload = {
        "schema_name": "reviewed-task-handback-integration-candidate",
        "schema_version": 1,
        "status": "INTEGRATION_DELTA_RVF_REQUIRED",
        "risk_classification": "MEDIUM",
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "base_ref": "main",
        "remote_name": "origin",
        "reviewed_candidate_evidence_path": str(candidate_evidence.resolve()),
        "reviewed_candidate_evidence_sha256": hashlib.sha256(
            b'{"schema_version":1}'
        ).hexdigest(),
        "reviewed_base_tip": reviewed_base_tip,
        "base_tip_at_candidate": base_tip_at_candidate,
        "reviewed_task_tip": reviewed_task_tip,
        "candidate_task_tip": candidate_task_tip,
        "base_delta_paths": ["package.json", "src/base.ts", "src/shared.ts"],
        "base_delta_protected_paths": ["package.json"],
        "task_change_paths": ["src/shared.ts", "src/task.ts"],
        "interaction_primary_paths": ["src/shared.ts"],
    }
    encoded_integration_receipt = json.dumps(
        integration_receipt_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    integration_receipt.write_bytes(encoded_integration_receipt)
    Path(f"{integration_receipt}.sha256").write_text(
        hashlib.sha256(encoded_integration_receipt).hexdigest() + "\n",
        encoding="ascii",
    )

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
            "--review-kind",
            "base_drift_integration",
            "--comparison-base-tip",
            base_tip_at_candidate,
            "--reviewed-base-tip",
            base_tip_at_candidate,
            "--reviewed-task-tip",
            reviewed_task_tip,
            "--candidate-task-tip",
            candidate_task_tip,
            "--base-delta-path",
            "package.json",
            "--base-delta-path",
            "src/base.ts",
            "--base-delta-path",
            "src/shared.ts",
            "--task-change-path",
            "src/shared.ts",
            "--task-change-path",
            "src/task.ts",
            "--interaction-primary-path",
            "src/shared.ts",
            "--base-delta-protected-path",
            "package.json",
            "--bootstrap-receipt",
            str(bootstrap_receipt),
            "--candidate-evidence",
            str(candidate_evidence),
            "--integration-candidate-receipt",
            str(integration_receipt),
        ]
    )

    payload = json.loads(completed.stdout)
    contract = json.loads(Path(payload["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["version"] == 3
    assert contract["review_kind"] == "base_drift_integration"
    assert contract["comparison_base_tip"] == base_tip_at_candidate
    assert contract["reviewed_base_tip"] == base_tip_at_candidate
    assert contract["reviewed_task_tip"] == reviewed_task_tip
    assert contract["candidate_task_tip"] == candidate_task_tip
    assert contract["base_delta_paths"] == ["package.json", "src/base.ts", "src/shared.ts"]
    assert contract["task_change_paths"] == ["src/shared.ts", "src/task.ts"]
    assert contract["interaction_primary_paths"] == ["src/shared.ts"]
    assert contract["base_delta_protected_paths"] == ["package.json"]
    assert contract["bootstrap_receipt_path"] == str(bootstrap_receipt.resolve())
    assert len(contract["bootstrap_receipt_digest"]) == 64
    assert contract["candidate_evidence_path"] == str(candidate_evidence.resolve())
    assert len(contract["candidate_evidence_digest"]) == 64
    assert contract["integration_candidate_receipt_path"] == str(
        integration_receipt.resolve()
    )
    assert len(contract["integration_candidate_receipt_digest"]) == 64
    for key in (
        "review_kind",
        "comparison_base_tip",
        "base_delta_paths",
        "interaction_primary_paths",
        "bootstrap_receipt_digest",
        "candidate_evidence_digest",
    ):
        assert contract["canonical_scope"][key] == contract[key]

    symlink_receipt = tmp_path / "symlink-bootstrap-receipt.json"
    symlink_receipt.symlink_to(bootstrap_receipt)
    Path(f"{symlink_receipt}.sha256").write_text(
        hashlib.sha256(b'{"schema_version":1}').hexdigest() + "\n", encoding="ascii"
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "rejected-runs"),
            "--bootstrap-receipt",
            str(symlink_receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "regular file" in rejected.stderr

    pretty_receipt = tmp_path / "pretty-bootstrap-receipt.json"
    pretty_receipt.write_text('{\n  "schema_version": 1\n}\n', encoding="utf-8")
    Path(f"{pretty_receipt}.sha256").write_text(
        hashlib.sha256(b'{"schema_version":1}').hexdigest() + "\n", encoding="ascii"
    )
    noncanonical = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "noncanonical-runs"),
            "--bootstrap-receipt",
            str(pretty_receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert noncanonical.returncode != 0
    assert "canonical JSON bytes" in noncanonical.stderr

    malformed_sidecar_receipt = tmp_path / "malformed-sidecar-receipt.json"
    malformed_sidecar_receipt.write_text('{"schema_version":1}', encoding="utf-8")
    Path(f"{malformed_sidecar_receipt}.sha256").write_text(
        hashlib.sha256(b'{"schema_version":1}').hexdigest().upper(), encoding="ascii"
    )
    malformed_sidecar = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "malformed-sidecar-runs"),
            "--bootstrap-receipt",
            str(malformed_sidecar_receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_sidecar.returncode != 0
    assert "digest mismatch" in malformed_sidecar.stderr


def test_prepare_review_run_blocks_opted_in_repo_until_local_base_is_absorbed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "reset", "--hard", "HEAD"], cwd=repo)
    (repo / "new.txt").unlink()
    task_start = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "branch", "-m", "main"], cwd=repo)
    local_base_ref = "main"
    (repo / "AGENTS.md").write_text(
        "RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n",
        encoding="utf-8",
    )
    run(["git", "add", "AGENTS.md"], cwd=repo)
    run(["git", "commit", "-m", "base enables pre-review sync"], cwd=repo)
    local_base_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "checkout", "--detach", task_start], cwd=repo)
    (repo / "tracked.txt").write_text("task change\n", encoding="utf-8")
    context = tmp_path / "context.md"
    context.write_text("task requires review\n", encoding="utf-8")
    sacrificial_repo = init_repo(tmp_path / "sacrificial-repo")
    sacrificial_head = run(["git", "rev-parse", "HEAD"], cwd=sacrificial_repo).stdout.strip()
    poisoned_environment = {
        **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        "GIT_DIR": str(sacrificial_repo / ".git"),
        "GIT_WORK_TREE": str(sacrificial_repo),
        "GIT_COMMON_DIR": str(sacrificial_repo / ".git"),
        "GIT_INDEX_FILE": str(sacrificial_repo / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(sacrificial_repo / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(sacrificial_repo / ".git" / "objects"),
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=poisoned_environment,
    )

    assert completed.returncode == 3
    payload = json.loads(completed.stdout)
    assert payload == {
        "status": "bootstrap_required_before_rvf",
        "required_skill": "sync-local-base-into-task-worktree-only",
        "local_base_ref": local_base_ref,
        "local_base_tip": local_base_tip,
        "task_head": task_start,
    }
    assert run(["git", "rev-parse", "HEAD"], cwd=sacrificial_repo).stdout.strip() == sacrificial_head

    run(["git", "merge", "--no-edit", local_base_ref], cwd=repo)
    absorbed_task_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    absolute_git_dir = Path(
        run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).stdout.strip()
    )
    git_common_dir = Path(
        run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo
        ).stdout.strip()
    ).resolve()
    manifest = json.loads(
        run(
            [
                sys.executable,
                str(PREPARE_REVIEW_RUN.parent / "validation_result_cache.py"),
                "manifest",
                "--repo",
                str(repo),
            ]
        ).stdout
    )
    receipt_path = (
        absolute_git_dir
        / "base-branch-sync"
        / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt = {
        "schema_name": "local-base-into-task-worktree-bootstrap-receipt",
        "schema_version": 1,
        "base_task_merge_base_tip": task_start,
        "absorbed_base_tip": local_base_tip,
        "base_ref": local_base_ref,
        "remote_name": "origin",
        "task_tip_before_base_absorption": task_start,
        "task_tip_after_base_absorption": absorbed_task_tip,
        "base_delta_paths": ["AGENTS.md"],
        "task_change_paths": [],
        "interaction_primary_paths": [],
        "candidate_tree_oid": run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip(),
        **manifest,
        "git_common_directory_realpath": str(git_common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "boundary_status": "VERIFIED",
        "lock_status": "RELEASED",
        "target_repository_hooks_status": "DISABLED",
        "wip_status": "RESTORED_AND_DROPPED",
    }
    receipt_bytes = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    Path(f"{receipt_path}.sha256").write_text(
        hashlib.sha256(receipt_bytes).hexdigest() + "\n", encoding="ascii"
    )
    prepared = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "absorbed-runs"),
        ]
    )
    prepared_payload = json.loads(prepared.stdout)
    contract = json.loads(
        Path(prepared_payload["scope_contract"]).read_text(encoding="utf-8")
    )
    assert contract["comparison_base_tip"] == local_base_tip
    assert contract["reviewed_base_tip"] == local_base_tip
    assert contract["reviewed_task_tip"] == absorbed_task_tip
    assert contract["candidate_task_tip"] == absorbed_task_tip
    assert contract["bootstrap_receipt_path"] == str(receipt_path.resolve())
    assert len(contract["bootstrap_receipt_digest"]) == 64
    assert contract["base_delta_paths"] == ["AGENTS.md"]
    assert contract["task_change_paths"] == []
    assert contract["interaction_primary_paths"] == []


def test_prepare_review_run_rejects_invalid_local_base_ref(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("invalid ref must fail closed\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
            "--local-base-ref",
            "main^{commit}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "invalid local base ref" in completed.stderr

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contract-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ]
    )
    assert "locked" in locked.stdout


def test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
        ]
    )

    payload = json.loads(completed.stdout)
    contract = json.loads(Path(payload["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["scope_mode"] == "manual-all-uncommitted"
    assert contract["primary_files"] == ["new.txt", "tracked.txt"]
    assert contract["fix_allowlist"] == ["new.txt", "tracked.txt"]


def test_prepare_review_run_can_build_session_manifest_from_transcript(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared transcript-scoped review run\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)

    result = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--transcript",
            str(transcript),
            "--base-dir",
            str(tmp_path / "runs"),
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["session_manifest"]).exists()
    assert payload["session_manifest_provided"] is True
    assert payload["source_session_manifest"] == f"transcript:{transcript.resolve()}"
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert "## Session Manifest" in packet_text
    assert "background contents" not in packet_text


def test_prepare_review_run_requires_session_context(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--base-dir",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_prepare_review_run_writes_worktree_bootstrap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "tracked.txt").write_text("base\n\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "blank context"], cwd=repo)
    (repo / "tracked.txt").write_text("changed\n\n", encoding="utf-8")
    (repo / "owned.txt").write_text("owned untracked\n", encoding="utf-8")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "repo": str(repo),
        "owned_paths": ["tracked.txt", "owned.txt"],
        "owned_dirty_paths": ["tracked.txt", "owned.txt"],
        "unattributed_dirty_paths": ["background.txt"],
        "confidence": "high",
    }), encoding="utf-8")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    completed = run([
        sys.executable,
        str(PREPARE_REVIEW_RUN),
        "--repo",
        str(repo),
        "--session-context",
        str(context),
        "--session-manifest",
        str(manifest),
    ])
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    assert bootstrap["tracked_paths"] == ["tracked.txt"]
    # Full-dirty bootstrap: both session-owned and unattributed untracked files
    # are now copied; background.txt is part of the bootstrap snapshot too.
    assert sorted(item["path"] for item in bootstrap["untracked_files"]) == [
        "background.txt",
        "owned.txt",
    ]
    assert bootstrap["bootstrap_kind"] == "full-dirty"
    assert bootstrap["session_owned_dirty_paths"] == ["owned.txt", "tracked.txt"]
    assert bootstrap["unattributed_dirty_paths"] == ["background.txt"]
    assert bootstrap["unattributed_path_count"] == 1
    assert "tracked.txt" in Path(payload["worktree_bootstrap_patch"]).read_text(encoding="utf-8")
    clean = tmp_path / "clean"
    run(["git", "clone", "-q", str(repo), str(clean)], cwd=tmp_path)
    run(["git", "apply", "--check", str(payload["worktree_bootstrap_patch"])], cwd=clean)


def test_prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "tracked.txt").write_text("base\n\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "blank context"], cwd=repo)
    (repo / "tracked.txt").write_text("changed\n\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "build.js").write_text("compiled\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("vendor\n", encoding="utf-8")
    (repo / "owned.txt").write_text("real owned\n", encoding="utf-8")
    (repo / ".review-validate-fix-ignore").write_text("dist/\nnode_modules/\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "owned_paths": ["tracked.txt", "owned.txt"],
                "owned_dirty_paths": ["tracked.txt", "owned.txt"],
                "unattributed_dirty_paths": ["dist/build.js", "node_modules/lib.js"],
                "confidence": "high",
            }
        ),
        encoding="utf-8",
    )
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ]
    )
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    paths_in_bootstrap = set(bootstrap["owned_dirty_paths"])
    assert "dist/build.js" not in paths_in_bootstrap
    assert "node_modules/lib.js" not in paths_in_bootstrap
    assert "owned.txt" in paths_in_bootstrap
    assert "tracked.txt" in paths_in_bootstrap
    assert bootstrap["bootstrap_kind"] == "session-owned-only"
    # Both ignored paths land in ignored_dirty_paths for transparency.
    ignored = set(bootstrap.get("ignored_dirty_paths") or [])
    assert "dist/build.js" in ignored
    assert "node_modules/lib.js" in ignored


def test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "a").mkdir()
    (repo / "a" / "b.txt").write_text("slash path\n", encoding="utf-8")
    (repo / "a__b.txt").write_text("flat path\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "owned_paths": ["a/b.txt", "a__b.txt"],
                "owned_dirty_paths": ["a/b.txt", "a__b.txt"],
                "unattributed_dirty_paths": [],
                "confidence": "high",
            }
        ),
        encoding="utf-8",
    )
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ]
    )
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    stored_paths = [item["stored_path"] for item in bootstrap["untracked_files"]]
    assert [item["path"] for item in bootstrap["untracked_files"]] == ["a/b.txt", "a__b.txt"]
    assert len(set(stored_paths)) == 2

    clean = tmp_path / "clean"
    run(["git", "clone", "-q", str(repo), str(clean)], cwd=tmp_path)
    run(
        [
            sys.executable,
            str(APPLY_WORKTREE_BOOTSTRAP),
            "--metadata",
            str(payload["worktree_bootstrap"]),
            "--repo",
            str(clean),
        ]
    )
    assert (clean / "a" / "b.txt").read_text(encoding="utf-8") == "slash path\n"
    assert (clean / "a__b.txt").read_text(encoding="utf-8") == "flat path\n"


def test_prepare_review_run_scope_file_matches_metadata_through_symlink_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    symlink_state = tmp_path / "state-link"
    symlink_state.symlink_to(real_state, target_is_directory=True)
    env = os.environ.copy()
    env["CODEX_RVF_STATE_DIR"] = str(symlink_state)

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
        ],
        env=env,
    )

    payload = json.loads(completed.stdout)
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert str(real_state.resolve()) in payload["scope_of_work_file"]


# ---------------------------------------------------------------------------
# pre-review local-base bootstrap 的可恢复等待生命周期（gate + reconcile + 入口 A）
#
# 共用 fixture：`_bootstrap_opt_in_repo` 造一个「local base 声明了 pre-review 同步 marker、
# 但 task worktree 还没吸收它」的仓库；`_write_bootstrap_receipt` 按 canonical JSON 字节 +
# .sha256 sidecar 写出一份同步 skill 应当留下的 receipt（外部 my-ai-setup 的产物形状）。
# ---------------------------------------------------------------------------

def _bootstrap_opt_in_repo(tmp_path: Path, name: str = "repo") -> dict[str, object]:
    """建一个 opt-in 仓库：local base `main` 带同步 marker，task HEAD detach 在吸收之前。"""
    repo = init_repo(tmp_path / name)
    run(["git", "reset", "--hard", "HEAD"], cwd=repo)
    (repo / "new.txt").unlink()
    task_start = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "branch", "-m", "main"], cwd=repo)
    (repo / "AGENTS.md").write_text(
        "RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n",
        encoding="utf-8",
    )
    run(["git", "add", "AGENTS.md"], cwd=repo)
    run(["git", "commit", "-m", "base enables pre-review sync"], cwd=repo)
    local_base_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "checkout", "--detach", task_start], cwd=repo)
    (repo / "tracked.txt").write_text("task change\n", encoding="utf-8")
    return {
        "repo": repo,
        "task_start": task_start,
        "local_base_ref": "main",
        "local_base_tip": local_base_tip,
    }


def _advance_local_base(fixture: dict[str, object]) -> str:
    """模拟等待期内 local base 前移（别的 worktree 往本地 base 落提交 / `base-branch-sync` ff）。

    task worktree 是 detached HEAD，故用 plumbing 直接在 base ref 上造一个子提交并推进 ref：
    不切分支、不动工作树——正是真实场景里「另一个 worktree 提交到本地 main」的形状。
    同步更新 fixture 的 ``local_base_tip``，后续 receipt 便按**新** base 身份产出。
    """
    repo = fixture["repo"]
    base_ref = str(fixture["local_base_ref"])
    tip = run(["git", "rev-parse", base_ref], cwd=repo).stdout.strip()
    tree = run(["git", "rev-parse", f"{tip}^{{tree}}"], cwd=repo).stdout.strip()
    advanced = run(
        ["git", "commit-tree", tree, "-p", tip, "-m", "local base advances during the wait"],
        cwd=repo,
    ).stdout.strip()
    run(["git", "update-ref", f"refs/heads/{base_ref}", advanced], cwd=repo)
    fixture["local_base_tip"] = advanced
    return advanced


def _absorb_local_base(fixture: dict[str, object]) -> str:
    repo = fixture["repo"]
    run(["git", "merge", "--no-edit", str(fixture["local_base_ref"])], cwd=repo)
    return run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _write_bootstrap_receipt(fixture: dict[str, object], **overrides: object) -> Path:
    """写出同步 skill 应产出的 canonical bootstrap receipt（含 .sha256 sidecar）。"""
    repo = fixture["repo"]
    absorbed_task_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    absolute_git_dir = Path(
        run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).stdout.strip()
    )
    git_common_dir = Path(
        run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo
        ).stdout.strip()
    ).resolve()
    manifest = json.loads(
        run(
            [
                sys.executable,
                str(PREPARE_REVIEW_RUN.parent / "validation_result_cache.py"),
                "manifest",
                "--repo",
                str(repo),
            ]
        ).stdout
    )
    receipt_path = (
        absolute_git_dir
        / "base-branch-sync"
        / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_name": "local-base-into-task-worktree-bootstrap-receipt",
        "schema_version": 1,
        "base_task_merge_base_tip": fixture["task_start"],
        "absorbed_base_tip": fixture["local_base_tip"],
        "base_ref": fixture["local_base_ref"],
        "remote_name": "origin",
        "task_tip_before_base_absorption": fixture["task_start"],
        "task_tip_after_base_absorption": absorbed_task_tip,
        "base_delta_paths": ["AGENTS.md"],
        "task_change_paths": [],
        "interaction_primary_paths": [],
        "candidate_tree_oid": run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=repo
        ).stdout.strip(),
        **manifest,
        "git_common_directory_realpath": str(git_common_dir),
        "task_worktree_realpath": str(Path(str(repo)).resolve()),
        "boundary_status": "VERIFIED",
        "lock_status": "RELEASED",
        "target_repository_hooks_status": "DISABLED",
        "wip_status": "RESTORED_AND_DROPPED",
    }
    receipt.update(overrides)
    receipt_bytes = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    Path(f"{receipt_path}.sha256").write_text(
        hashlib.sha256(receipt_bytes).hexdigest() + "\n", encoding="ascii"
    )
    return receipt_path


def _bootstrap_prep_payload(fixture: dict[str, object], run_dir: Path, run_id: str) -> dict:
    return {
        "origin_session_id": "sess-bootstrap",
        "origin_repo": str(fixture["repo"]),
        "origin_cwd": str(fixture["repo"]),
        "target_flow": "flow-1-self-rising",
        "target_worktree": str(fixture["repo"]),
        "target_kanban_task_id": "task-bootstrap",
        "dispatch_origin": "stop_hook",
        "rvf_run": {"run_id": run_id, "run_dir": str(run_dir)},
    }


def _prepared_bootstrap_prep(tmp_path: Path, fixture: dict[str, object]):
    """跑一次 `prepare_run_from_prep_file`，返回 (prepare 模块, prep record, state)。"""
    prepare = load_prepare_review_run_module()
    prep_file = load_rvf_prep_file_module()
    run_dir = tmp_path / "runs" / "bootstrap-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    record = prep_file.write_prep_file(
        _bootstrap_prep_payload(fixture, run_dir, "bootstrap-run"),
        root=tmp_path / "prep",
    )
    state = prepare.prepare_run_from_prep_file(record, base_dir=tmp_path / "runs")
    return prepare, record, state


def test_prepare_from_prep_file_waits_for_local_base_bootstrap_with_recoverable_gate(
    tmp_path: Path,
) -> None:
    """T1/T5：prepare 检出 bootstrap → 独立 waiting status + 白名单 gate；不建 run、不派 reviewer。"""
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    prepare, record, state = _prepared_bootstrap_prep(tmp_path, fixture)

    # 独立等待态，绝不与真失败（status=failed）混同。
    assert state["status"] == gate_module.WAITING_STATUS
    assert state["status"] != "failed"
    assert state["required_skill"] == "sync-local-base-into-task-worktree-only"

    gate = gate_module.gate_of(state)
    assert gate is not None
    assert gate["phase"] == gate_module.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP
    assert gate["local_base_ref"] == fixture["local_base_ref"]
    assert gate["expected_local_base_oid"] == fixture["local_base_tip"]
    assert gate["task_worktree_realpath"] == str(Path(str(fixture["repo"])).resolve())
    assert gate["origin"]["kind"] == gate_module.BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP
    assert gate["origin"]["prep_token"] == record.token
    assert gate["origin"]["kanban_task_id"] == "task-bootstrap"

    # gate 落进了 prep 文件（持久化，可跨进程恢复），且 run 目录里没有被建出 scope/packet。
    reread = json.loads(record.path.read_text(encoding="utf-8"))
    assert gate_module.gate_of(reread["rvf_run"]["shared_workflow_state"]) == gate
    assert not (tmp_path / "runs" / "bootstrap-run" / "artifacts" / "inputs").exists()

    # 仍需 bootstrap → reconcile 判 still_waiting，并交出可复用的 run 身份。
    reconciliation = gate_module.reconcile_bootstrap_gate(gate)
    assert reconciliation.outcome == gate_module.OUTCOME_STILL_WAITING
    assert reconciliation.phase == gate_module.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP
    assert reconciliation.holds_gate is True
    assert reconciliation.resume_run_id == "bootstrap-run"

    # store 过期（token/锁 TTL 到期）只改 phase 名，不改结论：仍是可恢复等待，不是失败。
    interrupted = gate_module.reconcile_bootstrap_gate(gate, store_expired=True)
    assert interrupted.outcome == gate_module.OUTCOME_STILL_WAITING
    assert interrupted.phase == gate_module.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE


def test_bootstrap_gate_reconciles_receipt_conflict_and_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    """T8/T9：冲突（未 VERIFIED / 无 receipt / 索引未合）与篡改（digest、身份、base 漂移）分类。"""
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    _prepare, _record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    gate = gate_module.gate_of(state)
    repo = fixture["repo"]

    # ① 已吸收但同步 skill 没留 receipt → CONFLICT（手工 merge 无证据不得进审）。
    _absorb_local_base(fixture)
    conflict = gate_module.reconcile_bootstrap_gate(gate)
    assert conflict.outcome == gate_module.OUTCOME_CONFLICT
    assert conflict.phase == (
        gate_module.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP_CONFLICT_RESOLUTION
    )
    assert conflict.holds_gate is True

    # ② receipt 在场但边界未 VERIFIED（吸收未干净收尾）→ 仍是 CONFLICT，不是篡改。
    _write_bootstrap_receipt(fixture, boundary_status="CONFLICTED")
    unfinished = gate_module.reconcile_bootstrap_gate(gate)
    assert unfinished.outcome == gate_module.OUTCOME_CONFLICT
    assert unfinished.reason == "bootstrap_receipt_not_cleanly_finished"

    # ③ receipt 干净 → RECEIPT_VALID，交出 digest 与可复用 run 身份。
    receipt_path = _write_bootstrap_receipt(fixture)
    valid = gate_module.reconcile_bootstrap_gate(gate)
    assert valid.outcome == gate_module.OUTCOME_RECEIPT_VALID
    assert valid.phase == gate_module.PHASE_REVIEWING
    assert valid.resumable is True
    assert len(valid.receipt_digest) == 64

    # ④ digest 与 gate 记录不符（内容被换）→ TAMPERED，不进 review、gate 不清。
    tampered_gate = {**gate, "bootstrap_receipt_digest": "0" * 64}
    tampered = gate_module.reconcile_bootstrap_gate(tampered_gate)
    assert tampered.outcome == gate_module.OUTCOME_TAMPERED
    assert tampered.holds_gate is True

    # ⑤ receipt 身份被改（换 common-dir）→ 全套 fail-closed 校验拦下，判 TAMPERED。
    _write_bootstrap_receipt(fixture, git_common_directory_realpath=str(tmp_path / "elsewhere"))
    assert (
        gate_module.reconcile_bootstrap_gate(gate).outcome == gate_module.OUTCOME_TAMPERED
    )
    _write_bootstrap_receipt(fixture)  # 复原

    # ⑥ gate 记录的 worktree 身份变了（指到别的仓）→ TAMPERED，绝不复用。
    other = init_repo(tmp_path / "other-repo")
    moved_gate = {**gate, "task_worktree_realpath": str(Path(str(other)).resolve())}
    assert (
        gate_module.reconcile_bootstrap_gate(moved_gate).outcome
        == gate_module.OUTCOME_TAMPERED
    )

    # ⑦ local base 前移到另一个 tip **不是**篡改：base 身份变化是等待期的常规事件，证据本身
    # （receipt 对**当前** base 的全套校验）才是判据。曾经在此判 TAMPERED（∈ HOLD_OUTCOMES）
    # 会让这一轮永久无法恢复，详见
    # `test_bootstrap_gate_recovers_when_local_base_advances_during_the_wait`。
    stale_base_gate = {**gate, "expected_local_base_oid": "1" * 40}
    stale_base = gate_module.reconcile_bootstrap_gate(stale_base_gate)
    assert stale_base.outcome == gate_module.OUTCOME_RECEIPT_VALID
    assert stale_base.local_base_identity_changed is True
    assert receipt_path.exists()


def test_bootstrap_gate_reports_artifact_loss_and_opt_in_withdrawal_without_raising(
    tmp_path: Path,
) -> None:
    """T10/T11：工件丢失 / opt-in 撤销 / 非 opt-in 仓 —— 显式诊断 + 安全重来，绝不抛。"""
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    _prepare, _record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    gate = gate_module.gate_of(state)

    # ① run 工件被清 → ARTIFACT_LOST（可恢复重来），带显式诊断。
    import shutil as _shutil

    _shutil.rmtree(tmp_path / "runs" / "bootstrap-run")
    lost = gate_module.reconcile_bootstrap_gate(gate)
    assert lost.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert lost.reason == "rvf_run_artifacts_missing"
    assert lost.holds_gate is False
    assert "bootstrap-run" in lost.diagnostic

    # ② gate 自身身份字段缺失 → ARTIFACT_LOST，绝不猜。
    broken = {**gate, "expected_local_base_oid": None}
    assert (
        gate_module.reconcile_bootstrap_gate(broken).reason == "gate_self_identity_incomplete"
    )

    # ③ task worktree 整个没了 → ARTIFACT_LOST，不抛。
    gone = {**gate, "task_worktree_realpath": str(tmp_path / "never-existed")}
    assert gate_module.reconcile_bootstrap_gate(gone).reason == "task_worktree_missing"

    # ④ 非 opt-in 仓（base 的 AGENTS.md 无 marker）→ 无 gate 可言；reconcile 判 opt-in 撤销。
    # `init_repo` 的模板仓由 `git init` 造出，分支名取决于外部 `init.defaultBranch`
    # （清洗过 GIT_CONFIG_GLOBAL/GIT_CONFIG_NOSYSTEM 的调用环境下是 master）。这里必须把分支名
    # 显式对齐到 gate 的 local_base_ref，否则用例会落进「base ref 解析失败」分支，验不到本意的
    # 「base ref 解析得到、但 base 提交无 marker → opt-in 撤销」语义。
    plain = init_repo(tmp_path / "plain-repo")
    run(["git", "branch", "-m", str(gate["local_base_ref"])], cwd=plain)
    prepare = load_prepare_review_run_module()
    assert prepare.pre_review_base_sync_state(plain, str(gate["local_base_ref"])) is None
    plain_gate = {
        **gate,
        "task_worktree_realpath": str(Path(str(plain)).resolve()),
        "git_common_directory_realpath": str(
            Path(
                run(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=plain,
                ).stdout.strip()
            ).resolve()
        ),
        # G0 会校验「检测时刻的 task HEAD 在**当前** worktree 里仍可解析」。这枚合成 gate 被
        # 指到了另一个仓库，若沿用原仓的 HEAD 就会先在那一关落地，验不到本意的 opt-in 撤销。
        "task_head_at_detection": run(["git", "rev-parse", "HEAD"], cwd=plain).stdout.strip(),
    }
    withdrawn = gate_module.reconcile_bootstrap_gate(plain_gate)
    assert withdrawn.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert withdrawn.reason == "pre_review_base_sync_opt_in_withdrawn"


def test_bootstrap_gate_never_carries_environment_or_credential_values(
    tmp_path: Path,
) -> None:
    """T13：gate 只带白名单字段，且不夹带任何 env 值（含同步 skill 的契约 env）。"""
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    poisoned = {
        gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV: "RVF",
        "ANTHROPIC_API_KEY": "sk-should-never-appear-in-gate",
        "CLAUDE_CODE_ROUTING_OVERRIDE": "personal-routing-should-never-appear",
    }
    previous = {name: os.environ.get(name) for name in poisoned}
    os.environ.update(poisoned)
    try:
        _prepare, _record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    gate = gate_module.gate_of(state)
    assert set(gate) <= gate_module.BOOTSTRAP_GATE_FIELDS
    assert set(gate["origin"]) <= gate_module.BOOTSTRAP_GATE_ORIGIN_FIELDS
    serialized = json.dumps(gate, ensure_ascii=False)
    for value in poisoned.values():
        assert value not in serialized
    # 契约 env 的**名字**只出现在给 agent 的指令里，绝不出现在落盘 gate 里。
    assert gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV not in serialized
    steps = "\n".join(gate_module.bootstrap_waiting_agent_steps(gate))
    assert gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV in steps
    assert "sync-local-base-into-task-worktree-only" in steps


def test_agent_cli_start_parks_on_bootstrap_then_resumes_same_run(tmp_path: Path) -> None:
    """T2/T3/T4：入口 A 全链路——等待态落 gate + 补 run 锚点、retry 期间不重跑 prepare、
    token 过 TTL 仍可复活、receipt 有效后复用**同一 run** 恢复且只 prepare 成功一次。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    repo = str(fixture["repo"])
    roots = _agent_cli_roots(cli, tmp_path)
    token = _mint_agent_decision_token(dts, roots, worktree=repo)

    initiation_run_dir = tmp_path / "runs" / "initiation"
    initiation_run_dir.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0, "resume": []}

    def _pipeline(
        *, token_record, repo, session_id, transcript_path, now, timeout_seconds=60.0,
        resume_run_id=None, resume_run_dir=None,
        parent_attempt_run_id=None, parent_attempt_run_dir=None,
    ):
        calls["n"] += 1
        calls["resume"].append((resume_run_id, resume_run_dir))
        state = {
            "status": gate_module.WAITING_STATUS,
            "bootstrap_gate": gate_module.build_bootstrap_gate(
                Path(repo),
                {
                    "required_skill": "sync-local-base-into-task-worktree-only",
                    "local_base_ref": fixture["local_base_ref"],
                    "local_base_tip": fixture["local_base_tip"],
                    "task_head": run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
                },
                origin_kind=gate_module.BOOTSTRAP_ORIGIN_DECISION_TOKEN,
                rvf_run_id="initiation",
                rvf_run_dir=str(initiation_run_dir),
            ),
            "run_id": None,
            "run_dir": None,
            "artifacts": {},
            "error": None,
            "initiation_run_id": "initiation",
            "initiation_run_dir": str(initiation_run_dir),
        }
        if calls["bootstrapped"]:
            state = {
                "status": "completed", "run_id": resume_run_id or "initiation",
                "run_dir": resume_run_dir or str(initiation_run_dir),
                "artifacts": {}, "error": None, "bootstrap_gate": None,
                "initiation_run_id": "initiation",
                "initiation_run_dir": str(initiation_run_dir),
            }
        return state

    calls["bootstrapped"] = False

    # ① 首次 start 撞 bootstrap → 专属退出码 + 等待态，token 仍 acknowledged（可 retry）。
    first = cli.run_start(
        token=token, reason="首轮发起", repo=repo, roots=roots, prepare_pipeline=_pipeline
    )
    assert first.status == "bootstrap_required"
    assert first.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED
    assert first.token_state == dts.STATE_ACKNOWLEDGED
    assert first.detail["phase"] == gate_module.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP
    # 模型可见的 self-injection checkpoint：调 skill → 解冲突 → 验 receipt → retry 恢复。
    steps = "\n".join(first.next_steps)
    assert "sync-local-base-into-task-worktree-only" in steps
    assert gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV in steps
    assert f" start --token {token} --retry" in steps

    stored = json.loads((roots.token_root / f"{token}.json").read_text(encoding="utf-8"))
    assert gate_module.is_waiting_gate(stored["bootstrap_gate"])
    assert stored["bootstrap_gate"]["origin"]["decision_token"] == token
    # run 锚点必须在场：没有它，900s token TTL 会在一次大 base 吸收里把整轮决策熬死。
    assert stored["run_anchor_dir"] == str(initiation_run_dir)

    # ② bootstrap 还没做完就 retry → 维持等待，**不重跑 prepare**（不新开 run）。
    second = cli.run_start(
        token=token, reason="重试", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert second.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED
    assert calls["n"] == 1

    # ③ token 过 TTL（等待期常态）：锚点在场 → 复活同一枚 token，不重铸、状态不变。
    expired_payload = json.loads((roots.token_root / f"{token}.json").read_text(encoding="utf-8"))
    expired_payload["expires_at"] = "2000-01-01T00:00:00Z"
    (roots.token_root / f"{token}.json").write_text(
        json.dumps(expired_payload, ensure_ascii=False), encoding="utf-8"
    )
    revived = cli.run_start(
        token=token, reason="中断后恢复", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert revived.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED
    assert revived.detail["phase"] == gate_module.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE
    after_revive = json.loads((roots.token_root / f"{token}.json").read_text(encoding="utf-8"))
    assert after_revive["revived_count"] == 1
    assert after_revive["state"] == dts.STATE_ACKNOWLEDGED
    assert len(list(roots.token_root.glob("*.json"))) == 1  # 无第二枚 token

    # ④ 跑完同步 skill、留下可信 receipt → retry 复用**同一 run** 恢复，gate 退场。
    _absorb_local_base(fixture)
    _write_bootstrap_receipt(fixture)
    calls["bootstrapped"] = True
    resumed = cli.run_start(
        token=token, reason="bootstrap 完成后恢复本轮", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert resumed.status == "ok"
    assert resumed.exit_code == cli.EXIT_OK
    assert calls["resume"][-1] == ("initiation", str(initiation_run_dir))
    assert resumed.detail["run_id"] == "initiation"
    final = json.loads((roots.token_root / f"{token}.json").read_text(encoding="utf-8"))
    assert "bootstrap_gate" not in final
    assert final["state"] == dts.STATE_ACKNOWLEDGED  # reviewer 仍须显式 dispatch，start 不派
    assert calls["n"] == 2  # 整轮只有首次探测 + 恢复这两次 prepare


def test_ups_stamps_waiting_bootstrap_onto_armed_kanban_followup_lock(tmp_path: Path) -> None:
    """T5/T12（入口 B 的 UPS 侧）：prepare 返回等待态 → 同一把已 arm 的锁被改写为
    ``state=waiting_bootstrap`` + gate，并给出**模型可见**的 self-injection checkpoint。

    借的是 UPS 的时序：锁在同一进程里先 arm、prepare 才跑，所以 gate 能就地写回同一把锁——
    不新开 marker 族（锁根下始终只有这一把），也不必让 Stop 读侧回头去查 prep 文件。
    """
    ups = load_rvf_user_prompt_submit_module()
    lock_module = load_kanban_followup_lock_module()
    gate_module = load_rvf_bootstrap_gate_module()
    prep_file = load_rvf_prep_file_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)

    lock_root = tmp_path / "locks"
    previous_root = os.environ.get(lock_module.LOCK_ROOT_ENV)
    os.environ[lock_module.LOCK_ROOT_ENV] = str(lock_root)
    try:
        run_dir = tmp_path / "runs" / "bootstrap-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        record = prep_file.write_prep_file(
            _bootstrap_prep_payload(fixture, run_dir, "bootstrap-run"),
            root=tmp_path / "prep",
        )
        event = {"session_id": "sess-bootstrap", "cwd": str(fixture["repo"])}

        # ① 投递确认即 arm（state=delivered_awaiting_agent_decision、无 gate）。
        armed = ups.arm_kanban_followup_lock_on_delivery(
            event, record, prep_root=tmp_path / "prep"
        )
        assert armed is not None
        assert (
            json.loads(Path(armed).read_text(encoding="utf-8"))["state"]
            == lock_module.STATE_DELIVERED_AWAITING_AGENT_DECISION
        )

        # ② prepare 判定等待 → 就地写回同一把锁。
        prepare = load_prepare_review_run_module()
        state = prepare.prepare_run_from_prep_file(record, base_dir=tmp_path / "runs")
        assert state["status"] == gate_module.WAITING_STATUS
        stamped_path = ups.stamp_kanban_followup_bootstrap_gate(
            event, record, state, prep_root=tmp_path / "prep"
        )
        assert stamped_path == armed  # 同一把锁，不是新开一把

        marker = json.loads(Path(armed).read_text(encoding="utf-8"))
        # gate 已盖上，但 state 仍是 awaiting：「收没收到这条 trigger」这一维不能被 gate 抹掉。
        # 升格到 waiting_bootstrap 由 Stop 读侧在确认 trigger 在祖先链上时做。
        assert marker["state"] == lock_module.STATE_DELIVERED_AWAITING_AGENT_DECISION
        promoted_state = lock_module.promote_marker_out_of_awaiting_agent_decision(
            {**marker, "_marker_path": armed}, reason="test_promote"
        )
        assert promoted_state == lock_module.STATE_WAITING_BOOTSTRAP
        marker = json.loads(Path(armed).read_text(encoding="utf-8"))
        assert marker["state"] == lock_module.STATE_WAITING_BOOTSTRAP
        assert gate_module.is_waiting_gate(marker["bootstrap_gate"])
        assert marker["bootstrap_gate"]["origin"]["prep_token"] == record.token
        assert marker["run_id"] == "bootstrap-run"
        # 锁根下始终只有这一把：等待态不得派生第二把锁 / 第二轮 followup。
        assert len(list(lock_root.glob("*.json"))) == 1

        # ③ 模型可见的 self-injection checkpoint（additionalContext，非 user-only systemMessage）。
        context = ups._bootstrap_waiting_additional_context_text(marker["bootstrap_gate"])
        assert "sync-local-base-into-task-worktree-only" in context
        assert gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV in context
        assert "不要**输出 handoff" in context
        # user-facing 那一行则明说「本轮 review 尚未开始」，不再谎称「派发已跑但未就绪」。
        notice = ups._trigger_system_message(
            kind="waiting_local_base_bootstrap", status=gate_module.WAITING_STATUS
        )
        assert "本轮 review 尚未开始" in notice
        assert gate_module.SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV not in notice
    finally:
        if previous_root is None:
            os.environ.pop(lock_module.LOCK_ROOT_ENV, None)
        else:
            os.environ[lock_module.LOCK_ROOT_ENV] = previous_root


def test_held_bootstrap_gate_persists_verified_receipt_digest_so_swapped_receipt_is_tampered(
    tmp_path: Path,
) -> None:
    """`bootstrap_receipt_digest_changed` 必须在**生产写侧**可达（RVF-002 回归）。

    `build_bootstrap_gate` 造 gate 时 receipt 尚不存在，digest 恒为 None。若没有任何写侧在
    「receipt 首次校验通过、但本轮仍要继续持有等待态」时把 digest 落盘，那条篡改判据永远只在
    手工构造的 gate 上成立——生产路径每一轮都只做身份校验，一份**被替换但仍能通过身份校验**的
    receipt 会被照单全收（原测试的绿是假绿：它自己造了个带 digest 的 gate）。

    这里走入口 A 的真实路径：receipt 有效 → prepare 失败（gate 继续被持有）→ 断言 token payload
    里的 gate 已带上 digest → 换一份内容不同但身份校验仍通过的 receipt → 下一轮判 tampered。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    repo = str(fixture["repo"])
    roots = _agent_cli_roots(cli, tmp_path)
    token = _mint_agent_decision_token(dts, roots, worktree=repo)

    initiation_run_dir = tmp_path / "runs" / "initiation"
    initiation_run_dir.mkdir(parents=True, exist_ok=True)
    pipeline_state = {"absorbed": False}

    def _pipeline(
        *, token_record, repo, session_id, transcript_path, now, timeout_seconds=60.0,
        resume_run_id=None, resume_run_dir=None,
        parent_attempt_run_id=None, parent_attempt_run_dir=None,
    ):
        if pipeline_state["absorbed"]:
            # 吸收已完成、receipt 有效，但 prepare 本身失败 → token 保持 acknowledged、gate 继续
            # 被持有。这正是「继续持有等待态」的生产路径，digest 必须在此之前已被写回。
            return {"status": "failed", "error": "stub: prepare 失败，本轮继续持有等待态"}
        return {
            "status": gate_module.WAITING_STATUS,
            "bootstrap_gate": gate_module.build_bootstrap_gate(
                Path(repo),
                {
                    "required_skill": "sync-local-base-into-task-worktree-only",
                    "local_base_ref": fixture["local_base_ref"],
                    "local_base_tip": fixture["local_base_tip"],
                    "task_head": run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
                },
                origin_kind=gate_module.BOOTSTRAP_ORIGIN_DECISION_TOKEN,
                rvf_run_id="initiation",
                rvf_run_dir=str(initiation_run_dir),
            ),
            "run_id": None,
            "run_dir": None,
            "artifacts": {},
            "error": None,
            "initiation_run_id": "initiation",
            "initiation_run_dir": str(initiation_run_dir),
        }

    token_path = roots.token_root / f"{token}.json"

    # ① 首轮撞 bootstrap：gate 落盘，digest 此刻**本就**该是 None（receipt 还不存在）。
    first = cli.run_start(
        token=token, reason="首轮发起", repo=repo, roots=roots, prepare_pipeline=_pipeline
    )
    assert first.status == "bootstrap_required"
    assert json.loads(token_path.read_text(encoding="utf-8"))["bootstrap_gate"][
        "bootstrap_receipt_digest"
    ] is None

    # ② 吸收完成 + 可信 receipt，但这一轮 prepare 失败 → 等待态继续被持有。
    _absorb_local_base(fixture)
    receipt_path = _write_bootstrap_receipt(fixture)
    original_digest = load_prepare_review_run_module().canonical_json_artifact_identity(
        receipt_path
    )[1]
    pipeline_state["absorbed"] = True
    held = cli.run_start(
        token=token, reason="bootstrap 完成后恢复本轮", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert held.status == "start_failed"

    # ③ 校验通过的 receipt 路径/digest 已被**持久化回**仍在等待的 gate（缺了它 ④ 永远不可达）。
    held_gate = json.loads(token_path.read_text(encoding="utf-8"))["bootstrap_gate"]
    assert gate_module.is_waiting_gate(held_gate)
    assert held_gate["bootstrap_receipt_digest"] == original_digest
    assert len(original_digest) == 64
    assert Path(held_gate["bootstrap_receipt_path"]) == receipt_path

    # ④ 换一份**内容不同、但身份校验仍全部通过**的 receipt：`remote_name` 记录的是吸收来源，
    # 却不在 `validated_bootstrap_receipt` 的校验集里——正是「只有 digest 能识破」的那类替换。
    swapped = _write_bootstrap_receipt(fixture, remote_name="swapped-remote")
    swapped_digest = load_prepare_review_run_module().canonical_json_artifact_identity(
        swapped
    )[1]
    assert swapped_digest != original_digest
    # 换过的 receipt 仍能通过 fail-closed 全套身份校验——所以只有 digest 能识破它。
    prepare = load_prepare_review_run_module()
    state = prepare.pre_review_base_sync_state(Path(repo), fixture["local_base_ref"])
    assert prepare.validated_bootstrap_receipt(Path(repo), state, swapped)

    tampered = gate_module.reconcile_bootstrap_gate(held_gate)
    assert tampered.outcome == gate_module.OUTCOME_TAMPERED
    assert tampered.reason == "bootstrap_receipt_digest_changed"
    assert tampered.holds_gate is True

    # ⑤ 入口 A 下一轮 retry 也据此拒绝进审：仍是等待态（可 retry），而不是照单全收去 prepare。
    refused = cli.run_start(
        token=token, reason="再次重试", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert refused.status == "bootstrap_required"
    assert refused.detail["bootstrap_outcome"] == gate_module.OUTCOME_TAMPERED
    assert refused.detail["bootstrap_reason"] == "bootstrap_receipt_digest_changed"


def test_bootstrap_replay_after_receipt_reuses_the_same_run_end_to_end(tmp_path: Path) -> None:
    """T7 真链路：吸收完成 + receipt 可信后，按 gate 记录的 prep 重放 prepare —— **复用同一个
    run_id/run_dir** 跑完全套（不 stub 重放），且不会派生第二个 run 目录。

    这是「恢复不得变成第二轮独立 RVF」的端到端证据：prep 的 ``rvf_run`` 里钉死的 run 身份被
    原样透传给 prepare_run，重放后 shared_workflow_state 变 completed、artifacts 落在原 run 下。
    """
    gate_module = load_rvf_bootstrap_gate_module()
    prep_file = load_rvf_prep_file_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)

    prep_root = tmp_path / "prep"
    previous_prep_root = os.environ.get(prep_file.ENV_PREP_ROOT)
    os.environ[prep_file.ENV_PREP_ROOT] = str(prep_root)
    try:
        _prepare, record, state = _prepared_bootstrap_prep(tmp_path, fixture)
        assert state["status"] == gate_module.WAITING_STATUS
        gate = gate_module.gate_of(state)

        # 跑同步 skill（这里直接做等价的吸收）并留下可信 receipt。
        _absorb_local_base(fixture)
        _write_bootstrap_receipt(fixture)
        reconciliation = gate_module.reconcile_bootstrap_gate(gate)
        assert reconciliation.outcome == gate_module.OUTCOME_RECEIPT_VALID
        assert reconciliation.resume_run_id == "bootstrap-run"

        replay = gate_module.replay_prepare_for_gate(gate, timeout_seconds=180.0)
        assert replay.get("status") == "completed", replay
        assert replay["run_id"] == "bootstrap-run"  # 同一个 run，不是新开的第二轮
        assert Path(replay["run_dir"]) == tmp_path / "runs" / "bootstrap-run"
        assert gate_module.gate_of(replay) is None  # 进入 REVIEWING，gate 退场

        # prep 上的 shared_workflow_state 已就地推进为 completed（下一次读到即幂等短路）。
        reread = json.loads(record.path.read_text(encoding="utf-8"))
        assert reread["rvf_run"]["shared_workflow_state"]["status"] == "completed"
        # runs 根下只有这一个 run 目录：整轮从等待到恢复只用了一个 run。
        assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == ["bootstrap-run"]
    finally:
        if previous_prep_root is None:
            os.environ.pop(prep_file.ENV_PREP_ROOT, None)
        else:
            os.environ[prep_file.ENV_PREP_ROOT] = previous_prep_root


def test_bootstrap_gate_g0_rejects_forged_origin_run_lineage_and_lost_run_artifacts(
    tmp_path: Path,
) -> None:
    """RVF-006①：G0 自证必须覆盖 origin 归属 / run 血缘 / 检测时刻 HEAD，且「receipt 有效但
    在途工件已丢失」必须判 `artifact_lost`——绝不能靠一枚被伪造/被截断的 gate 拿到 `receipt_valid`。

    修复前 G0 只校验 5 个字段（skill / base ref / base oid / common-dir / worktree），且
    ``run_dir`` 存在性只写在 G3；base 一旦已吸收，函数在 G2 就 return 了，那条检查根本到不了。
    """
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    _prepare, _record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    gate = gate_module.gate_of(state)
    run_dir = tmp_path / "runs" / "bootstrap-run"

    # 基线：吸收完成 + 可信 receipt 后，这枚**未被动过**的 gate 必须仍判 receipt_valid。
    # 没有它，下面每一条负例都可能只是因为整条路径退化成了红而“通过”。
    _absorb_local_base(fixture)
    _write_bootstrap_receipt(fixture)
    assert (
        gate_module.reconcile_bootstrap_gate(gate).outcome
        == gate_module.OUTCOME_RECEIPT_VALID
    )

    def _reconciled(**overrides: object):
        return gate_module.reconcile_bootstrap_gate({**gate, **overrides})

    # ① 伪造 origin 归属：恢复动作完全由 origin 决定（回哪个 token / 哪把 followup 锁），
    # kind 不认识、或夹带白名单外字段一律 tampered。
    forged_kind = _reconciled(origin={**gate["origin"], "kind": "kanban_followup_admin"})
    assert forged_kind.outcome == gate_module.OUTCOME_TAMPERED
    assert forged_kind.reason == "bootstrap_gate_origin_untrusted"
    assert forged_kind.outcome != gate_module.OUTCOME_RECEIPT_VALID
    smuggled = _reconciled(
        origin={**gate["origin"], "authorization": "sk-should-never-be-a-gate-field"}
    )
    assert smuggled.outcome == gate_module.OUTCOME_TAMPERED
    assert smuggled.reason == "bootstrap_gate_origin_untrusted"
    missing_origin = _reconciled(origin=None)
    assert missing_origin.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert missing_origin.reason == "gate_self_identity_incomplete"

    # ② 篡改 run 血缘：run 目录自述的身份（summary.json）是唯一可信锚点。
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": "bootstrap-run", "status": "running"}, ensure_ascii=False),
        encoding="utf-8",
    )
    forged_run_id = _reconciled(rvf_run_id="someone-elses-run")
    assert forged_run_id.outcome == gate_module.OUTCOME_TAMPERED
    assert forged_run_id.reason == "bootstrap_gate_run_lineage_incoherent"
    assert forged_run_id.outcome != gate_module.OUTCOME_RECEIPT_VALID
    self_parent = _reconciled(
        parent_attempt_run_id="bootstrap-run",
        parent_attempt_run_dir=str(tmp_path / "runs" / "earlier-attempt"),
    )
    assert self_parent.outcome == gate_module.OUTCOME_TAMPERED
    assert self_parent.reason == "bootstrap_gate_run_lineage_incoherent"
    half_anchor = _reconciled(rvf_run_dir=None)
    assert half_anchor.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert half_anchor.reason == "gate_run_lineage_incomplete"

    # ③ 检测时刻的 task HEAD：缺失 = 身份不全；形状不是 oid = 伪造；解析不到 = 依据已消失。
    assert _reconciled(task_head_at_detection=None).reason == "gate_self_identity_incomplete"
    forged_head = _reconciled(task_head_at_detection="not-a-git-object-id")
    assert forged_head.outcome == gate_module.OUTCOME_TAMPERED
    assert forged_head.reason == "task_head_at_detection_invalid"
    unresolvable_head = _reconciled(task_head_at_detection="0" * 40)
    assert unresolvable_head.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert unresolvable_head.reason == "task_head_at_detection_unresolvable"

    # ④ receipt 仍然有效，但等待期的 run 工件被清掉 → artifact_lost（修复前照判 receipt_valid，
    # 调用方会拿着一个已经不存在的 run 身份去恢复）。放最后，因为它真的删目录。
    import shutil as _shutil

    _shutil.rmtree(run_dir)
    lost = gate_module.reconcile_bootstrap_gate(gate)
    assert lost.outcome == gate_module.OUTCOME_ARTIFACT_LOST
    assert lost.reason == "rvf_run_artifacts_missing"
    assert lost.resumable is False


def test_bootstrap_gate_recovers_when_local_base_advances_during_the_wait(
    tmp_path: Path,
) -> None:
    """RVF-008①：等待期内 local base 前移必须**可恢复**，不得被判 TAMPERED 而永久挂死。

    `expected_local_base_oid` 只在 `build_bootstrap_gate` 写过一次、全仓无刷新写侧；而等待期内
    base 前移（别的 worktree 往本地 base 落提交、`base-branch-sync` fast-forward）是常规事件。
    修复前 G1 在「base 已吸收但 tip != gate 记录」时返回 `_tampered(local_base_identity_changed)`，
    而 TAMPERED ∈ HOLD_OUTCOMES ——agent 照 next_steps 重跑同步 skill、吸收新 tip、留下能通过
    `validated_bootstrap_receipt` 全套校验的 receipt，判定仍恒等于「base 身份变了」：prepare 再也
    不被调用、gate 永不摘除、入口 A 三次 `start --retry` 全部 exit 6，该轮**永久无法进入 review**。

    修复后 base 身份改由证据判定：`validated_bootstrap_receipt` 要求 ``absorbed_base_tip`` 等于
    **当前** base tip，故旧 epoch 的 receipt 照样 fail-closed（④），digest 判据在 base 未变时也
    照旧 fail-closed（⑤）——放行的只有「新 epoch + 新合法 receipt」这条真实恢复路径。
    """
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    _prepare, _record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    gate = gate_module.gate_of(state)
    armed_base_oid = gate["expected_local_base_oid"]

    # ① base 前移但 task 还没吸收 → 仍是 still_waiting（不是 tampered），且诊断指向**当前** tip：
    # 拿 armed 那一刻的旧 oid 指路会让 agent 去吸收一枚早就不是 base 尖端的提交。
    advanced = _advance_local_base(fixture)
    assert advanced != armed_base_oid
    waiting = gate_module.reconcile_bootstrap_gate(gate)
    assert waiting.outcome == gate_module.OUTCOME_STILL_WAITING
    assert waiting.outcome != gate_module.OUTCOME_TAMPERED
    assert waiting.local_base_identity_changed is True
    assert waiting.observed_local_base_oid == advanced
    assert advanced in waiting.diagnostic

    # ② agent 重跑同步 skill（吸收新 tip + 留下合法 receipt）→ **必须**能进 review。
    _absorb_local_base(fixture)
    _write_bootstrap_receipt(fixture)
    resumed = gate_module.reconcile_bootstrap_gate(gate)
    assert resumed.outcome == gate_module.OUTCOME_RECEIPT_VALID
    assert resumed.resumable is True
    assert resumed.holds_gate is False
    assert resumed.local_base_identity_changed is True
    assert resumed.observed_local_base_oid == advanced
    assert resumed.resume_run_id == "bootstrap-run"  # 仍是同一个 run，不是第二轮

    # ③ 写侧把 base 身份就地重新快照；重新快照后的 gate 不再标记「身份已变」。
    refreshed = gate_module.gate_with_verified_receipt(gate, resumed)
    assert refreshed["expected_local_base_oid"] == advanced
    settled = gate_module.reconcile_bootstrap_gate(refreshed)
    assert settled.outcome == gate_module.OUTCOME_RECEIPT_VALID
    assert settled.local_base_identity_changed is False
    pinned_digest = refreshed["bootstrap_receipt_digest"]
    assert len(pinned_digest) == 64

    # ④ 旧 epoch 的 receipt 不因此被放行：``absorbed_base_tip`` 仍指旧 base → 全套校验拦下。
    _write_bootstrap_receipt(fixture, absorbed_base_tip=armed_base_oid)
    stale_evidence = gate_module.reconcile_bootstrap_gate(refreshed)
    assert stale_evidence.outcome == gate_module.OUTCOME_TAMPERED
    assert stale_evidence.reason == "bootstrap_receipt_identity_invalid"

    # ⑤ base 身份**未变**时，digest 判据一如既往 fail-closed（④/⑥ 的放行不得顺带削弱它）。
    swapped = _write_bootstrap_receipt(fixture, remote_name="swapped-remote")
    assert swapped.exists()
    swapped_under_same_base = gate_module.reconcile_bootstrap_gate(refreshed)
    assert swapped_under_same_base.outcome == gate_module.OUTCOME_TAMPERED
    assert swapped_under_same_base.reason == "bootstrap_receipt_digest_changed"

    # ⑥ 老 epoch 里钉下的 digest 不得把新 epoch 的合法 receipt 判成篡改——否则「首轮 receipt 已
    # 验证过、随后 base 前移」这条路径仍会永久挂死（同一个失败家族的第二张脸）。
    _write_bootstrap_receipt(fixture)
    old_epoch_pin = {**gate, "bootstrap_receipt_digest": "0" * 64}
    recovered = gate_module.reconcile_bootstrap_gate(old_epoch_pin)
    assert recovered.outcome == gate_module.OUTCOME_RECEIPT_VALID
    assert recovered.local_base_identity_changed is True


def test_agent_cli_start_retry_resumes_after_local_base_advances_during_the_wait(
    tmp_path: Path,
) -> None:
    """RVF-008① 入口 A 侧：base 前移后的 `start --retry` 必须真正推进到 prepare。

    reviewer 实测的 probe 形状：修复前三次 `start --retry` 全部 exit 6 /
    ``local_base_identity_changed``，prepare 再也不被调用、token 停在 acknowledged、gate 永不摘除
    ——入口 A 没有任何预算兜底（预算机制只在入口 B 的 Stop 读侧），故该轮永久无法进入 review。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    repo = str(fixture["repo"])
    roots = _agent_cli_roots(cli, tmp_path)
    token = _mint_agent_decision_token(dts, roots, worktree=repo)

    initiation_run_dir = tmp_path / "runs" / "initiation"
    initiation_run_dir.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0, "resume": [], "bootstrapped": False}

    def _pipeline(
        *, token_record, repo, session_id, transcript_path, now, timeout_seconds=60.0,
        resume_run_id=None, resume_run_dir=None,
        parent_attempt_run_id=None, parent_attempt_run_dir=None,
    ):
        calls["n"] += 1
        calls["resume"].append((resume_run_id, resume_run_dir))
        if calls["bootstrapped"]:
            return {
                "status": "completed", "run_id": resume_run_id or "initiation",
                "run_dir": resume_run_dir or str(initiation_run_dir),
                "artifacts": {}, "error": None, "bootstrap_gate": None,
                "initiation_run_id": "initiation",
                "initiation_run_dir": str(initiation_run_dir),
            }
        return {
            "status": gate_module.WAITING_STATUS,
            "bootstrap_gate": gate_module.build_bootstrap_gate(
                Path(repo),
                {
                    "required_skill": "sync-local-base-into-task-worktree-only",
                    "local_base_ref": fixture["local_base_ref"],
                    "local_base_tip": fixture["local_base_tip"],
                    "task_head": run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
                },
                origin_kind=gate_module.BOOTSTRAP_ORIGIN_DECISION_TOKEN,
                rvf_run_id="initiation",
                rvf_run_dir=str(initiation_run_dir),
            ),
            "run_id": None, "run_dir": None, "artifacts": {}, "error": None,
            "initiation_run_id": "initiation",
            "initiation_run_dir": str(initiation_run_dir),
        }

    # ① 首次 start 撞 bootstrap → 等待态落 gate（记的是**当时**的 base tip）。
    first = cli.run_start(
        token=token, reason="首轮发起", repo=repo, roots=roots, prepare_pipeline=_pipeline
    )
    assert first.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED
    token_path = roots.token_root / f"{token}.json"
    armed_base_oid = json.loads(token_path.read_text(encoding="utf-8"))["bootstrap_gate"][
        "expected_local_base_oid"
    ]

    # ② 等待期内 local base 前移；agent 随后按 next_steps 吸收新 base 并留下合法 receipt。
    advanced = _advance_local_base(fixture)
    assert advanced != armed_base_oid
    _absorb_local_base(fixture)
    _write_bootstrap_receipt(fixture)
    calls["bootstrapped"] = True

    # ③ `start --retry`：必须真正走到 prepare 并复用**同一个** run（修复前恒 exit 6、prepare 不被调用）。
    resumed = cli.run_start(
        token=token, reason="bootstrap 完成后恢复本轮", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )
    assert resumed.status == "ok"
    assert resumed.exit_code == cli.EXIT_OK
    assert calls["n"] == 2
    assert calls["resume"][-1] == ("initiation", str(initiation_run_dir))
    final = json.loads(token_path.read_text(encoding="utf-8"))
    assert "bootstrap_gate" not in final  # gate 已摘除，回落常规流程
    assert len(list(roots.token_root.glob("*.json"))) == 1  # 无第二枚 token


def test_agent_cli_concurrent_bootstrap_resume_claims_prepare_exactly_once(
    tmp_path: Path,
) -> None:
    """RVF-008②：入口 A 的 receipt_valid 恢复路径必须有与入口 B 对偶的原子 claim。

    修复前两个并发 `rvf-agent start --retry` 会各自 reconcile 出 ``receipt_valid``，再各自带
    **同一个** ``resume_run_id``/``resume_run_dir`` 调 ``start_run(run_id=X, run_dir=X)`` ——同一个
    run 目录被并发写、并可能分配出第二条 tracker lease，违反「恢复不得产生重复 lease / reviewer」。

    这里让第二次 `start --retry` 在第一次**仍在 prepare 之中**时到达（占位是进程外的文件锁，
    同进程重入与跨进程等价），断言只有一个进入者真正调到 prepare pipeline，另一个拿到可重试的
    在途等待态；并直接压一遍 store 侧原语的 claim / 拒绝 / 释放 / 过窗接手四态。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    gate_module = load_rvf_bootstrap_gate_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    repo = str(fixture["repo"])
    roots = _agent_cli_roots(cli, tmp_path)
    token = _mint_agent_decision_token(dts, roots, worktree=repo)

    initiation_run_dir = tmp_path / "runs" / "initiation"
    initiation_run_dir.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0, "bootstrapped": False}
    concurrent: dict[str, object] = {}

    def _pipeline(
        *, token_record, repo, session_id, transcript_path, now, timeout_seconds=60.0,
        resume_run_id=None, resume_run_dir=None,
        parent_attempt_run_id=None, parent_attempt_run_dir=None,
    ):
        calls["n"] += 1
        if calls["bootstrapped"]:
            if not concurrent.get("dispatched"):
                # 第一个进入者还在 prepare 之中，另一次并发 `start --retry` 到达。
                # 先置 dispatched 再重入：占位缺失时这条重入会**真的**跑进 pipeline，单发标记
                # 保证它只发生一次（否则退化成无限递归，红得不干净、也看不出断言在哪）。
                concurrent["dispatched"] = True
                concurrent["result"] = cli.run_start(
                    token=token, reason="并发重试", repo=repo, retry=True, roots=roots,
                    prepare_pipeline=_pipeline,
                )
                concurrent["calls_after"] = calls["n"]
            return {
                "status": "completed", "run_id": resume_run_id or "initiation",
                "run_dir": resume_run_dir or str(initiation_run_dir),
                "artifacts": {}, "error": None, "bootstrap_gate": None,
                "initiation_run_id": "initiation",
                "initiation_run_dir": str(initiation_run_dir),
            }
        return {
            "status": gate_module.WAITING_STATUS,
            "bootstrap_gate": gate_module.build_bootstrap_gate(
                Path(repo),
                {
                    "required_skill": "sync-local-base-into-task-worktree-only",
                    "local_base_ref": fixture["local_base_ref"],
                    "local_base_tip": fixture["local_base_tip"],
                    "task_head": run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
                },
                origin_kind=gate_module.BOOTSTRAP_ORIGIN_DECISION_TOKEN,
                rvf_run_id="initiation",
                rvf_run_dir=str(initiation_run_dir),
            ),
            "run_id": None, "run_dir": None, "artifacts": {}, "error": None,
            "initiation_run_id": "initiation",
            "initiation_run_dir": str(initiation_run_dir),
        }

    first = cli.run_start(
        token=token, reason="首轮发起", repo=repo, roots=roots, prepare_pipeline=_pipeline
    )
    assert first.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED

    _absorb_local_base(fixture)
    _write_bootstrap_receipt(fixture)
    calls["bootstrapped"] = True

    resumed = cli.run_start(
        token=token, reason="bootstrap 完成后恢复本轮", repo=repo, retry=True, roots=roots,
        prepare_pipeline=_pipeline,
    )

    # ① 只有一个进入者真正调到 prepare：并发那次在 pipeline 里**没有**再加一次计数。
    assert resumed.status == "ok"
    assert concurrent["calls_after"] == 2  # 1 = 首轮探测，2 = 本次恢复；并发那次未进 pipeline
    assert calls["n"] == 2

    # ② 并发那次拿到可重试的在途等待态：仍是 EXIT_BOOTSTRAP_REQUIRED、token 保持 acknowledged，
    # 且文案明确「别再跑同步 skill」（再跑会产出另一份 receipt，下一轮按 digest 判 tampered）。
    blocked = concurrent["result"]
    assert blocked.status == "bootstrap_replay_in_flight"
    assert blocked.exit_code == cli.EXIT_BOOTSTRAP_REQUIRED
    assert blocked.token_state == dts.STATE_ACKNOWLEDGED
    assert blocked.detail["bootstrap_reason"] == "bootstrap_replay_in_flight"
    steps = "\n".join(blocked.next_steps)
    assert "**不要**再跑一次同步 skill" in steps
    assert f" start --token {token} --retry" in steps

    # ③ 占位在 prepare 收尾后已释放，且它不是一枚会被 token 扫描看见的第二个 token 文件。
    claim_path = dts.bootstrap_replay_claim_path(token, roots.token_root)
    assert not claim_path.exists()
    assert len(list(roots.token_root.glob("*.json"))) == 1

    # ④ store 侧原语四态：抢到 / 窗口内拒绝 / 释放后可再抢 / 过窗接手。
    claimed, age = dts.claim_bootstrap_replay_in_flight(
        token, window_seconds=300.0, root=roots.token_root
    )
    assert (claimed, age) == (True, None)
    again, held_age = dts.claim_bootstrap_replay_in_flight(
        token, window_seconds=300.0, root=roots.token_root
    )
    assert again is False
    assert held_age is not None and held_age >= 0.0
    assert dts.release_bootstrap_replay_in_flight(token, roots.token_root) is True
    reclaimed, _ = dts.claim_bootstrap_replay_in_flight(
        token, window_seconds=300.0, root=roots.token_root
    )
    assert reclaimed is True
    taken_over, stale_age = dts.claim_bootstrap_replay_in_flight(
        token, window_seconds=0.0, root=roots.token_root
    )
    assert taken_over is True  # 持有者被硬杀留下的残留占位不得永久堵死本轮
    assert stale_age is not None
    assert dts.release_bootstrap_replay_in_flight(token, roots.token_root) is True


def test_bootstrap_replay_hard_bounds_a_stuck_prepare_in_a_killable_subprocess(
    tmp_path: Path,
) -> None:
    """RVF-006②：一次卡死的 prepare 不得拖垮调用方的**进程**寿命。

    修复前 `replay_prepare_for_gate` 在 Stop hook 进程内直调 `prepare_run_from_prep_file`：
    ``future.result(timeout=…)`` 只让**函数**按时返回，`concurrent.futures.thread` 注册的
    ``_python_exit`` 仍会在解释器退出时 join 那个非守护 worker——进程挂到 prepare 自己跑完为止，
    60s 预算形同虚设、Stop hook 被 120s 预算杀掉，自动恢复决定永远送不出去。

    因此这里断言的是**进程级**上限：起一个只调 `replay_prepare_for_gate` 的探针进程，让 prepare
    卡在一条永不返回的 git 上（PATH 垫片，真实故障形状，不是测试专用钩子），要求探针进程在
    ``预算 + 强杀余量`` 量级内整体退出，返回值落在可重试的 ``timeout`` 分类上而不是崩溃。
    """
    gate_module = load_rvf_bootstrap_gate_module()
    prep_file = load_rvf_prep_file_module()
    fixture = _bootstrap_opt_in_repo(tmp_path)
    _prepare, record, state = _prepared_bootstrap_prep(tmp_path, fixture)
    gate = gate_module.gate_of(state)
    assert gate["origin"]["prep_token"] == record.token

    # 卡死的 prepare：PATH 最前面放一个只会长睡的 git 垫片，prepare_run 的第一条 git 调用即挂住。
    shim_dir = tmp_path / "stuck-git-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    shim.chmod(0o755)

    scripts_dir = Path(gate_module.__file__).resolve().parent
    probe = tmp_path / "replay_probe.py"
    probe.write_text(
        "import json, sys, time\n"
        f"sys.path.insert(0, {str(scripts_dir)!r})\n"
        "import rvf_bootstrap_gate\n"
        "gate = json.loads(sys.argv[1])\n"
        "started = time.monotonic()\n"
        "result = rvf_bootstrap_gate.replay_prepare_for_gate(\n"
        "    gate, timeout_seconds=float(sys.argv[2])\n"
        ")\n"
        "with open(sys.argv[3], 'w', encoding='utf-8') as handle:\n"
        "    json.dump({'elapsed': time.monotonic() - started, 'result': result}, handle)\n",
        encoding="utf-8",
    )

    budget = 1.0
    hard_ceiling = budget + gate_module.REPLAY_SUBPROCESS_KILL_GRACE_SECONDS
    probe_result = tmp_path / "replay-probe-result.json"
    probe_log = tmp_path / "replay-probe.log"
    environment = dict(os.environ)
    environment["PATH"] = f"{shim_dir}{os.pathsep}{environment.get('PATH', '')}"
    environment[prep_file.ENV_PREP_ROOT] = str(tmp_path / "prep")

    started = time.monotonic()
    # 管道会让「强杀后仍要等孙进程放掉写端」重新变成一次不可控等待，故一律走文件。
    with probe_log.open("wb") as sink:
        completed = subprocess.run(
            [
                sys.executable,
                str(probe),
                json.dumps(gate, ensure_ascii=False),
                str(budget),
                str(probe_result),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            env=environment,
            timeout=45,
        )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, probe_log.read_text(encoding="utf-8", errors="replace")
    payload = json.loads(probe_result.read_text(encoding="utf-8"))
    # 进程级硬上限（留出解释器启动 + import prepare_review_run 的余量）。修复前这里是
    # 「等到 60s 的 git 垫片自己睡完」，早就撞穿了断言与 45s 的外层保险。
    assert elapsed < hard_ceiling + 12.0, (elapsed, payload)
    assert payload["elapsed"] < hard_ceiling + 12.0, payload

    # 可重试的等待态（分类信号），而不是抛异常、也不是被当成成功。
    assert payload["result"]["status"] == gate_module.REPLAY_STATUS_TIMEOUT
    assert payload["result"]["status"] != gate_module.REPLAY_STATUS_COMPLETED
    assert payload["result"].get("error")

    # prep 仍在、且没有被谎报成 completed：窗口过后重放同一枚 prep 是安全且必要的。
    reread = json.loads(record.path.read_text(encoding="utf-8"))
    assert reread["rvf_run"]["shared_workflow_state"]["status"] != "completed"
