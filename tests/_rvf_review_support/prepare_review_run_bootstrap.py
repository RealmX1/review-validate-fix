#!/usr/bin/env python3
"""prepare review run 与 worktree bootstrap 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
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
    assert contract["version"] == 2
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

