"""正式分配范围、冻结证据与最终输入预算的行为回归。"""

import hashlib
import json
from pathlib import Path

import pytest

from test_review_start_independent_user_turn_boundary import valid_boundary, isolate_runtime_state, git
from test_review_packet_from_frozen_candidate_snapshot import freeze_packet_test_candidate


def test_committed_and_dirty_units_keep_separate_frozen_evidence(valid_boundary, tmp_path):
    import build_review_packet
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    lines = [f"原始行 {index}" for index in range(80)]
    (repo / "a.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 范围投影基线")
    baseline = git(repo, "rev-parse", "HEAD")
    lines[2] = "本轮已提交内容"
    (repo / "a.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 同轮提交")
    lines[72] = "本轮尚未提交内容"
    (repo / "a.py").write_text("\n".join(lines) + "\n")
    prior_unit_ids = []
    for committed_paths in ({"a.py"}, set()):
        registered = tracker.register_claims(repo=repo, session_id="frozen-evidence-session", run_id=None,
            worktree=repo, branch=None, owned_paths=["a.py"], apply_patch_paths=set(), exec_only_paths=set(),
            committed_paths=committed_paths, committed_baseline=baseline,
            log_root_override=tmp_path / "tracker-state")
        if committed_paths:
            prior_unit_ids = registered.claim_ids
    allocated = tracker.allocate_review_scope(
        repo=repo, session_id="frozen-evidence-session", run_id="frozen-evidence-run",
        reviewer_id="independent-reviewers", committed_baseline=baseline, log_root_override=tmp_path / "tracker-state",
        union_with_prior_scope_unit_ids=prior_unit_ids,
    )
    scope = allocated["scope"]
    assert {entry["frozen_evidence"]["source"] for entry in scope["hunks"]} == {"dirty", "committed"}
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["a.py"],
        "owned_dirty_paths": ["a.py"], "committed_round_baseline": baseline,
        "tracker": {"tracker_scope": scope}}))
    (repo / "a.py").write_text("冻结之后出现的其他内容\n")
    git(repo, "config", "diff.interHunkContext", "100")
    packet, metadata = build_review_packet.build_packet(
        repo, None, manifest, 10000, [], [], allow_missing_session_context=True,
        frozen_candidate_commit=candidate, evidence_directory=tmp_path / "frozen-review-evidence",
    )
    assert packet.count("+本轮已提交内容") == 1
    assert packet.count("+本轮尚未提交内容") == 1
    assert "冻结之后出现的其他内容" not in packet
    assert "## Full Git Diff" not in packet
    index = json.loads(Path(metadata["frozen_evidence_index"]).read_text())
    assert index["candidate_commit"] == candidate
    assert {entry["unit_id"] for entry in index["units"]} == set(scope["unit_ids"])
    assert all(entry["evidence_status"] == "verified" for entry in index["units"])
    # 即使 Git blob 身份仍正确，枚举阶段记录的全文摘要漂移也必须保留为缺口。
    scope["hunks"][0]["frozen_evidence"]["postimage_sha256"] = "0" * 64
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["a.py"],
        "committed_round_baseline": baseline, "tracker": {"tracker_scope": scope}}))
    _, mismatched = build_review_packet.build_packet(repo, None, manifest, 10000, [], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate,
        evidence_directory=tmp_path / "mismatched-frozen-review-evidence")
    assert mismatched["frozen_evidence_complete"] is False
    assert "内容摘要" in mismatched["frozen_evidence_gaps"][0]["evidence_gap"]


def test_final_prompt_budget_switches_to_references(tmp_path):
    import run_alternative_reviewer as runner

    prompt = tmp_path / "reviewer.md"
    prompt.write_text("审查整个正式范围。\n")
    packet = tmp_path / "review-packet.md"
    packet.write_text("源代码" * 100000)
    built = runner.build_prompt(prompt, None, packet, tmp_path)
    assert len(built.encode()) <= 128 * 1024
    assert str(packet) in built
    assert "源代码" * 100 not in built


def test_frozen_evidence_pages_bound_long_lines_and_keep_bytes(tmp_path):
    from core.session_scope_allocation.frozen_review_unit_evidence import read_frozen_evidence_page

    evidence = tmp_path / "single-long-line.txt"
    content = ("很长的源码行" * 20000).encode() + b"\x00\xff"
    evidence.write_bytes(content)
    import hashlib
    import base64
    offset = 0
    recovered = bytearray()
    while offset < len(content):
        page = read_frozen_evidence_page(evidence, expected_sha256=hashlib.sha256(content).hexdigest(), offset=offset)
        chunk = base64.b64decode(page["content_base64"])
        assert len(chunk) <= 32 * 1024
        recovered.extend(chunk)
        offset = page["next_offset"]
    assert bytes(recovered) == content


def test_same_hunk_coordinates_do_not_inline_unassigned_committed_change(valid_boundary, tmp_path):
    import build_review_packet
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    repo = valid_boundary["repo"]
    (repo / "a.py").write_text("value = 0\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 历史基线")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 非本轮历史")
    (repo / "a.py").write_text("value = 2\n")
    spec = tracker._specs_from_observation(tracker._classify_path(repo, "a.py"), "a.py")[0]
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "scope.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["a.py"], "committed_round_baseline": baseline,
        "tracker": {"tracker_scope": {"unit_ids": [spec.unit_id], "lease_id": "frozen-lease", "scope_hash": "frozen-scope",
            "paths": ["a.py"], "hunks": [{"unit_id": spec.unit_id, "path": "a.py", "hunk_header": spec.hunk_header}]}}}))
    packet, metadata = build_review_packet.build_packet(repo, None, manifest, 10000, [], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate, evidence_directory=tmp_path / "evidence")
    assert "-value = 1" in packet and "+value = 2" in packet
    assert "+value = 1" not in packet and "-value = 0" not in packet
    assert metadata["frozen_evidence_complete"] is True


def test_generated_source_relation_keeps_raw_artifact_in_scope(valid_boundary, tmp_path):
    import build_review_packet
    repo = valid_boundary["repo"]
    (repo / "source.ts").write_text("export const value = 2;\n")
    (repo / "generated.js").write_text("// Generated from source.ts\nexport const value=2;\n")
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    packet, metadata = build_review_packet.build_packet(repo, None, None, 10000, ["generated.js"], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate, evidence_directory=tmp_path / "evidence")
    unit = metadata["frozen_evidence_units"][0]
    assert unit["generated_source_relationship"]["status"] == "known"
    assert unit["generated_source_relationship"]["declared_source_path"] == "source.ts"
    assert Path(unit["after"]["file"]).read_text().startswith("// Generated from source.ts")
    assert "diff --git a/source.ts" not in packet


def test_added_deleted_renamed_and_binary_units_remain_readable_after_workspace_changes(valid_boundary, tmp_path):
    import base64
    import subprocess
    import sys
    import build_review_packet
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    for path, content in {"deleted.py": b"obsolete = True\n", "old-name.py": b"value = 1\n",
                          "binary-resource.bin": b"\x00before\xff"}.items():
        (repo / path).write_bytes(content)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 文件类型基线")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "deleted.py").unlink()
    git(repo, "mv", "old-name.py", "new-name.py")
    (repo / "added.py").write_text("added = True\n")
    (repo / "binary-resource.bin").write_bytes(b"\x00after\xff")
    paths = ["deleted.py", "old-name.py", "new-name.py", "added.py", "binary-resource.bin"]
    tracker.register_claims(repo=repo, session_id="file-change-session", run_id=None, worktree=repo,
        branch=None, owned_paths=paths, apply_patch_paths=set(), exec_only_paths=set(), committed_paths=set(),
        committed_baseline=baseline, log_root_override=tmp_path / "tracker-state")
    allocated = tracker.allocate_review_scope(repo=repo, session_id="file-change-session", run_id="file-change-run",
        reviewer_id="independent-reviewers", committed_baseline=baseline, log_root_override=tmp_path / "tracker-state")
    scope = allocated["scope"]
    assert set(scope["paths"]) == set(paths)
    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "file-change-manifest.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": paths, "committed_round_baseline": baseline,
        "tracker": {"tracker_scope": scope}}))
    for path in paths:
        (repo / path).write_text("冻结后变化\n")
    packet, metadata = build_review_packet.build_packet(repo, None, manifest, 10000, [], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate, evidence_directory=tmp_path / "evidence")
    index = json.loads(Path(metadata["frozen_evidence_index"]).read_text())
    assert all(unit["evidence_status"] == "verified" for unit in index["units"])
    assert {unit["unit_id"] for unit in index["units"]} == set(scope["unit_ids"])
    assert packet.count("+added = True") == 1 and "冻结后变化" not in packet
    units_by_path = {unit["path"]: unit for unit in index["units"]}
    assert Path(units_by_path["deleted.py"]["before"]["file"]).read_text() == "obsolete = True\n"
    assert Path(units_by_path["new-name.py"]["after"]["file"]).read_text() == "value = 1\n"
    reader = Path(build_review_packet.__file__).with_name("read_frozen_review_evidence.py")
    command = [sys.executable, str(reader), "--index", metadata["frozen_evidence_index"],
        "--unit-id", units_by_path["binary-resource.bin"]["unit_id"], "--content", "after"]
    read = subprocess.run([*command, "--candidate-commit", candidate], capture_output=True, text=True)
    assert read.returncode == 0, read.stderr
    assert base64.b64decode(json.loads(read.stdout)["content_base64"]) == b"\x00after\xff"
    wrong_candidate = subprocess.run([*command, "--candidate-commit", baseline], capture_output=True, text=True)
    assert wrong_candidate.returncode != 0


def freeze_counter_change_tracker_scope(valid_boundary, tmp_path, *, committed):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    lines = [f"相邻源码 {index}" for index in range(50)]
    lines[1], lines[42] = "--counter;", "-- a/file-header-looking-content"
    (repo / "a.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 加减号正文基线")
    baseline = git(repo, "rev-parse", "HEAD")
    lines[1], lines[42] = "++counter;", "++ b/file-header-looking-content"
    (repo / "a.py").write_text("\n".join(lines) + "\n")
    dirty_unit_ids = {spec.unit_id for spec in tracker._specs_from_observation(
        tracker._classify_path(repo, "a.py"), "a.py")}
    if committed:
        git(repo, "add", "a.py")
        git(repo, "commit", "-qm", "test: 加减号正文提交")
    tracker.register_claims(repo=repo, session_id="counter-change-session", run_id=None,
        worktree=repo, branch=None, owned_paths=["a.py"], apply_patch_paths=set(), exec_only_paths=set(),
        committed_paths={"a.py"} if committed else set(), committed_baseline=baseline,
        log_root_override=tmp_path / "tracker-state")
    allocated = tracker.allocate_review_scope(repo=repo, session_id="counter-change-session",
        run_id="counter-change-run", reviewer_id="independent-reviewers", committed_baseline=baseline,
        log_root_override=tmp_path / "tracker-state")
    scope = allocated["scope"]
    assert set(scope["unit_ids"]) == dirty_unit_ids
    assert len(scope["hunks"]) == 2
    return repo, baseline, scope


def build_counter_change_frozen_packet(repo, baseline, scope, tmp_path):
    import build_review_packet

    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "counter-change-manifest.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": ["a.py"],
        "committed_round_baseline": baseline, "tracker": {"tracker_scope": scope}}))
    return build_review_packet.build_packet(repo, None, manifest, 10000, [], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate,
        evidence_directory=tmp_path / "frozen-counter-change-evidence")


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("saved_frozen_evidence", [False, True])
def test_frozen_counter_change_keeps_header_like_body_in_each_allocated_hunk(
        valid_boundary, tmp_path, committed, saved_frozen_evidence):
    repo, baseline, scope = freeze_counter_change_tracker_scope(valid_boundary, tmp_path, committed=committed)
    if saved_frozen_evidence:
        frozen_body = "".join(entry["frozen_evidence"]["hunk_patch"] for entry in scope["hunks"])
        assert "---counter;\n+++counter;" in frozen_body
        assert "--- a/file-header-looking-content\n+++ b/file-header-looking-content" in frozen_body
    else:
        for entry in scope["hunks"]:
            del entry["frozen_evidence"]
    packet, metadata = build_counter_change_frozen_packet(repo, baseline, scope, tmp_path)
    assert packet.count("---counter;") == packet.count("+++counter;") == 1
    assert packet.count("--- a/file-header-looking-content") == 1
    assert packet.count("+++ b/file-header-looking-content") == 1
    assert metadata["frozen_evidence_complete"] is True
    index = json.loads(Path(metadata["frozen_evidence_index"]).read_text())
    assert index["evidence_complete"] is True
    assert len(index["units"]) == 2
    assert all(unit["evidence_status"] == "verified" for unit in index["units"])


@pytest.mark.parametrize("corruption", ["header_only", "body_content", "header_coordinates", "header_line_counts"])
def test_frozen_hunk_with_self_consistent_identity_must_still_match_blob_ranges(
        valid_boundary, tmp_path, corruption):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    from core.session_scope_allocation.frozen_review_unit_evidence import tracked_hunk_content_identity

    repo, baseline, scope = freeze_counter_change_tracker_scope(valid_boundary, tmp_path, committed=False)
    entry = next(entry for entry in scope["hunks"] if "相邻源码 0" in entry["frozen_evidence"]["hunk_patch"])
    record = entry["frozen_evidence"]
    patch_lines = record["hunk_patch"].splitlines()
    if corruption == "header_only":
        patch_lines = patch_lines[:1]
    elif corruption == "body_content":
        patch_lines = [line.replace(" 相邻源码 0", " 伪造源码 0") for line in patch_lines]
    else:
        (old_start, old_count), (new_start, new_count), _ = tracker._hunk_header_parts(patch_lines[0])
        if corruption == "header_coordinates":
            old_start += 1
            new_start += 1
        else:
            old_count += 1
            new_count += 1
        patch_lines[0] = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
    record["hunk_patch"] = "\n".join(patch_lines) + "\n"
    record["hunk_patch_sha256"] = hashlib.sha256(record["hunk_patch"].encode()).hexdigest()
    original_unit_id = entry["unit_id"]
    changed_unit_id = tracked_hunk_content_identity("a.py", "modify", patch_lines[1:])
    entry["unit_id"] = record["unit_id"] = changed_unit_id
    scope["unit_ids"] = [changed_unit_id if unit_id == original_unit_id else unit_id for unit_id in scope["unit_ids"]]
    _, metadata = build_counter_change_frozen_packet(repo, baseline, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is False
    index = json.loads(Path(metadata["frozen_evidence_index"]).read_text())
    assert index["evidence_complete"] is False
    assert len(metadata["frozen_evidence_gaps"]) == 1
    assert "hunk" in metadata["frozen_evidence_gaps"][0]["evidence_gap"]


def test_single_line_counter_change_never_becomes_complete_header_only_evidence(valid_boundary, tmp_path):
    from test_review_packet_from_frozen_candidate_snapshot import observe_test_tracker_hunk_descriptors

    repo = valid_boundary["repo"]
    (repo / "a.py").write_text("--counter;\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 单行递减基线")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("++counter;\n")
    entries = observe_test_tracker_hunk_descriptors(repo, ["a.py"], save_frozen_evidence=True)
    scope = {"unit_ids": [entry["unit_id"] for entry in entries], "lease_id": "single-counter-lease",
        "scope_hash": "single-counter-scope", "paths": ["a.py"], "hunks": entries}
    packet, metadata = build_counter_change_frozen_packet(repo, baseline, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is True
    assert "---counter;\n+++counter;" in entries[0]["frozen_evidence"]["hunk_patch"]
    assert "---counter;\n+++counter;" in packet


@pytest.mark.parametrize(("before_content", "after_content"), [
    ("--counter;", "++counter;"),
    ("--counter;\n", "++counter;"),
    ("--counter;", "++counter;\n"),
    ("", "++counter;\n"),
    ("--counter;\n", ""),
    ("--counter;\r\n", "++counter;\r\n"),
])
def test_frozen_hunk_blob_validation_keeps_newline_and_zero_line_range_semantics(
        valid_boundary, tmp_path, before_content, after_content):
    from test_review_packet_from_frozen_candidate_snapshot import observe_test_tracker_hunk_descriptors

    repo = valid_boundary["repo"]
    (repo / "a.py").write_text(before_content)
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 换行与空区间基线")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text(after_content)
    entries = observe_test_tracker_hunk_descriptors(repo, ["a.py"], save_frozen_evidence=True)
    scope = {"unit_ids": [entry["unit_id"] for entry in entries], "lease_id": "counter-line-range-lease",
        "scope_hash": "counter-line-range-scope", "paths": ["a.py"], "hunks": entries}
    packet, metadata = build_counter_change_frozen_packet(repo, baseline, scope, tmp_path)
    assert len(entries) == 1
    assert metadata["frozen_evidence_complete"] is True
    if before_content:
        assert "---counter;" in packet
    if after_content:
        assert "+++counter;" in packet


def freeze_observed_path_scope(repo, path, observation):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    from core.session_scope_allocation.frozen_review_unit_evidence import freeze_observed_unit_evidence

    assert observation is not None
    specs = tracker._specs_from_observation(observation, path)
    baseline = git(repo, "rev-parse", "HEAD")
    records = freeze_observed_unit_evidence(repo, observation, specs, source="dirty",
        baseline_commit=baseline, target_commit=baseline)
    entries = [{"unit_id": spec.unit_id, "path": path, "hunk_header": spec.hunk_header,
        "frozen_evidence": record} for spec, record in zip(specs, records)]
    return {"unit_ids": [spec.unit_id for spec in specs], "lease_id": "normalized-content-lease",
        "scope_hash": "normalized-content-scope", "paths": [path], "hunks": entries}


def build_frozen_observed_path_packet(repo, path, scope, tmp_path):
    import build_review_packet

    candidate = freeze_packet_test_candidate(repo, tmp_path)
    manifest = tmp_path / "normalized-content-manifest.json"
    manifest.write_text(json.dumps({"repo": str(repo), "owned_paths": [path],
        "tracker": {"tracker_scope": scope}}))
    return build_review_packet.build_packet(repo, None, manifest, 10000, [], [],
        allow_missing_session_context=True, frozen_candidate_commit=candidate,
        evidence_directory=tmp_path / "normalized-content-evidence")


@pytest.mark.parametrize("path_state", ["modified", "untracked", "staged_add"])
def test_frozen_crlf_content_uses_git_normalized_blob_and_keeps_observed_raw_digest(
        valid_boundary, tmp_path, path_state):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    path = "normalized.txt"
    (repo / ".gitattributes").write_text("*.txt text eol=lf\n")
    if path_state == "modified":
        (repo / path).write_bytes(b"before\n")
        git(repo, "add", path)
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "test: Git 行尾规范化基线")
    baseline = git(repo, "rev-parse", "HEAD")
    raw_content = b"after\r\n"
    (repo / path).write_bytes(raw_content)
    if path_state == "staged_add":
        git(repo, "add", path)
    observation = tracker._classify_path(repo, path)
    scope = freeze_observed_path_scope(repo, path, observation)
    packet, metadata = build_frozen_observed_path_packet(repo, path, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is True
    record = scope["hunks"][0]["frozen_evidence"]
    assert record["observed_worktree_content_sha256"] == hashlib.sha256(raw_content).hexdigest()
    assert record["postimage_sha256"] == hashlib.sha256(b"after\n").hexdigest()
    assert Path(metadata["frozen_evidence_units"][0]["after"]["file"]).read_bytes() == b"after\n"
    assert (repo / path).read_bytes() == raw_content
    assert "+after" in packet
    git(repo, "add", path)
    git(repo, "commit", "-qm", "test: 提交规范化内容")
    committed = tracker._classify_committed_path(repo, path, baseline)
    assert {spec.unit_id for spec in tracker._specs_from_observation(committed, path)} == set(scope["unit_ids"])


@pytest.mark.parametrize("new_link", [False, True])
@pytest.mark.parametrize("dangling_target", [False, True])
def test_frozen_symlink_identity_uses_link_text_instead_of_target_content(
        valid_boundary, tmp_path, new_link, dangling_target):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    path = "source-link.txt"
    (repo / "before-target.txt").write_text("原链接目标内容\n")
    (repo / "after-target.txt").write_text("不同的新目标内容\n")
    if not new_link:
        (repo / path).symlink_to("before-target.txt")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 符号链接内容基线")
    if not new_link:
        (repo / path).unlink()
    target = "missing-target.txt" if dangling_target else "after-target.txt"
    (repo / path).symlink_to(target)
    observation = tracker._classify_path(repo, path)
    scope = freeze_observed_path_scope(repo, path, observation)
    packet, metadata = build_frozen_observed_path_packet(repo, path, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is True
    record = scope["hunks"][0]["frozen_evidence"]
    assert record["observed_worktree_content_sha256"] == hashlib.sha256(target.encode()).hexdigest()
    assert record["postimage_sha256"] == hashlib.sha256(target.encode()).hexdigest()
    assert Path(metadata["frozen_evidence_units"][0]["after"]["file"]).read_bytes() == target.encode()
    assert "+" + target in packet
    assert (repo / path).is_symlink()


@pytest.mark.parametrize("symbolic_link", [False, True])
@pytest.mark.parametrize("change_before_evidence", [False, True])
def test_observed_raw_content_or_link_drift_stays_rejected_with_git_normalization(
        valid_boundary, tmp_path, symbolic_link, change_before_evidence):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    path = "observed-content.txt"
    (repo / ".gitattributes").write_text("*.txt text eol=lf\n")
    (repo / "first-target.txt").write_text("相同目标内容\n")
    (repo / "second-target.txt").write_text("相同目标内容\n")
    if symbolic_link:
        (repo / path).symlink_to("first-target.txt")
    else:
        (repo / path).write_bytes(b"before\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 漂移门禁基线")
    if symbolic_link:
        (repo / path).unlink()
        (repo / path).symlink_to("second-target.txt")
    else:
        (repo / path).write_bytes(b"after\r\n")
    observation = tracker._classify_path(repo, path)
    if not change_before_evidence:
        scope = freeze_observed_path_scope(repo, path, observation)
    if symbolic_link:
        (repo / path).unlink()
        (repo / path).symlink_to("first-target.txt")
    else:
        # 仅改行尾也必须被原始工作区摘要识别；clean 后相同不代表没有漂移。
        (repo / path).write_bytes(b"after\n" if change_before_evidence else b"later-change\n")
    if change_before_evidence:
        scope = freeze_observed_path_scope(repo, path, observation)
        assert "枚举后" in scope["hunks"][0]["frozen_evidence"]["evidence_gap"]
    _, metadata = build_frozen_observed_path_packet(repo, path, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is False
    assert metadata["frozen_evidence_gaps"]


@pytest.mark.parametrize("symbolic_link", [False, True])
def test_git_clean_filter_normalizes_regular_content_but_never_symlink_text(
        valid_boundary, tmp_path, symbolic_link):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    repo = valid_boundary["repo"]
    path = "filtered-content.txt"
    (repo / ".gitattributes").write_text("*.txt filter=frozen-content-normalization\n")
    git(repo, "config", "filter.frozen-content-normalization.clean", "sed s/raw-/clean-/g")
    git(repo, "config", "filter.frozen-content-normalization.required", "true")
    if symbolic_link:
        (repo / "raw-before-target.txt").write_text("原目标\n")
        (repo / "raw-after-target.txt").write_text("新目标\n")
        (repo / path).symlink_to("raw-before-target.txt")
    else:
        (repo / path).write_bytes(b"raw-before\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 自定义 clean filter 基线")
    if symbolic_link:
        (repo / path).unlink()
        (repo / path).symlink_to("raw-after-target.txt")
        expected_content = b"raw-after-target.txt"
    else:
        (repo / path).write_bytes(b"raw-after\n")
        expected_content = b"clean-after\n"
    observation = tracker._classify_path(repo, path)
    scope = freeze_observed_path_scope(repo, path, observation)
    _, metadata = build_frozen_observed_path_packet(repo, path, scope, tmp_path)
    assert metadata["frozen_evidence_complete"] is True
    assert Path(metadata["frozen_evidence_units"][0]["after"]["file"]).read_bytes() == expected_content
