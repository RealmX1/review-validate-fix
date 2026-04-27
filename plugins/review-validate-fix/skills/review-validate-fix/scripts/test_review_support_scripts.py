#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"
SESSION_MANIFEST = SCRIPT_DIR / "session_manifest.py"


def run(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=path)
    run(["git", "config", "user.name", "RVF Test"], cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=path)
    run(["git", "commit", "-q", "-m", "base"], cwd=path)
    (path / "tracked.txt").write_text("base\nchange\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")
    return path


def write_alternative_reviewer_config(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    output_format: str | None = "text",
) -> Path:
    payload = {
        "enabled": True,
        "label": "alternative-reviewer:test",
        "command": command,
        "allow_repo_cwd": True,
        "idle_timeout_seconds": idle_timeout_seconds,
        "activity_check_interval_seconds": activity_check_interval_seconds,
        "env_unset": [],
    }
    if output_format is not None:
        payload["output_format"] = output_format
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_codex_transcript(path: Path, repo: Path) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        "-base\n"
        "+base edited by session\n"
        "*** Add File: owned-new.txt\n"
        "+owned\n"
        "*** Delete File: removed.txt\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-tracking-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
        {
            "timestamp": "2026-04-27T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf generated > generated.txt", "workdir": str(repo)}),
                "call_id": "call_exec",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_check_review_output_lock_request() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="RVF_LOCK_REQUEST name=npm-test command=npm test reason=shared-cache\n",
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "lock_request"
    assert payload["lock_request_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name=n command=x reason=y\nNO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0


def test_check_review_output_accepts_wrapped_issue_continuation() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "1. apps/theseus-mcp/src/tool_registry.ts:1306 task 级上下文先截断 reviewRuns。\n"
            "`query_checkpoint_context` 随后用截断后的 run 集合过滤 signals，可能漏掉同 task 的较早 run。\n"
        ),
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "issues"
    assert payload["issue_count"] == 1
    assert payload["continuation_line_count"] == 1

    extensionless_numbered = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. Dockerfile:3 合法 issue 可以引用没有扩展名的文件。\n",
    )
    extensionless_payload = json.loads(extensionless_numbered.stdout)
    assert extensionless_payload["valid"] is True
    assert extensionless_payload["issue_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. apps/foo.ts 这条缺少行号\n续行不能补足 path:line\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0

    misplaced_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 这里先写说明，再引用 plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert misplaced_path_line.returncode != 0

    english_misplaced_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. explanation before plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert english_misplaced_path_line.returncode != 0

    prose_see_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. See plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_see_path_line.returncode != 0

    prose_in_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. in plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_in_path_line.returncode != 0

    prose_because_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. Because a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_because_path_line.returncode != 0

    chinese_because_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 因为 a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_because_path_line.returncode != 0

    chinese_file_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 文件 a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_file_path_line.returncode != 0

    prose_note_colon_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. Note: a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_note_colon_path_line.returncode != 0

    prose_warning_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. warning a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_warning_path_line.returncode != 0

    invalid_extensionless = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input=(
            "1. plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 valid issue\n"
            "Dockerfile:2 missing numbered prefix\n"
            "Makefile:10 missing numbered prefix\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_extensionless.returncode != 0

    unnumbered_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nb.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_issue.returncode != 0

    unnumbered_no_extension_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nMakefile:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_no_extension_issue.returncode != 0

    malformed_numbered_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n2) b.py:2 第二条编号格式错误\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_numbered_issue.returncode != 0

    malformed_numbered_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n2) 第二条编号格式错误\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_numbered_continuation.returncode != 0

    spaced_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. slide-versions/claude cowork 1/deck.txt:2 含空格路径仍是合法 path:line。\n",
    )
    spaced_payload = json.loads(spaced_path.stdout)
    assert spaced_payload["valid"] is True
    assert spaced_payload["issue_count"] == 1

    spaced_root_component = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. my dir/file.py:2 根目录组件含空格仍是合法 path:line。\n",
    )
    spaced_root_payload = json.loads(spaced_root_component.stdout)
    assert spaced_root_payload["valid"] is True
    assert spaced_root_payload["issue_count"] == 1

    colon_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. foo:bar.py:2 路径名含冒号时应使用最后的 :line 作为行号。\n",
    )
    colon_payload = json.loads(colon_path.stdout)
    assert colon_payload["valid"] is True
    assert colon_payload["issue_count"] == 1

    unicode_root_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. 设计 文档.md:3 非 ASCII 根路径也应支持。\n",
    )
    unicode_root_payload = json.loads(unicode_root_path.stdout)
    assert unicode_root_payload["valid"] is True
    assert unicode_root_payload["issue_count"] == 1

    repeated_path_line = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. a.py:1 causes b.py:2 to fail when both paths are involved.\n",
    )
    repeated_payload = json.loads(repeated_path_line.stdout)
    assert repeated_payload["valid"] is True
    assert repeated_payload["issue_count"] == 1

    chinese_no_issue_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n没有问题\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_no_issue_continuation.returncode != 0

    fix_summary_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n修复说明：已修改文件\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert fix_summary_continuation.returncode != 0

    unnumbered_spaced_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nmy file.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_spaced_issue.returncode != 0

    unnumbered_spaced_dir_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nmy dir/file.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_spaced_dir_issue.returncode != 0

    unnumbered_colon_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nfoo:bar.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_colon_issue.returncode != 0

    unnumbered_unicode_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n设计 文档.md:3 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_unicode_issue.returncode != 0


def test_build_packet_metadata_and_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
            "--primary-file",
            "tracked.txt",
            "--background-file",
            "new.txt",
        ]
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Review Scope" in packet_text
    assert "## Session Context" in packet_text
    assert payload["session_context_provided"] is True
    assert payload["session_context_bytes"] > 0
    assert payload["scope_of_work_file"] == str(context.resolve())
    assert payload["primary_files"] == ["tracked.txt"]
    assert payload["background_files"] == ["new.txt"]
    assert payload["packet_bytes"] == len(packet_text.encode("utf-8"))


def test_session_manifest_extracts_apply_patch_and_command_candidates(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"

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


def test_session_manifest_resolves_exec_paths_from_command_workdir(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("x\n", encoding="utf-8")
    transcript = tmp / "session.jsonl"
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-subdir-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
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
    manifest_path = tmp / "manifest.json"

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


def test_build_packet_uses_session_manifest_as_scope_anchor(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned contents\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest = tmp / "manifest.json"
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest),
        ]
    )

    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Session Manifest" in packet_text
    assert "## Session-Owned Git Diff" in packet_text
    assert "## Full Git Diff HEAD (Evidence Only)" in packet_text
    assert "Session-owned paths:" in packet_text
    assert "- tracked.txt" in packet_text
    assert "- background.txt" in packet_text
    assert "Background untracked paths below were not attributed to this session and are not inlined" in packet_text
    assert "### owned-new.txt" in packet_text
    assert "owned contents" in packet_text
    assert "### background.txt" not in packet_text
    assert "background contents" not in packet_text
    assert payload["session_manifest_provided"] is True
    assert payload["session_owned_path_count"] >= 3
    assert payload["owned_untracked_count"] == 1
    assert payload["background_untracked_count"] >= 2


def test_build_packet_rejects_session_manifest_for_different_repo(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject mismatched manifest\n",
        encoding="utf-8",
    )
    manifest = tmp / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(tmp / "other-repo"),
                "owned_paths": ["tracked.txt"],
                "owned_dirty_paths": ["tracked.txt"],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session manifest repo does not match current repo" in completed.stderr


def test_build_packet_rejects_empty_session_owned_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject empty manifest scope\n",
        encoding="utf-8",
    )
    manifest = tmp / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(repo.resolve()),
                "owned_paths": [],
                "owned_dirty_paths": [],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session manifest has no owned paths" in completed.stderr


def test_build_packet_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_build_packet_honors_review_validate_fix_ignore(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared ignored artifacts\n",
        encoding="utf-8",
    )
    (repo / ".review-validate-fix-ignore").write_text("slide-versions/\nsecret\n", encoding="utf-8")
    (repo / "secret.txt").write_text("committed secret contents\n", encoding="utf-8")
    run(["git", "add", "secret.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add secret"], cwd=repo)
    ignored = repo / "slide-versions" / "claude cowork 1"
    ignored.mkdir(parents=True)
    (ignored / "deck.txt").write_text("ignored deck contents\n", encoding="utf-8")
    (repo / "secret.txt").write_text("ignored secret contents\n", encoding="utf-8")
    (repo / "secret-alpha.txt").write_text("ignored secret prefix contents\n", encoding="utf-8")
    (repo / "kept.txt").write_text("visible contents\n", encoding="utf-8")

    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["excluded_path_prefixes"] == ["secret", "slide-versions/"]
    assert payload["untracked_count"] == 3
    assert "## Excluded Paths" in packet_text
    assert "- secret" in packet_text
    assert "- slide-versions/" in packet_text
    assert "### .review-validate-fix-ignore" in packet_text
    assert "### kept.txt" in packet_text
    assert "### new.txt" in packet_text
    assert "slide-versions/claude cowork 1/deck.txt" not in packet_text
    assert "ignored deck contents" not in packet_text
    assert "### secret.txt" not in packet_text
    assert "secret.txt |" not in packet_text
    assert "### secret-alpha.txt" not in packet_text
    assert "committed secret contents" not in packet_text
    assert "ignored secret contents" not in packet_text
    assert "ignored secret prefix contents" not in packet_text


def test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared literal ignore paths\n",
        encoding="utf-8",
    )
    (repo / ".review-validate-fix-ignore").write_text("literal[glob]/\nsecret*.txt\n", encoding="utf-8")
    literal_dir = repo / "literal[glob]"
    wildcard_dir = repo / "literalx"
    literal_dir.mkdir()
    wildcard_dir.mkdir()
    (literal_dir / "hidden.txt").write_text("hidden literal dir\n", encoding="utf-8")
    (wildcard_dir / "visible.txt").write_text("visible wildcard-like dir\n", encoding="utf-8")
    (repo / "secret*.txt").write_text("hidden literal file\n", encoding="utf-8")
    (repo / "secret-alpha.txt").write_text("visible wildcard-like file\n", encoding="utf-8")

    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    assert "literal[glob]/hidden.txt" not in packet_text
    assert "hidden literal dir" not in packet_text
    assert "### secret*.txt" not in packet_text
    assert "hidden literal file" not in packet_text
    assert "### literalx/visible.txt" in packet_text
    assert "visible wildcard-like dir" in packet_text
    assert "### secret-alpha.txt" in packet_text
    assert "visible wildcard-like file" in packet_text


def test_prepare_review_run_and_command_lock(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
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
            str(tmp / "runs"),
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
    assert payload["session_context"] == payload["scope_of_work_file"]
    assert payload["source_session_context"] == str(context.resolve())
    assert payload["session_context_provided"] is True
    assert payload["excluded_path_prefixes"] == ["secret.txt"]
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert metadata["excluded_path_prefixes"] == ["secret.txt"]
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert "## Excluded Paths" in packet_text
    assert "- secret.txt" in packet_text
    assert "### secret.txt" not in packet_text

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


def test_prepare_review_run_can_build_session_manifest_from_transcript(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared transcript-scoped review run\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)

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
            str(tmp / "runs"),
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["session_manifest"]).exists()
    assert payload["session_manifest_provided"] is True
    assert payload["source_session_manifest"] == f"transcript:{transcript.resolve()}"
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert "## Session Manifest" in packet_text
    assert "background contents" not in packet_text


def test_prepare_review_run_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--base-dir",
            str(tmp / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_alternative_reviewer_idle_timeout_flag(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr


def test_alternative_reviewer_activity_refreshes_idle_timeout(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time; sys.stdin.read(); "
                "[print(f'tick-{i}', flush=True) or time.sleep(0.08) for i in range(4)]; "
                "print('NO_ISSUES', flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NO_ISSUES" in completed.stdout
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_claude_stream_json_extracts_result(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_legacy_claude_config_gets_stream_json(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--include-hook-events" in argv
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert "--disable-slash-commands" in argv


def test_alternative_reviewer_respects_explicit_claude_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p", "--output-format", "text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "text"]


def test_alternative_reviewer_respects_explicit_claude_equals_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p", "--output-format=text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format=text"]


def test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "stream_wrapper"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-u", str(shim), "--native-stream"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    assert json.loads(sink.read_text(encoding="utf-8")) == ["--native-stream"]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        test_check_review_output_lock_request()
        test_check_review_output_accepts_wrapped_issue_continuation()
        test_build_packet_metadata_and_scope(root / "packet")
        test_session_manifest_extracts_apply_patch_and_command_candidates(root / "session-manifest")
        test_session_manifest_resolves_exec_paths_from_command_workdir(root / "session-manifest-workdir")
        test_build_packet_uses_session_manifest_as_scope_anchor(root / "packet-manifest")
        test_build_packet_rejects_session_manifest_for_different_repo(root / "packet-manifest-repo")
        test_build_packet_rejects_empty_session_owned_scope(root / "packet-manifest-empty")
        test_build_packet_requires_session_context(root / "packet-requires-context")
        test_build_packet_honors_review_validate_fix_ignore(root / "packet-ignore")
        test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(root / "packet-literal-ignore")
        test_prepare_review_run_and_command_lock(root / "prepare")
        test_prepare_review_run_can_build_session_manifest_from_transcript(root / "prepare-transcript")
        test_prepare_review_run_requires_session_context(root / "prepare-requires-context")
        test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout")
        test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity")
        test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json")
        test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config")
        test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config")
        test_alternative_reviewer_respects_explicit_claude_equals_text_output(
            root / "alternative-equals-text-config"
        )
        test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(root / "alternative-wrapper")
    print("review support script tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
