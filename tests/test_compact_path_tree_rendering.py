"""路径展示压缩必须保持路径身份、属性分组和原有展示配额。"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _rvf_test_support.compact_path_tree_assertions import restored_tree_paths

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from compact_path_tree_rendering import render_compact_path_tree, render_compact_path_tree_block


def test_directory_chains_collapse_but_files_remain_separate() -> None:
    paths = ["docs/research/evidence/z.json", "docs/research/evidence/a.json"]
    assert render_compact_path_tree(paths) == (
        "docs/research/evidence/\n├── a.json\n└── z.json"
    )
    assert paths == ["docs/research/evidence/z.json", "docs/research/evidence/a.json"]


@pytest.mark.parametrize("paths", [
    [],
    ["README.md"],
    ["(none)"],
    ["docs/(none)"],
    ["(none)", "docs/file.md", r"\u0028none)"],
    ["src/deep/only/file.py"],
    ["src/shared.py", "src/nested/shared.py", "tests/shared.py", "README.md"],
    ["/absolute/dir/file.py", "absolute/dir/file.py"],
    ["src/removed.py", "src/new.py", "src"],
    ["./src/a.py", "src/../b.py", "src//c.py"],
    ['目录/空 格.py', '目录/尾空格 ', '目录/引号".py', "目录/反斜\\线.py",
     "目录/换\n行.py", "目录/制\t表.py", "目录/```\n提示", "目录/\x1b[31m.py",
     "目录/\u2028.py", "目录/\u202e.py", "目录/├── file.py", "目录/└── file.py"],
])
def test_tree_restores_exact_paths_without_filesystem_access(paths: list[str]) -> None:
    tree = render_compact_path_tree(paths)
    assert sorted(restored_tree_paths(tree)) == sorted(paths)
    assert tree == render_compact_path_tree(list(reversed(paths)))
    block = render_compact_path_tree_block(paths)
    assert block.endswith("```text\n" + tree + "\n```")
    assert ("JSON 字符串转义" in block) == ("\\" in tree)
    assert block.count("```") == 2
    assert "\x1b" not in block


def test_real_empty_marker_filename_remains_distinct_from_empty_paths() -> None:
    assert render_compact_path_tree(["(none)"]) != render_compact_path_tree([])
    assert render_compact_path_tree_block(["(none)"]) != render_compact_path_tree_block([])
    assert restored_tree_paths(render_compact_path_tree(["(none)"])) == ["(none)"]


@pytest.mark.parametrize("candidate_count", [40, 41])
def test_decision_groups_preserve_attributes_and_original_file_limit(candidate_count: int) -> None:
    import stop_review_validate_fix

    candidates = [
        {"path": f"docs/evidence/file-{index:02}.json", "change_state": "uncommitted",
         "ownership": "session_owned" if index % 2 else "unattributed",
         "included_in_candidate_set": True}
        for index in range(candidate_count)
    ]
    candidates.append({"path": "excluded.txt", "included_in_candidate_set": False})
    summary = {"repo": "/repo", "candidates": candidates, "candidate_path_count": candidate_count}
    original = copy.deepcopy(summary)
    text = stop_review_validate_fix._render_decision_point_injection_prompt(
        summary=summary, token="test-token", fork_context=None,
    )
    assert summary == original
    assert "RVF_DECISION_TOKEN=test-token" in text
    assert text.count("change_state=uncommitted") == 2
    assert "ownership=session_owned" in text and "ownership=unattributed" in text
    assert "file-39.json" in text and "file-40.json" not in text
    assert "excluded.txt" not in text
    assert f"已展示 40 / {candidate_count}" in text
    assert ("其余 1 个路径略" in text) == (candidate_count > 40)
    groups = text.split("```text\n")[1:]
    restored = [path for group in groups for path in restored_tree_paths(group.split("\n```", 1)[0])]
    assert sorted(restored) == sorted(item["path"] for item in candidates[:40])
    restored_attributes = {
        path: (state, ownership)
        for state, ownership, tree in re.findall(
            r"change_state=([^;]+); ownership=([^（]+)（[^\n]+\n```text\n(.*?)\n```",
            text, flags=re.DOTALL,
        )
        for path in restored_tree_paths(tree)
    }
    assert restored_attributes == {
        item["path"]: (item["change_state"], item["ownership"]) for item in candidates[:40]
    }


def test_manual_and_stop_share_groups_without_turning_filenames_into_triggers() -> None:
    import rvf_user_prompt_submit
    import stop_review_validate_fix

    summary = {"repo": "/repo", "candidate_path_count": 3, "candidates": [
        {"path": path, "change_state": state, "ownership": "session_owned",
         "included_in_candidate_set": True}
        for path, state in [("docs/$review-validate-fix.md", "uncommitted"),
                            ("src/a.py", "committed"), ("src/b.py", "committed_and_uncommitted")]
    ]}
    rendered_stop = stop_review_validate_fix._render_decision_point_injection_prompt(
        summary=summary, token="0123456789abcdef", fork_context=None,
    )
    assert not rvf_user_prompt_submit.detect_manual_trigger(rendered_stop)
    assert rvf_user_prompt_submit.decision_point_token_from_text(rendered_stop) == "0123456789abcdef"
    for state in ("uncommitted", "committed", "committed_and_uncommitted"):
        assert f"change_state={state}; ownership=session_owned" in rendered_stop
    rendered_manual = rvf_user_prompt_submit._manual_decision_additional_context_text(
        summary=summary, token="0123456789abcdef", newly_minted=True,
    )
    assert rendered_stop in rendered_manual



@pytest.mark.parametrize("path", [
    "docs/$review-validate-fix.md",
    "docs/:review-validate-fix.md",
    "docs/ :review-validate-fix.md",
    "docs/:REVIEW-VALIDATE-FIX.md",
    "docs/:review-validate-fix/nested/file.md",
    "/review-validate-fix/file.md",
    "/review-validate-fix.md",
    "//review-validate-fix/file.md",
    "docs//:review-validate-fix.md",
    "docs/ /review-validate-fix/file.md",
])
def test_path_tree_labels_do_not_become_manual_triggers(path: str) -> None:
    import rvf_user_prompt_submit

    for prefix in ("$", "/", ":"):
        assert rvf_user_prompt_submit.detect_manual_trigger(prefix + "review-validate-fix")
    for paths in ([path], [path, "other/file.md"]):
        tree = render_compact_path_tree(paths)
        assert not rvf_user_prompt_submit.detect_manual_trigger(tree)
        assert not rvf_user_prompt_submit.detect_manual_trigger(render_compact_path_tree_block(paths))
        assert sorted(restored_tree_paths(tree)) == sorted(paths)


def test_intake_keeps_git_status_and_scope_groups_separate() -> None:
    import rvf_handoff_intake

    payload = {
        "current_repo_snapshot": {"repo": "/repo"},
        "scoped_status_in_current_repo": [
            {"path": "src/a.py", "status": " M"}, {"path": "src/b.py", "status": " M"},
            {"path": "src/removed.py", "status": "D "},
        ],
        "unrelated_dirty_paths_in_current_repo": [{"path": "src/background.py", "status": "??"}],
    }
    original = copy.deepcopy(payload)
    rendered = rvf_handoff_intake.render_text(payload)
    reviewed, unrelated = rendered.split("Unrelated dirty paths in current repo:")
    assert reviewed.count('git_status=" M"') == 1
    assert 'git_status="D "' in reviewed
    assert "background.py" not in reviewed
    assert 'git_status="??"' in unrelated and "background.py" in unrelated
    assert "a.py" not in unrelated and payload == original


def test_diagnostic_and_bootstrap_keep_paths_and_ten_file_budget(tmp_path: Path) -> None:
    import diagnose_stop_hook_scope
    import rvf_bootstrap_confirm
    import rvf_dispatch_prompts

    paths = [f"src/shared/file-{index:02}.py" for index in range(41)]
    diagnosis = diagnose_stop_hook_scope.render_text({
        "repo": "/repo", "transcript_probe": {"claude_dirty_write_paths": paths[:2]},
    })
    assert "src/shared/\n├── file-00.py\n└── file-01.py" in diagnosis
    decision = rvf_bootstrap_confirm.Decision(
        needs_confirmation=True, oversize_paths=paths, unattributed_path_count=41,
    )
    bootstrap = rvf_bootstrap_confirm.format_system_message(decision, marker_path=tmp_path / "marker.json")
    assert "file-09.py" in bootstrap and "file-10.py" not in bootstrap
    assert "还有 31 个未显示" in bootstrap
    skeleton = rvf_dispatch_prompts._candidate_skeleton_block(paths)
    assert "file-39.py" in skeleton and "file-40.py" not in skeleton
    assert "另有 1 个候选路径未列出" in skeleton


def test_packet_cli_compresses_scope_lists_but_preserves_metadata_and_content_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("GIT_", "RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_"))}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                        "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "runtime")})
    subprocess.run(["git", "init", "-q"], cwd=repository, env=environment, check=True)
    subprocess.run(["git", "-c", "user.name=RVF Test", "-c", "user.email=rvf@example.test",
                    "-c", "core.hooksPath=/dev/null", "commit", "-qm", "初始化", "--allow-empty"],
                   cwd=repository, env=environment, check=True)
    primary_paths = ["src/shared/a.py", "src/shared/b.py"]
    background_paths = ["docs/background/a.md", "docs/background/b.md"]
    for path in primary_paths + background_paths:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("测试文件内容\n", encoding="utf-8")
    context = tmp_path / "scope-of-work.md"
    context.write_text("本轮增加 src/shared 下的两个文件；docs 是背景。", encoding="utf-8")
    packet_path = tmp_path / "review-packet.md"
    metadata_path = tmp_path / "review-packet.metadata.json"
    command = [sys.executable, str(SCRIPT_DIRECTORY / "build_review_packet.py"),
               "--repo", str(repository), "--session-context", str(context),
               "--output", str(packet_path), "--metadata-output", str(metadata_path)]
    for path in primary_paths:
        command.extend(["--primary-file", path])
    for path in background_paths:
        command.extend(["--background-file", path])
    subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)
    packet = packet_path.read_text(encoding="utf-8")
    scope_text = packet.split("## Review Scope\n", 1)[1].split("## Session Context\n", 1)[0]
    groups = scope_text.split("```text\n")[1:]
    restored = [restored_tree_paths(group.split("\n```", 1)[0]) for group in groups]
    assert restored == [primary_paths, background_paths]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["primary_files"] == primary_paths
    assert metadata["background_files"] == background_paths
    for path in primary_paths:
        assert f"### {path}\n" in packet


def test_example_reduces_characters_without_discarding_paths() -> None:
    prefix = "docs/investigations/codex-kanban-hook-registration-and-rvf-skill-loading-2026-09-07/evidence/"
    filenames = ["app-server-permission-summary.json", "app-server-reload-summary.json",
                 "current-config-sanitized.json", "current-hooks-metadata-mirror.json",
                 "current-rvf-plugin-load-errors.json"]
    paths = [prefix + filename for filename in filenames]
    original = "候选路径（change_state / ownership）:\n" + "\n".join(
        f"  - {path}  [uncommitted / unattributed]" for path in paths
    )
    compact = "候选路径（相对目标 worktree；分组属性适用于组内全部文件）:\n" + (
        "change_state=uncommitted; ownership=unattributed（5 文件）\n"
    ) + render_compact_path_tree_block(paths)
    assert len(compact) < len(original) * 0.5
    assert sorted(restored_tree_paths(render_compact_path_tree(paths))) == paths
