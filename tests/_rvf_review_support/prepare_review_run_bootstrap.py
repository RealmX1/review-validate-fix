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
