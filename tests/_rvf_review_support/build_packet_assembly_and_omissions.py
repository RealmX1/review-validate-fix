#!/usr/bin/env python3
"""review packet 组装与省略规则 测试簇。

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

from _rvf_test_support.compact_path_tree_assertions import paths_from_compact_path_tree_blocks

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_build_packet_metadata_and_scope',
    'test_build_packet_allows_clean_repo_with_manual_scope',
    'test_build_packet_uses_session_manifest_as_scope_anchor',
    'test_build_packet_filters_session_owned_diff_to_tracker_hunk',
    'test_build_packet_rejects_session_manifest_for_different_repo',
    'test_build_packet_rejects_empty_session_owned_scope',
    'test_build_packet_requires_session_context',
    'test_build_packet_honors_review_validate_fix_ignore',
    'test_build_packet_treats_ignore_prefixes_as_literal_pathspecs',
    'test_build_packet_excludes_nested_paths_under_review_scope_excluded_path_prefixes',
    'test_build_packet_emits_cross_session_conflict_section',
    'test_build_packet_omits_cross_session_section_when_clean',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_build_packet_metadata_and_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
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


def test_build_packet_allows_clean_repo_with_manual_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "add", "tracked.txt", "new.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "settle worktree"], cwd=repo)
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：manual scoped review\n"
        "- 本 turn 主会话实际完成的工作：仓库当前 clean；本轮审查范围来自用户显式指定\n"
        "- Scope：审查 tracked.txt 的现有实现面\n",
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"

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
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Review Scope" in packet_text
    assert "Primary files for this turn:" in packet_text
    assert "tracked.txt" in packet_text
    assert "## Git Status\n\n```text\n(clean)\n```" in packet_text
    assert "## Git Diff HEAD\n\n```diff\n(no tracked diff)\n```" in packet_text
    assert payload["status_bytes"] == 0
    assert payload["diff_bytes"] == 0
    assert payload["primary_files"] == ["tracked.txt"]
    assert payload["session_context_provided"] is True


def test_build_packet_uses_session_manifest_as_scope_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned contents\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)
    manifest = tmp_path / "manifest.json"
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

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
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
    assert "## Full Git Diff HEAD (Evidence Only)" not in packet_text
    assert "Session-owned paths:" in packet_text
    owned_section = packet_text.split("Session-owned paths:", 1)[1].split("Unattributed dirty paths", 1)[0]
    assert "tracked.txt" in paths_from_compact_path_tree_blocks(owned_section)
    background_section = packet_text.split("Unattributed dirty paths (background, not default review scope):", 1)[1]
    assert "background.txt" in paths_from_compact_path_tree_blocks(background_section.split("\n\n", 1)[0])
    assert "Background untracked paths below were not attributed to this session and are not inlined" in packet_text
    assert "### owned-new.txt" in packet_text
    assert "owned contents" in packet_text
    assert "### background.txt" not in packet_text
    assert "background contents" not in packet_text
    assert payload["session_manifest_provided"] is True
    assert payload["session_owned_path_count"] >= 3
    assert payload["owned_untracked_count"] == 1
    assert payload["background_untracked_count"] >= 2


def test_build_packet_filters_session_owned_diff_to_tracker_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n" + "\n".join(f"keep-{index}" for index in range(20)) + "\nbottom\n",
        encoding="utf-8",
    )
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n"
        "background\n"
        + "\n".join(f"keep-{index}" for index in range(20))
        + "\nbottom\n"
        + "session-owned\n",
        encoding="utf-8",
    )
    diff_text = run(["git", "diff", "-U3", "HEAD", "--", "a.txt"], cwd=repo).stdout
    selected_header: str | None = None
    current_header: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            current_header = line
        if "+session-owned" in line:
            selected_header = current_header
            break
    assert selected_header is not None

    context = tmp_path / "context.md"
    context.write_text("session context\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(repo.resolve()),
                "session_id": "S",
                "confidence": "medium",
                "owned_paths": ["a.txt"],
                "owned_dirty_paths": ["a.txt"],
                "unattributed_dirty_paths": [],
                "tracker": {
                    "status": "ok",
                    "session_id": "S",
                    "tracker_scope": {
                        "unit_ids": ["unit-session"],
                        "lease_id": "lse-test",
                        "scope_hash": "sha256:" + "a" * 64,
                        "paths": ["a.txt"],
                        "hunks": [
                            {
                                "unit_id": "unit-session",
                                "path": "a.txt",
                                "hunk_header": selected_header,
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"

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
    session_owned_diff = packet_text.split("## Full Git Diff HEAD", 1)[0]
    assert "+session-owned" in session_owned_diff
    assert "+background" not in session_owned_diff
    assert "+background" not in packet_text
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "+background" in Path(payload["repository_diff_evidence"]["file"]).read_text()


def test_build_packet_rejects_session_manifest_for_different_repo(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject mismatched manifest\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(tmp_path / "other-repo"),
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


def test_build_packet_rejects_empty_session_owned_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject empty manifest scope\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
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


def test_build_packet_requires_session_context(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
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


def test_build_packet_honors_review_validate_fix_ignore(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
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
    # Serena 会话启动时写进工作树的文件：内置前缀 .serena/ 让它们不进 packet。
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text("serena generated project config\n", encoding="utf-8")

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
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
    assert payload["excluded_path_prefixes"] == [".serena/", "secret", "slide-versions/"]
    assert payload["untracked_count"] == 3
    assert "## Excluded Paths" in packet_text
    assert "- secret" in packet_text
    assert "- slide-versions/" in packet_text
    assert "- .serena/" in packet_text
    assert ".serena/project.yml" not in packet_text
    assert "serena generated project config" not in packet_text
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


def test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
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

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
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


def _packet_section_body(packet_text: str, section_heading: str) -> str:
    """取 packet 里某个 ``## `` 二级标题下、到下一个二级标题为止的正文。"""
    start = packet_text.index(f"\n{section_heading}\n")
    next_heading = packet_text.find("\n## ", start + 1)
    return packet_text[start:] if next_heading == -1 else packet_text[start:next_heading]


def test_build_packet_excludes_nested_paths_under_review_scope_excluded_path_prefixes(tmp_path: Path) -> None:
    """排除前缀是「字符串前缀」语义，覆盖任意深度的嵌套路径，且与候选集侧 matcher 口径一致。

    回归：以前每个前缀只发一条 ``:(exclude,top,glob)<prefix>*``，glob magic 下 ``*`` 不跨 ``/``，
    于是「已跟踪文件在嵌套子目录里被修改」与「父目录已跟踪、嵌套子目录里有未跟踪文件」两种形态
    都会漏进 Git Status / Git Diff HEAD / Untracked Files 三段，而 Stop 候选集那侧早已把它们排除。
    """
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared nested excluded paths\n",
        encoding="utf-8",
    )
    (repo / ".review-validate-fix-ignore").write_text("secret\n", encoding="utf-8")
    tracked_paths_expected_excluded = [
        ".serena/project.yml",
        ".serena/memories/auth.md",
        "secret.txt",
        "secret/x.txt",
        "secret-dir/nested.txt",
    ]
    tracked_paths_expected_visible = ["keep.txt", "docs/secret/visible-tracked.txt"]
    for rel in [*tracked_paths_expected_excluded, *tracked_paths_expected_visible]:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("base\n", encoding="utf-8")
    run(["git", "add", ".review-validate-fix-ignore", *tracked_paths_expected_excluded, *tracked_paths_expected_visible], cwd=repo)
    run(["git", "commit", "-q", "-m", "add nested paths"], cwd=repo)
    for rel in tracked_paths_expected_excluded:
        (repo / rel).write_text(f"base\nexcluded tracked change in {rel}\n", encoding="utf-8")
    for rel in tracked_paths_expected_visible:
        (repo / rel).write_text(f"base\nvisible tracked change in {rel}\n", encoding="utf-8")
    untracked_paths_expected_excluded = [
        ".serena/memories/new.md",
        ".serena/cache/deep/c.bin",
        "secret/new.txt",
        "secret-dir/deeper/new.txt",
    ]
    untracked_paths_expected_visible = ["docs/secret/visible-untracked.txt"]
    for rel in untracked_paths_expected_excluded:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"excluded untracked contents in {rel}\n", encoding="utf-8")
    for rel in untracked_paths_expected_visible:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"visible untracked contents in {rel}\n", encoding="utf-8")

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
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
    assert payload["excluded_path_prefixes"] == [".serena/", "secret"]
    git_status_section = _packet_section_body(packet_text, "## Git Status")
    git_diff_section = _packet_section_body(packet_text, "## Git Diff HEAD")
    untracked_files_section = _packet_section_body(packet_text, "## Untracked Files")
    for rel in [*tracked_paths_expected_excluded, *untracked_paths_expected_excluded]:
        assert rel not in git_status_section, (rel, git_status_section)
        assert rel not in git_diff_section, (rel, git_diff_section)
        assert rel not in untracked_files_section, (rel, untracked_files_section)
    assert "excluded tracked change" not in packet_text
    assert "excluded untracked contents" not in packet_text
    # 正向对照：前缀锚定在仓库根，docs/secret/ 不以 secret 开头，不能被过度排除。
    for rel in tracked_paths_expected_visible:
        assert rel in git_status_section, (rel, git_status_section)
        assert f"visible tracked change in {rel}" in git_diff_section
    for rel in untracked_paths_expected_visible:
        assert rel in git_status_section, (rel, git_status_section)
        assert f"### {rel}" in untracked_files_section
        assert f"visible untracked contents in {rel}" in untracked_files_section
    # 候选集侧（Stop 发起闸）与 packet 侧口径统一：同一批路径的「排除 / 保留」结论逐条一致。
    if str(BUILD_PACKET.parent) not in sys.path:
        sys.path.insert(0, str(BUILD_PACKET.parent))
    import review_scope_excluded_path_prefixes as prefixes_module

    for rel in [*tracked_paths_expected_excluded, *untracked_paths_expected_excluded]:
        assert prefixes_module.path_matches_review_scope_excluded_path_prefix(rel, payload["excluded_path_prefixes"]), rel
    for rel in [*tracked_paths_expected_visible, *untracked_paths_expected_visible]:
        assert not prefixes_module.path_matches_review_scope_excluded_path_prefix(rel, payload["excluded_path_prefixes"]), rel


def test_build_packet_emits_cross_session_conflict_section(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    # Pre-register a claim from a different session so the current run sees a conflict.
    module.register_claims(
        repo=repo,
        session_id="other-session",
        run_id="run-other",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"
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
            "run-current",
        ],
        env=env,
    )
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：cross-session conflict test\n"
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
            "--session-manifest",
            str(manifest_path),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ],
        env=env,
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Cross-Session Conflicts" in packet_text
    assert "other-session" in packet_text
    assert payload["cross_session_conflicts"]
    assert payload["cross_session_conflicts"][0]["other_session_id"] == "other-session"


def test_build_packet_omits_cross_session_section_when_clean(tmp: Path) -> None:
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
            "run-1",
        ],
        env=env,
    )
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：no conflict path\n"
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
            "--session-manifest",
            str(manifest_path),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ],
        env=env,
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Cross-Session Conflicts" not in packet_text
    assert payload["cross_session_conflicts"] == []
