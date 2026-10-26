"""审查正文始终来自冻结 Git 候选，活动文件短暂改动不能改变 reviewer 输入。"""

import hashlib
import json
import os
from pathlib import Path
import pytest

from test_review_start_independent_user_turn_boundary import valid_boundary, isolate_runtime_state, git


def freeze_packet_test_candidate(repo, tmp_path):
    environment = dict(os.environ, GIT_INDEX_FILE=str(tmp_path / "packet-candidate-index"))
    git(repo, "read-tree", "HEAD", env=environment)
    git(repo, "add", "--all", env=environment)
    tree = git(repo, "write-tree", env=environment)
    return git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "test: 冻结 packet 输入")


def test_frozen_packet_ignores_transient_live_content_and_scope_policy(valid_boundary, tmp_path):
    import build_review_packet
    f = valid_boundary
    repo = f["repo"]
    (repo / ".review-validate-fix-ignore").write_text("excluded-\n")
    (repo / "new-source.py").write_text("new_value = 2\n")
    (repo / "excluded-source.py").write_text("excluded = 2\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    comparison = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 99\n")
    (repo / "new-source.py").write_text("new_value = 99\n")
    (repo / ".review-validate-fix-ignore").write_text("")
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, ["a.py", "new-source.py"], [],
        allow_missing_session_context=True, base_ref_override=comparison,
        frozen_candidate_commit=candidate,
    )
    assert "+value = 2" in packet
    assert "value = 99" not in packet
    assert "new_value = 2" in packet
    assert "new_value = 99" not in packet
    assert "excluded-source.py" not in packet
    item = next(item for item in metadata["untracked_files"] if item["path"] == "new-source.py")
    assert item["sha256"] == hashlib.sha256(b"new_value = 2\n").hexdigest()
    assert metadata["frozen_candidate_commit"] == candidate
    assert git(repo, "rev-parse", "HEAD") == comparison


def test_frozen_packet_rejects_invalid_utf8_review_scope_policy(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / ".review-validate-fix-ignore").write_bytes(b"excluded-\xff\n")
    (repo / "excluded-\ufffd.py").write_text("不能被替换解码后虚构的策略排除\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    with pytest.raises(UnicodeDecodeError):
        build_review_packet.build_packet(
            repo, None, None, 10000, [], [], allow_missing_session_context=True,
            frozen_candidate_commit=candidate,
            frozen_packet_context_only_paths=[".review-validate-fix-ignore"],
        )


def test_frozen_packet_keeps_committed_and_dirty_tracker_projection(valid_boundary, tmp_path):
    import build_review_packet
    f = valid_boundary
    repo = f["repo"]
    comparison = git(repo, "rev-parse", "HEAD")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 已提交的同轮修改")
    (repo / "a.py").write_text("value = 3\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "packet-session-manifest.json"
    manifest.write_text(json.dumps({
        "repo": str(repo), "owned_paths": ["a.py"], "owned_dirty_paths": ["a.py"],
        "owned_committed_round_paths": ["a.py"], "committed_round_baseline": comparison,
        "tracker": {"tracker_scope": {
            "unit_ids": ["isolated-packet-unit"], "lease_id": "isolated-packet-lease",
            "scope_hash": "isolated-packet-scope", "paths": ["a.py"],
        }},
    }))
    (repo / "a.py").write_text("value = 99\n")
    packet, metadata = build_review_packet.build_packet(
        repo, None, manifest, 10000, ["a.py"], [],
        allow_missing_session_context=True, base_ref_override=comparison,
        frozen_candidate_commit=candidate,
    )
    allocated = packet.split("## Allocated Git Diff", 1)[1].split("## Full Git Diff", 1)[0]
    assert "-value = 1" in allocated
    assert "+value = 3" in allocated
    assert "value = 99" not in packet
    assert metadata["owned_diff_base_ref"] == comparison


def test_prepare_and_scope_rerender_keep_the_same_frozen_candidate(valid_boundary, tmp_path, monkeypatch):
    import prepare_review_run
    f = valid_boundary
    context = tmp_path / "approved-frozen-packet-scope.md"
    context.write_text("审查 a.py 中 value 从 1 改为 2 的实现。\n")
    original_run = prepare_review_run.run
    rendered_commands = []
    def render_while_live_file_temporarily_changes(command, **kwargs):
        if len(command) > 1 and command[1] == str(prepare_review_run.BUILD_PACKET):
            rendered_commands.append(command)
            (f["repo"] / "a.py").write_text("value = 99\n")
            try:
                return original_run(command, **kwargs)
            finally:
                (f["repo"] / "a.py").write_text("value = 2\n")
        return original_run(command, **kwargs)
    monkeypatch.setattr(prepare_review_run, "run", render_while_live_file_temporarily_changes)
    prepared = prepare_review_run.prepare_run(
        repo=f["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[], decision_token=f["token"],
    )
    run = Path(prepared["run_dir"])
    packet = (run / "artifacts" / "review-packet.md").read_text()
    assert "+value = 2" in packet
    assert "value = 99" not in packet
    command = rendered_commands[0]
    assert "--frozen-candidate-commit" in command
    candidate = command[command.index("--frozen-candidate-commit") + 1]
    assert len(candidate) == 40
    recorded = json.loads((run / "artifacts" / "inputs" / "review-packet.build-command.json").read_text())
    assert recorded["args"][recorded["args"].index("--frozen-candidate-commit") + 1] == candidate
    context.write_text("补充同一冻结候选的范围说明。\n")
    render_while_live_file_temporarily_changes(command)
    assert "value = 99" not in (run / "artifacts" / "review-packet.md").read_text()
    f["gate"].require_prepared_run_review_start_boundary(
        repo=str(f["repo"]), scope_contract=prepared["scope_contract"],
        run_id=prepared["run_id"], run_dir=prepared["run_dir"],
    )


def test_frozen_packet_keeps_file_modes_rename_delete_and_link_blobs(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "previous-name.txt").write_text("固定重命名内容\n")
    (repo / "deleted-source.txt").write_text("固定删除内容\n")
    git(repo, "add", "previous-name.txt", "deleted-source.txt")
    git(repo, "commit", "-qm", "test: 冻结文件类型基线")
    comparison = git(repo, "rev-parse", "HEAD")
    (repo / "previous-name.txt").rename(repo / "renamed-source.txt")
    (repo / "deleted-source.txt").unlink()
    (repo / "a.py").chmod(0o755)
    (repo / "repository-link").symlink_to("missing-frozen-target")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    (repo / "a.py").chmod(0o644)
    (repo / "repository-link").unlink()
    (repo / "repository-link").write_text("活动文件替换了链接\n")
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, [], [], allow_missing_session_context=True,
        base_ref_override=comparison, frozen_candidate_commit=candidate,
    )
    assert "new mode 100755" in packet
    assert "rename from previous-name.txt" in packet
    assert "rename to renamed-source.txt" in packet
    assert "deleted file mode 100644" in packet
    assert "missing-frozen-target" in packet
    assert "活动文件替换了链接" not in packet
    link = next(item for item in metadata["untracked_files"] if item["path"] == "repository-link")
    assert link["git_mode"] == "120000"


@pytest.mark.parametrize("scope_kind", ["full", "owned", "tracker"])
def test_frozen_packet_blob_omission_and_symlinked_ignore_policy(valid_boundary, tmp_path, scope_kind):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "frozen-ignore-policy").write_text("excluded-\n")
    (repo / ".review-validate-fix-ignore").symlink_to("frozen-ignore-policy")
    (repo / "excluded-source.py").write_text("不得进入审查\n")
    (repo / "large-source.txt").write_text("x" * 128)
    (repo / "binary-source.bin").write_bytes(b"\0binary")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    (repo / "frozen-ignore-policy").write_text("")
    manifest = None
    if scope_kind != "full":
        manifest = tmp_path / "new-file-scope.json"
        payload = {"repo": str(repo), "owned_paths": ["large-source.txt", "binary-source.bin"]}
        if scope_kind == "tracker":
            payload["tracker"] = {"tracker_scope": {
                "unit_ids": ["new-file-unit"], "lease_id": "new-file-lease", "scope_hash": "new-file-scope",
                "paths": ["large-source.txt", "binary-source.bin"],
            }}
        manifest.write_text(json.dumps(payload))
    packet, metadata = build_review_packet.build_packet(
        repo, None, manifest, 8, [], [], allow_missing_session_context=True,
        frozen_candidate_commit=candidate,
    )
    assert "excluded-source.py" not in packet
    items = {item["path"]: item for item in metadata["untracked_files"]}
    assert items["large-source.txt"]["omitted"]
    assert items["large-source.txt"]["sha256"] == hashlib.sha256(b"x" * 128).hexdigest()
    assert items["binary-source.bin"]["reason"] == "binary file"
    assert "x" * 128 not in packet


def test_frozen_tracker_projection_keeps_only_allocated_dirty_hunk(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    original = [f"unchanged line {index}" for index in range(50)]
    (repo / "a.py").write_text("\n".join(original) + "\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 多 hunk 范围基线")
    original[1] = "allocated candidate change"
    original[45] = "unallocated background change"
    (repo / "a.py").write_text("\n".join(original) + "\n")
    headers = [line for line in git(repo, "diff", "HEAD", "--", "a.py").splitlines() if line.startswith("@@")]
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "allocated-dirty-hunk.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["a.py"], "owned_dirty_paths": ["a.py"],
        "tracker": {"tracker_scope": {"unit_ids": ["first-hunk"], "lease_id": "hunk-lease", "scope_hash": "hunk-scope",
            "paths": ["a.py"], "hunks": [{"path": "a.py", "hunk_header": headers[0]}]}}}))
    packet, _ = build_review_packet.build_packet(
        repo, None, manifest, 10000, [], [], allow_missing_session_context=True, frozen_candidate_commit=candidate,
    )
    allocated = packet.split("## Allocated Git Diff", 1)[1].split("## Full Git Diff", 1)[0]
    assert "allocated candidate change" in allocated
    assert "unallocated background change" not in allocated


@pytest.mark.parametrize("attributes_source", ["worktree", "info", "default_home", "default_xdg"])
def test_frozen_diff_does_not_read_live_or_info_git_attributes(valid_boundary, tmp_path, monkeypatch, attributes_source):
    import build_review_packet
    f = valid_boundary
    snapshot = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    if attributes_source == "worktree":
        attributes = f["repo"] / ".gitattributes"
    elif attributes_source == "info":
        attributes = f["repo"] / ".git" / "info" / "attributes"
    else:
        home = tmp_path / "isolated-attributes-home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        config = home / ".config"
        if attributes_source == "default_xdg":
            config = home / "isolated-xdg-config"
            monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        attributes = config / "git" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.py -diff\n")
    packet, _ = build_review_packet.build_packet(
        f["repo"], None, None, 10000, [], [], allow_missing_session_context=True,
        frozen_candidate_commit=snapshot["checkpoint_commit"],
    )
    assert "+value = 2" in packet
    assert "Binary files" not in packet


def test_frozen_policy_resolves_absolute_repository_link_and_directory_links(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "real-policy-directory").mkdir()
    (repo / "real-policy-directory" / "policy.txt").write_text("excluded-\n")
    (repo / "policy-directory-link").symlink_to("real-policy-directory", target_is_directory=True)
    (repo / ".review-validate-fix-ignore").symlink_to(repo / "policy-directory-link" / "policy.txt")
    (repo / "excluded-source.py").write_text("excluded = 2\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, [], [], allow_missing_session_context=True, frozen_candidate_commit=candidate,
    )
    assert "excluded-" in metadata["excluded_path_prefixes"]
    assert "excluded-source.py" not in packet


@pytest.mark.parametrize("invalid_candidate", ["HEAD", "0" * 40])
def test_frozen_packet_refuses_mutable_or_missing_candidate_refs(valid_boundary, invalid_candidate):
    import build_review_packet
    with pytest.raises((ValueError, RuntimeError)):
        build_review_packet.build_packet(
            valid_boundary["repo"], None, None, 10000, [], [],
            allow_missing_session_context=True, frozen_candidate_commit=invalid_candidate,
        )


def test_frozen_policy_context_is_used_without_becoming_review_code(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "policy-only-context").write_text("excluded-\n# PRIVATE_CONTEXT_MARKER\n")
    (repo / ".review-validate-fix-ignore").symlink_to("policy-only-context")
    (repo / "excluded-source.py").write_text("excluded = 2\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, [], [], allow_missing_session_context=True,
        frozen_candidate_commit=candidate,
        frozen_packet_context_only_paths=["policy-only-context", ".review-validate-fix-ignore"],
    )
    assert "excluded-" in metadata["excluded_path_prefixes"]
    assert "excluded-source.py" not in packet
    assert "PRIVATE_CONTEXT_MARKER" not in packet
    assert "policy-only-context" not in {item["path"] for item in metadata["untracked_files"]}


def test_frozen_policy_resolves_parent_after_directory_symlink_expansion(valid_boundary, tmp_path):
    import build_review_packet
    import stop_decision_candidate_summary
    f = valid_boundary
    repo = f["repo"]
    (repo / "real-policy" / "deep").mkdir(parents=True)
    (repo / "real-policy" / "policy.txt").write_text("excluded-\n")
    (repo / "policy.txt").write_text("")
    (repo / "directory-link").symlink_to("real-policy/deep", target_is_directory=True)
    git(repo, "add", "directory-link", "real-policy/policy.txt", "policy.txt")
    git(repo, "commit", "-qm", "test: 保留策略路径中的空目录语义")
    (repo / ".review-validate-fix-ignore").symlink_to("directory-link/../policy.txt")
    (repo / "excluded-source.py").write_text("excluded = 2\n")
    assert (repo / ".review-validate-fix-ignore").read_text() == "excluded-\n"
    summary = stop_decision_candidate_summary.build_candidate_summary(
        event={"session_id": f["payload"]["session_id"], "cwd": str(repo)}, cwd=str(repo), repo=str(repo), ledger=None,
    )
    token = f["tokens"].mint_decision_token(
        worktree=str(repo), session_id=f["payload"]["session_id"],
        candidate_summary=summary, candidate_fingerprint=summary["candidate_fingerprint"],
    ).token
    snapshot = f["gate"].require_review_start_boundary(token_payload=f["tokens"].read_decision_token(token).payload, repo=str(repo))
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, [], [], allow_missing_session_context=True,
        frozen_candidate_commit=snapshot["checkpoint_commit"], frozen_packet_context_only_paths=snapshot["packet_context_only_paths"],
    )
    assert "excluded-" in metadata["excluded_path_prefixes"]
    assert "excluded-source.py" not in packet


def test_frozen_tracker_projection_matches_ranges_when_function_titles_differ(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / ".gitattributes").write_text("source.txt diff=packet_scope\n")
    git(repo, "config", "diff.packet_scope.xfuncname", "^SECTION.*")
    lines = ["SECTION_ALPHA", *[f"noise {index}" for index in range(40)]]
    (repo / "source.txt").write_text("\n".join(lines) + "\n")
    git(repo, "add", ".gitattributes", "source.txt")
    git(repo, "commit", "-qm", "test: 自定义函数标题基线")
    lines[18] = "allocated frozen change"
    (repo / "source.txt").write_text("\n".join(lines) + "\n")
    header = next(line for line in git(repo, "diff", "-U3", "HEAD", "--", "source.txt").splitlines() if line.startswith("@@"))
    assert "SECTION_ALPHA" in header
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "custom-title-hunk.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["source.txt"], "owned_dirty_paths": ["source.txt"],
        "tracker": {"tracker_scope": {"unit_ids": ["custom-title-hunk"], "lease_id": "custom-title-lease", "scope_hash": "custom-title-scope",
            "paths": ["source.txt"], "hunks": [{"path": "source.txt", "hunk_header": header}]}}}))
    packet, _ = build_review_packet.build_packet(
        repo, None, manifest, 10000, [], [], allow_missing_session_context=True, frozen_candidate_commit=candidate,
    )
    allocated = packet.split("## Allocated Git Diff", 1)[1].split("## Full Git Diff", 1)[0]
    assert "+allocated frozen change" in allocated


def test_frozen_packet_does_not_inline_unowned_new_file_contents(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "owned-new.py").write_text("owned frozen contents\n")
    (repo / "background-new.py").write_text("BACKGROUND_CONTENTS_MUST_NOT_BE_INLINED\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "owned-new-file-scope.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["owned-new.py"], "owned_dirty_paths": ["owned-new.py"]}))
    packet, metadata = build_review_packet.build_packet(
        repo, None, manifest, 10000, ["owned-new.py"], ["background-new.py"],
        allow_missing_session_context=True, frozen_candidate_commit=candidate,
    )
    assert "owned frozen contents" in packet
    assert "BACKGROUND_CONTENTS_MUST_NOT_BE_INLINED" not in packet
    assert metadata["background_untracked_count"] >= 1


@pytest.mark.parametrize("diff_setting", ["algorithm", "interhunkcontext"])
@pytest.mark.parametrize("changed_after_allocation", [False, True])
def test_frozen_tracker_diff_settings_keep_allocated_hunks_or_fail_closed(valid_boundary, tmp_path, diff_setting, changed_after_allocation):
    import build_review_packet
    repo = valid_boundary["repo"]
    if diff_setting == "algorithm":
        original = "delta alpha delta delta alpha beta alpha beta delta beta alpha gamma epsilon alpha alpha alpha epsilon beta epsilon alpha gamma epsilon alpha alpha beta epsilon".split()
        modified = "delta alpha delta delta beta alpha gamma epsilon alpha beta alpha beta delta alpha alpha alpha epsilon beta epsilon alpha gamma epsilon alpha alpha beta epsilon".split()
        value = "histogram"
    else:
        original = [f"unchanged line {index}" for index in range(50)]
        modified = list(original)
        modified[5] = "first allocated candidate change"
        modified[22] = "second allocated candidate change"
        value = "20"
    (repo / "source.txt").write_text("\n".join(original) + "\n")
    git(repo, "add", "source.txt")
    git(repo, "commit", "-qm", "test: 分配侧 diff 配置基线")
    git(repo, "config", "diff." + diff_setting, value)
    (repo / "source.txt").write_text("\n".join(modified) + "\n")
    headers = [line for line in git(repo, "diff", "-U3", "HEAD", "--", "source.txt").splitlines() if line.startswith("@@")]
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "configured-allocated-hunks.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["source.txt"], "owned_dirty_paths": ["source.txt"],
        "tracker": {"tracker_scope": {"unit_ids": ["configured-hunk"], "lease_id": "configured-lease", "scope_hash": "configured-scope",
            "paths": ["source.txt"], "hunks": [{"path": "source.txt", "hunk_header": header} for header in headers]}}}))
    if changed_after_allocation:
        git(repo, "config", "--unset", "diff." + diff_setting)
    def render():
        return build_review_packet.build_packet(
            repo, None, manifest, 10000, [], [], allow_missing_session_context=True, frozen_candidate_commit=candidate,
        )
    if changed_after_allocation:
        with pytest.raises(ValueError, match="已分配.*hunk"):
            render()
    else:
        packet, _ = render()
        allocated = packet.split("## Allocated Git Diff", 1)[1].split("## Full Git Diff", 1)[0]
        assert "(no tracked diff for allocated paths)" not in allocated
        assert "@@" in allocated
