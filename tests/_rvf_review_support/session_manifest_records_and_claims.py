#!/usr/bin/env python3
"""session manifest 记录与 ownership claims 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_session_manifest_extracts_apply_patch_and_command_candidates',
    'test_session_manifest_does_not_claim_post_commit_same_path_background_dirty',
    'test_session_manifest_claims_apply_patch_after_commit_cutoff',
    'test_session_manifest_only_claims_matching_apply_patch_hunk',
    'test_session_manifest_records_edit_claim_user_context',
    'test_session_manifest_records_codex_message_user_context',
    'test_session_manifest_suppresses_unresolved_without_tracker_watermark',
    'test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark',
    'test_session_manifest_uses_tracker_transcript_watermark',
    'test_session_manifest_legacy_timestampless_transcript_fallback_warns',
    'test_session_manifest_resolves_exec_paths_from_command_workdir',
    'test_session_manifest_claims_claude_write_tool_paths',
    'test_session_manifest_writes_tracker_claim',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_session_manifest_extracts_apply_patch_and_command_candidates(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)
    manifest_path = tmp_path / "manifest.json"

    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
        ]
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_id"] == "session-tracking-test"
    assert manifest["confidence"] == "medium"
    assert "tracked.txt" in manifest["owned_paths"]
    assert "owned-new.txt" in manifest["owned_paths"]
    assert "removed.txt" in manifest["owned_paths"]
    assert "generated.txt" in manifest["owned_paths"]
    assert "tracked.txt" in manifest["owned_dirty_paths"]
    assert "generated.txt" in manifest["owned_dirty_paths"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    assert "new.txt" in manifest["unattributed_dirty_paths"]
    assert manifest["apply_patch_operations"][0]["operation"] == "update"
    assert manifest["command_path_candidates"][0]["reason"] == "shell_redirect"


def test_session_manifest_does_not_claim_post_commit_same_path_background_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    transcript = tmp_path / "session.jsonl"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    records = [
        {"timestamp": "2020-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "S"}},
        {
            "timestamp": "2020-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-old"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("owned\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit owned change"], cwd=repo)
    (repo / "a.txt").write_text("background\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["owned_paths"] == ["a.txt"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["unattributed_dirty_paths"] == ["a.txt"]
    assert manifest["ownership_baseline"]["mode"] == "head_commit_time"
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1


def test_session_manifest_claims_apply_patch_after_commit_cutoff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    old_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    new_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " owned\n"
        "+new\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": old_patch}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "git commit -m owned", "workdir": str(repo)}),
                "call_id": "commit-call",
            },
        },
        {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "commit-call", "exit_code": 0}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": new_patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("owned\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "owned"], cwd=repo)
    (repo / "a.txt").write_text("owned\nnew\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["owned_paths"] == ["a.txt"]
    assert manifest["owned_dirty_paths"] == ["a.txt"]
    assert manifest["unattributed_dirty_paths"] == []
    assert manifest["ownership_baseline"]["mode"] == "line_cutoff"
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 1
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1


def test_session_manifest_only_claims_matching_apply_patch_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n"
        "keep\n"
        "middle\n"
        "keep\n"
        "bottom\n",
        encoding="utf-8",
    )
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " bottom\n"
        "+session-owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text(
        "top\n"
        "background\n"
        "middle\n"
        "keep\n"
        "bottom\n"
        "session-owned\n",
        encoding="utf-8",
    )
    log_root = tmp_path / "state"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
            ],
            env=env,
        ).stdout
    )

    owned_units = manifest["tracker"]["owned_units"]
    assert manifest["owned_dirty_paths"] == ["a.txt"]
    assert len(owned_units) == 1
    assert owned_units[0]["unit"] == "hunk"
    assert "session-owned" in run(["git", "diff", "HEAD", "--", "a.txt"], cwd=repo).stdout


def test_session_manifest_records_edit_claim_user_context(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+claimed\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "first request"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "keep the claimed line"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nclaimed\n", encoding="utf-8")
    log_root = tmp_path / "logs"

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-1",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    assert len(manifest["edit_claims"]) == 1
    claim = manifest["edit_claims"][0]
    assert claim["path"] == "a.txt"
    assert claim["call_id"] == "patch-1"
    assert claim["latest_user_message"] == "keep the claimed line"
    assert claim["latest_user_line_number"] == 3
    assert claim["mapped_unit_ids"]
    assert manifest["edit_claim_registration"]["status"] == "ok"
    assert manifest["edit_claim_registration"]["registered_count"] == 1

    tracker_dir = Path(manifest["tracker"]["tracker_dir"])
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT claim_id, session_id, call_id, path, status, latest_user_line_number, latest_user_message
              FROM edit_claims
            """
        ).fetchone()
        assert row is not None
        assert row["session_id"] == "S"
        assert row["call_id"] == "patch-1"
        assert row["path"] == "a.txt"
        assert row["status"] == "pending"
        assert row["latest_user_line_number"] == 3
        assert row["latest_user_message"] == "keep the claimed line"
        unit_count = conn.execute(
            "SELECT COUNT(*) FROM edit_claim_units WHERE claim_id=?",
            (row["claim_id"],),
        ).fetchone()[0]
        assert unit_count == len(claim["mapped_unit_ids"])
        conn.execute("DELETE FROM session_units WHERE session_id='S'")

    allocated = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="review-run",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=4,
    )
    assert allocated["status"] == "allocated"
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=allocated["lease_id"],
        unit_ids=allocated["scope"]["unit_ids"],
        scope_hash=allocated["scope_hash"],
        run_id="review-run",
        log_root_override=log_root,
    )
    assert completed["status"] == "released"
    assert completed["reviewed_edit_claim_count"] == 1
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        status = conn.execute("SELECT status FROM edit_claims").fetchone()[0]
        assert status == "reviewed"


def test_session_manifest_records_codex_message_user_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+claimed\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex message user context"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nclaimed\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["apply_patch_operations"][0]["latest_user_line_number"] == 2
    assert manifest["apply_patch_operations"][0]["latest_user_message"] == "codex message user context"
    claim = manifest["edit_claims"][0]
    assert claim["latest_user_line_number"] == 2
    assert claim["latest_user_message"] == "codex message user context"


def test_session_manifest_suppresses_unresolved_without_tracker_watermark(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nbackground\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    unresolved = manifest["patch_ownership"]["unresolved_owned_patch_hunks"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["unattributed_dirty_paths"] == ["a.txt"]
    assert unresolved == []


def test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("base\nleased\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    seeded = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=1,
    )
    assert seeded["status"] == "allocated"

    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "event_msg", "payload": {"note": "prior turn"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nbackground\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-2",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    unresolved = manifest["patch_ownership"]["unresolved_owned_patch_hunks"]
    assert len(unresolved) == 1
    assert unresolved[0]["path"] == "a.txt"
    assert unresolved[0]["call_id"] == "patch-1"
    assert unresolved[0]["reason"] == "no_current_diff_hunk_contains_patch_mutations"


def test_session_manifest_uses_tracker_transcript_watermark(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("base\nold\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    first = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=2,
    )
    assert first["status"] == "allocated"

    old_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+old\n"
        "*** End Patch\n"
    )
    new_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+new\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": old_patch}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": new_patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nnew\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-2",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    assert manifest["ownership_baseline"]["tracker_transcript_max_line_number"] == 2
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 1
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1
    assert manifest["patch_ownership"]["transcript_max_line_number"] == 3
    assert manifest["patch_ownership"]["expected_apply_patch_paths"] == ["a.txt"]


def test_session_manifest_legacy_timestampless_transcript_fallback_warns(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        "-base\n"
        "+base\n"
        "+legacy\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "legacy-session"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["ownership_baseline"]["mode"] == "legacy_full_transcript"
    assert any("ownership_baseline_fallback" in warning for warning in manifest["warnings"])


def test_session_manifest_resolves_exec_paths_from_command_workdir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("x\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-subdir-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf x > note.md", "workdir": str(docs)}),
                "call_id": "call_exec",
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
        ]
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "docs/note.md" in manifest["owned_paths"]
    assert "note.md" not in manifest["owned_paths"]
    assert "docs/note.md" in manifest["owned_dirty_paths"]
    assert "docs/note.md" not in manifest["unattributed_dirty_paths"]


def test_session_manifest_claims_claude_write_tool_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("base\nclaude\n", encoding="utf-8")
    notebook = repo / "analysis.ipynb"
    notebook.write_text('{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}\n', encoding="utf-8")
    run(["git", "add", "analysis.ipynb"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add notebook"], cwd=repo)
    notebook.write_text('{"cells":[{"cell_type":"code"}],"metadata":{},"nbformat":4,"nbformat_minor":5}\n', encoding="utf-8")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    transcript = tmp_path / "claude-session.jsonl"
    records = [
        {"timestamp": "2999-04-27T00:00:00.000Z", "sessionId": "claude-session-1"},
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": str(repo / "tracked.txt"),
                            "old_string": "base\n",
                            "new_string": "base\nclaude\n",
                        },
                    }
                ]
            },
        },
        {
            "timestamp": "2999-04-27T00:00:02.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "NotebookEdit",
                        "input": {
                            "notebook_path": str(notebook),
                            "new_source": "print('rvf')",
                            "cell_type": "code",
                        },
                    }
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["session_id"] == "claude-session-1"
    assert manifest["confidence"] == "medium"
    assert manifest["owned_paths"] == ["analysis.ipynb", "tracked.txt"]
    assert manifest["owned_dirty_paths"] == ["analysis.ipynb", "tracked.txt"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    assert "new.txt" in manifest["unattributed_dirty_paths"]
    assert manifest["claude_write_events"] == [
        {"line_number": 2, "name": "Edit", "path": "tracked.txt"},
        {"line_number": 3, "name": "NotebookEdit", "path": "analysis.ipynb"},
    ]
    assert manifest["ownership_baseline"]["mode"] == "head_commit_time"
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 2


def test_session_manifest_writes_tracker_claim(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
            "--tracker-run-id",
            "run-tracker",
        ],
        env=env,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    tracker = payload.get("tracker")
    assert isinstance(tracker, dict)
    assert tracker["status"] == "ok"
    assert tracker["repo_key"]
    assert tracker["claim_ids"]
    assert tracker["tracker_dir"]
    assert any(unit.get("unit") in {"hunk", "path"} for unit in tracker.get("owned_units", []))

