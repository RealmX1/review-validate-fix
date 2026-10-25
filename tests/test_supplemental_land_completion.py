"""历史 Land 补证的真 Git、独立验收、负例及跨进程恢复。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_reviewed_task_candidate_evidence import SCRIPT_DIRECTORY, canonical_bytes, create_completed_run, git, repository
from test_reviewed_candidate_completion_qualification import completed_machine_issue_run, source_and_index_snapshot
from test_rvf_land_single_step_prepare_and_commit import land_environment

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import supplemental_land_completion as supplemental
import rvf_land
from core.review_start_boundary.review_candidate_snapshot import SNAPSHOT_BINDING_FIELDS, SNAPSHOT_BINDING_PREFIX, SNAPSHOT_SCHEMA
from reviewed_candidate_completion_facts import completed_review_facts


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("GIT_", "RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name)
    for name, value in land_environment(tmp_path).items():
        monkeypatch.setenv(name, value)


def bind_snapshot(repo: Path, run_dir: Path, *, task_id="8bd3c", token="5d1452c3310aae10") -> dict:
    parent = git(repo, "rev-parse", "HEAD")
    git(repo, "add", "--all")
    tree = git(repo, "write-tree")
    snapshot = {"schema": SNAPSHOT_SCHEMA, "decision_token": token, "session_id": "historical-session",
                "worktree": str(repo), "candidate_fingerprint": "sha256:" + "a" * 64,
                "worktree_head_commit_sha": parent, "comparison_base_tip": parent, "comparison_base_ref": "main",
                "comparison_base_ref_tip": parent, "kanban_task_id": task_id, "local_review_base_ref": "main",
                "local_review_base_tip": parent, "packet_context_only_paths": []}
    binding = {key: snapshot.get(key) for key in SNAPSHOT_BINDING_FIELDS}
    checkpoint = git(repo, "commit-tree", tree, "-p", parent, "-m", SNAPSHOT_BINDING_PREFIX + json.dumps(binding, ensure_ascii=False))
    snapshot.update(checkpoint_commit=checkpoint, checkpoint_tree=tree)
    contract_path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(contract_path.read_text())
    contract["review_candidate_snapshot"] = snapshot
    contract_path.write_bytes(canonical_bytes(contract))
    return snapshot


def historical_case(tmp_path: Path, *, residual="same_path", machine_issue=False):
    if machine_issue:
        repo, run_dir = completed_machine_issue_run(tmp_path)
    else:
        repo = repository(tmp_path / "repo")
        (repo / "tracked.txt").write_text("candidate\n已审查实现\n")
        run_dir = create_completed_run(tmp_path, repo)
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text(handoff.read_text().replace("- review_status: COMPLETED\n", ""))
    task_id = "199e6" if residual == "199e6" else "8bd3c"
    token = "75ff459567c76572" if residual == "199e6" else "5d1452c3310aae10"
    snapshot = bind_snapshot(repo, run_dir, task_id=task_id, token=token)
    if residual == "same_path":
        (repo / "tracked.txt").write_text((repo / "tracked.txt").read_text() + "提交检查触发的额外测试差异\n")
    elif residual == "199e6":
        (repo / "docs").mkdir()
        (repo / "docs/selection.md").write_text("说明第一行\n说明第二行\n说明第三行\n说明第四行\n")
        (repo / "fallout.test.py").write_text("# 测试第一行\n# 测试第二行\n# 测试第三行\n# 测试第四行\n# 测试第五行\n")
    elif residual == "rename_mode":
        (repo / "tracked.txt").rename(repo / "renamed.txt")
        (repo / "renamed.txt").chmod(0o755)
    elif residual == "delete":
        (repo / "tracked.txt").unlink()
    git(repo, "add", "--all")
    git(repo, "commit", "--allow-empty", "-qm", "test: 历史实际提交")
    expected = {"task_id": task_id, "decision_token": token, "run_id": "rvf-candidate-evidence-test",
                "run_directory": str(run_dir), "handoff_sha256": hashlib.sha256(handoff.read_bytes()).hexdigest(),
                "target_commit": git(repo, "rev-parse", "HEAD")}
    return repo, run_dir, expected, snapshot


def cli_arguments(action: str, repo: Path, run_dir: Path, expected: dict) -> list[str]:
    arguments = ["supplemental-completion", action, "--repo", str(repo), "--run-dir", str(run_dir)]
    for field in ("task_id", "decision_token", "run_id", "handoff_sha256", "target_commit"):
        arguments += ["--" + field.replace("_", "-"), expected[field]]
    return arguments


def invoke(capsys, action, repo, run_dir, expected, *extra):
    code = rvf_land.main(cli_arguments(action, repo, run_dir, expected) + list(extra))
    output = capsys.readouterr()
    return code, json.loads(output.out)


def complete_double_review(subject, directory, workspace):
    supplemental.ensure_supplemental_review_inputs(subject, directory, workspace)
    root = directory / "artifacts/reviewers"
    for reviewer_id in ("independent-reviewer-a", "independent-reviewer-b"):
        path = root / reviewer_id / "review-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "独立检查固定差异及必要上下文，未发现问题。"}))
    (root / "reviewer-plan.json").write_text(json.dumps({"schema_version": 1, "status": "completed", "reviewers": [
        {"reviewer_id": name, "returncode": 0} for name in ("independent-reviewer-a", "independent-reviewer-b")]}))
    (directory / "artifacts/supplemental-review-execution.json").write_text(json.dumps({
        "subject_sha256": subject["supplemental_review_subject_sha256"], "started_at": "2026-10-25T12:00:00+00:00",
        "completed_at": "2026-10-25T12:01:00+00:00", "status": "completed",
        "target_commit": subject["target_commit"], "target_tree": subject["target_tree"]}))


@pytest.mark.parametrize("residual,paths", [("same_path", ["tracked.txt"]), ("199e6", ["docs/selection.md", "fallout.test.py"]),
                                           ("rename_mode", ["renamed.txt", "tracked.txt"]), ("delete", ["tracked.txt"])])
def test_inspect_reports_content_delta_and_never_mutates_history(tmp_path, capsys, residual, paths):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual=residual)
    source_before = source_and_index_snapshot(repo)
    artifacts_before = {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    code, payload = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code == 3, payload
    assert payload["subject"]["supplemental_review_paths"] == paths
    assert payload["subject"]["handoff_marker_facts"]["missing_markers"] == ["review_status"]
    assert source_and_index_snapshot(repo) == source_before
    assert {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()} == artifacts_before
    with pytest.raises(ValueError, match="均须完成"):
        completed_review_facts(run_dir, (run_dir / "artifacts/handoff.md").read_text())


def test_missing_marker_cannot_be_forged_by_historical_code_examples():
    from reviewed_candidate_completion_facts import handoff_completion_marker_facts
    facts = handoff_completion_marker_facts("- handoff_status: completed\n```text\nreview_status: COMPLETED\n```\n", allow_missing=True)
    assert facts["missing_markers"] == ["review_status"]
    with pytest.raises(ValueError):
        handoff_completion_marker_facts("handoff_status: COMPLETED\nreview_status: COMPLETED\nreview_status: FAILED\n", allow_missing=True)


@pytest.mark.parametrize("tampered", [False, True])
def test_frozen_scope_archive_requires_canonical_fields_and_restamp_provenance(tmp_path, capsys, tampered):
    from validation_result_cache import write_json_with_digest
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    path = run_dir / "artifacts/inputs/scope.contract.json"
    frozen = json.loads(path.read_text())
    if tampered:
        frozen["primary_files"] = ["foreign.txt"]
    digest = write_json_with_digest(path.with_name("scope.contract.reviewed.json"), frozen)
    current = json.loads(path.read_text())
    current["landing_restamp"] = [{"run_id": expected["run_id"], "reviewed_contract_sha256": digest, "reviewed_scope_hash": frozen["scope_hash"]}]
    current["canonical_scope"]["candidate_task_tip"] = expected["target_commit"]
    current["candidate_task_tip"] = expected["target_commit"]
    current["scope_hash"] = hashlib.sha256(canonical_bytes(current["canonical_scope"])).hexdigest()
    path.write_bytes(canonical_bytes(current))
    code, payload = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code == (4 if tampered else 3), payload


def test_missing_original_git_object_waits_for_bound_machine_recovery(tmp_path, capsys):
    repo, run_dir, expected, snapshot = historical_case(tmp_path)
    oid = snapshot["checkpoint_commit"]
    object_path = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")) / oid[:2] / oid[2:]
    object_path.unlink()
    code, payload = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code == 3, payload
    assert oid in payload["reason"]
    assert payload["input_fingerprint"]
    assert "重新对账" in payload["recovery_condition"]


@pytest.mark.parametrize("residual", ["same_path", "199e6", "none"])
def test_full_supplemental_proof_is_independently_accepted_and_retry_reuses_it(tmp_path, capsys, monkeypatch, residual):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual=residual)
    original = source_and_index_snapshot(repo)
    original_contract = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    calls = []
    def review(subject, directory, workspace):
        calls.append(subject["supplemental_review_subject_sha256"])
        complete_double_review(subject, directory, workspace)
    monkeypatch.setattr(supplemental, "recover_double_review", review)
    code, first = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, first
    assert first["historical_completion"]["target_commit"] == expected["target_commit"]
    assert first["current_execution_eligibility"]["stop_restrictions_unchanged"] is True
    code, verified = invoke(capsys, "verify", repo, run_dir, expected, "--evidence", first["evidence_path"], "--evidence-sha256", first["evidence_sha256"])
    assert code == 0, verified
    code, repeated = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0 and repeated["reused"] is True, repeated
    assert len(calls) == (0 if residual == "none" else 1)
    assert original == source_and_index_snapshot(repo)
    assert (run_dir / "artifacts/inputs/scope.contract.json").read_bytes() == original_contract
    assert len(list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))) == 1


def independent_base_validation_case(tmp_path, *, context_kind="base_worktree", requires_base=True, foreign_repository=False,
                                     execution_mismatch=None):
    """真实执行原 argv，只有原始历史义务夹具声明 base 上下文。"""
    from validation_result_cache import write_json_with_digest
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual="none")
    commands_path = run_dir / "artifacts/handoff-validation-commands.json"
    commands = json.loads(commands_path.read_text())
    command = commands["validation_commands"][0]
    command.update(validation_context_kind="base_worktree", requires_base_worktree_context=True)
    command["environment_keys"] = ["RVF_TEST_BASE_VALIDATION_FLAVOR"]
    write_json_with_digest(commands_path, commands)
    base_worktree = tmp_path / "independent-base-validation-worktree"
    if foreign_repository:
        git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(repo), str(base_worktree))
    else:
        git(repo, "worktree", "add", "--quiet", "--detach", str(base_worktree), expected["target_commit"])
    validation_run = tmp_path / "independent-base-validation-run"
    invocation = [sys.executable, str(SCRIPT_DIRECTORY / "validation_result_cache.py"), "run", "--repo", str(base_worktree),
                  "--name", command["name"], "--working-directory", command["working_directory_relative_to_worktree"],
                  "--validation-context-kind", context_kind, "--rvf-run-dir", str(validation_run)]
    for key in command["environment_keys"]:
        invocation += ["--environment-key", key]
    if requires_base:
        invocation.append("--requires-base-worktree-context")
    environment = dict(os.environ)
    executed_argv = command["argv"]
    if execution_mismatch == "environment":
        environment["RVF_TEST_BASE_VALIDATION_FLAVOR"] = "不同执行环境"
    elif execution_mismatch == "command":
        executed_argv = [sys.executable, "-c", "print('另一条通过命令')"]
    elif execution_mismatch == "content":
        (base_worktree / "tracked.txt").write_text("不同的验证目标内容\n")
    completed = subprocess.run([*invocation, "--", *executed_argv], env=environment, capture_output=True, text=True, check=True)
    native_record = json.loads(completed.stdout)
    assert native_record["status"] == "passed" and native_record["cache_status"] == "executed"
    return repo, run_dir, expected, base_worktree, validation_run, native_record


@pytest.mark.parametrize("reclaim_before_record", [False, True])
def test_independent_base_validation_records_are_accepted_and_verified_offline(tmp_path, capsys, reclaim_before_record):
    import shutil
    repo, run_dir, expected, base_worktree, validation_run, native = independent_base_validation_case(tmp_path)
    native_path = Path(native["cache_record_path"])
    native_before = native_path.read_bytes()
    if reclaim_before_record:
        # Git 保留的 prunable 注册仍可与执行时的原生路径摘要核对。
        shutil.rmtree(base_worktree)
    for action in ("inspect", "record"):
        code, proof = invoke(capsys, action, repo, run_dir, expected, "--validation-run-dir", str(validation_run))
        assert code == 0, proof
    assert native_path.read_bytes() == native_before
    evidence = json.loads(Path(proof["evidence_path"]).read_text())
    accepted = evidence["validation_snapshots"][0]
    assert accepted["record"] == json.loads(native_before)
    assert accepted["reference"]["cache_record_path"] == str(native_path)
    assert base_worktree != repo
    if base_worktree.exists():
        git(repo, "worktree", "remove", "--force", str(base_worktree))
    else:
        git(repo, "worktree", "prune", "--expire", "now")
    shutil.rmtree(validation_run)
    shutil.rmtree(tmp_path / "user-data")
    # 离线验收只重放已固定的原生材料，不追读旧 base 路径、注册或 cache。
    code, verified = invoke(capsys, "verify", repo, run_dir, expected,
                            "--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"])
    assert code == 0, verified


@pytest.mark.parametrize("context_kind,requires_base,foreign_repository", [
    ("task_worktree", False, False), ("task_worktree", True, False), ("base_worktree", True, True),
])
def test_supplied_base_validation_rejects_wrong_context_or_repository(tmp_path, capsys, context_kind, requires_base, foreign_repository):
    repo, run_dir, expected, _base, validation_run, _native = independent_base_validation_case(
        tmp_path, context_kind=context_kind, requires_base=requires_base, foreign_repository=foreign_repository)
    code, refused = invoke(capsys, "record", repo, run_dir, expected, "--validation-run-dir", str(validation_run))
    assert code == 3, refused
    assert not list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))


@pytest.mark.parametrize("execution_mismatch", ["environment", "command", "content"])
def test_supplied_base_validation_still_requires_matching_execution(tmp_path, capsys, execution_mismatch):
    repo, run_dir, expected, _base, validation_run, _native = independent_base_validation_case(
        tmp_path, execution_mismatch=execution_mismatch)
    code, refused = invoke(capsys, "record", repo, run_dir, expected, "--validation-run-dir", str(validation_run))
    assert code == 3, refused
    assert not list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))


def test_base_registration_gap_recovers_from_same_native_record_without_reexecution(tmp_path, capsys):
    repo, run_dir, expected, base_worktree, validation_run, native = independent_base_validation_case(tmp_path)
    native_path = Path(native["cache_record_path"])
    native_before = native_path.read_bytes()
    git(repo, "worktree", "remove", "--force", str(base_worktree))
    code, waiting = invoke(capsys, "inspect", repo, run_dir, expected, "--validation-run-dir", str(validation_run))
    assert code == 3 and "恢复绑定注册材料" in waiting["reason"], waiting
    git(repo, "worktree", "add", "--quiet", "--detach", str(base_worktree), expected["target_commit"])
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--validation-run-dir", str(validation_run))
    assert code == 0, proof
    assert native_path.read_bytes() == native_before


@pytest.mark.parametrize("mutation", ["registration_path", "registration_bytes", "repository", "context_digest",
                                      "cache_namespace", "context_kind", "requires_base", "environment"])
def test_base_context_proof_rejects_tampering_with_recomputed_outer_digest(tmp_path, capsys, mutation):
    from validation_result_cache import validation_record_reference
    repo, run_dir, expected, _base, validation_run, _native = independent_base_validation_case(tmp_path)
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--validation-run-dir", str(validation_run))
    assert code == 0, proof
    path = Path(proof["evidence_path"])
    payload = json.loads(path.read_text())
    snapshot = payload["validation_snapshots"][0]
    context, record = snapshot["base_worktree_context"], snapshot["record"]
    if mutation == "registration_path":
        raw = base64.b64decode(context["git_worktree_registration_base64"])
        context["git_worktree_registration_base64"] = base64.b64encode(
            b"worktree " + os.fsencode(str(repo)) + b"\0" + raw.split(b"\0", 1)[1]).decode()
    elif mutation == "registration_bytes":
        context["git_worktree_registration_base64"] = base64.b64encode("非 Git 注册原件".encode()).decode()
    elif mutation == "repository":
        context["repository_git_common_directory"] = str(tmp_path / "another-repository/.git")
    else:
        if mutation == "context_digest":
            record["validation_context_worktree_realpath_sha256"] = "0" * 64
        elif mutation == "cache_namespace":
            record_path = Path(record["cache_record_path"])
            record["cache_record_path"] = str(record_path.parent.parent.parent.parent / ("f" * 64) / "entries" / record["cache_key"] / "record.json")
        elif mutation == "context_kind":
            record["validation_context_kind"] = "task_worktree"
        elif mutation == "requires_base":
            record["requires_base_worktree_context"] = False
        elif mutation == "environment":
            record["environment_fingerprint_sha256"] = "0" * 64
        # 内层记录引用也重算，仍须由 cache identity、注册及同仓门禁拒绝。
        snapshot["record_sha256"] = supplemental.payload_digest(record)
        snapshot["reference"] = validation_record_reference(record, snapshot["record_sha256"])
    encoded = canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    mutated_path = path.parent / (digest + ".json")
    supplemental.durable_publish(mutated_path, encoded)
    code, refused = invoke(capsys, "verify", repo, run_dir, expected, "--evidence", str(mutated_path), "--evidence-sha256", digest)
    assert code == 4, refused


@pytest.mark.parametrize("mutation", ["missing_leg", "request", "explicit_incomplete", "unapplied", "newer_failed", "stale_dispositions", "handoff_hash", "task", "token"])
def test_historical_obligations_and_identity_cannot_be_bypassed(tmp_path, capsys, mutation):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, machine_issue=True)
    if mutation in {"missing_leg", "request", "stale_dispositions"}:
        path = run_dir / "artifacts/reviewers/reviewer-a/review-result.json"
        payload = json.loads(path.read_text())
        if mutation == "missing_leg":
            path.unlink()
        else:
            if mutation == "request":
                payload.update(kind="request", requests=[{"message": "需要机器上下文"}])
            else:
                payload["issues"][0]["message"] = "事后改变的审查"
            path.write_text(json.dumps(payload))
    elif mutation == "explicit_incomplete":
        path = run_dir / "artifacts/handoff.md"
        path.write_text(path.read_text() + "- review_status: FAILED\n")
        expected["handoff_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif mutation in {"unapplied", "newer_failed"}:
        if mutation == "unapplied":
            path = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1/attempt.json"
            payload = json.loads(path.read_text());payload["status"] = "fixed";path.write_text(json.dumps(payload))
        else:
            from test_reviewed_task_candidate_evidence import record_terminal_fix_attempt
            record_terminal_fix_attempt(run_dir, "RVF-001", status="failed", attempt_number=2)
    else:
        key = {"handoff_hash": "handoff_sha256", "task": "task_id", "token": "decision_token"}[mutation]
        expected[key] = {"handoff_hash": "0" * 64, "task": "another-task", "token": "0" * 16}[mutation]
    code, result = invoke(capsys, "record", repo, run_dir, expected)
    assert code == 4, result
    assert not list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))


def test_replay_checks_complete_preimages_and_includes_unreconstructable_whole_paths(tmp_path, capsys):
    repo, run_dir, expected, snapshot = historical_case(tmp_path, residual="none", machine_issue=True)
    attempt_root = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("修复后的正文\n")
    patch = subprocess.run(["git", "diff", "--binary", "--full-index"], cwd=repo, capture_output=True, check=True).stdout
    (attempt_root / "fix.patch").write_bytes(patch)
    for name in ("attempt.json", "result.json"):
        path = attempt_root / name;payload = json.loads(path.read_text())
        payload.update(baseline_commit=baseline, changed_paths=[{"path": "tracked.txt", "op": "M"}])
        path.write_text(json.dumps(payload))
    git(repo, "add", "tracked.txt");git(repo, "commit", "-qm", "test: 应用修复")
    expected["target_commit"] = git(repo, "rev-parse", "HEAD")
    code, payload = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code in {0, 3}, payload
    assert payload["subject"]["replayed_attempt_ids"] == [attempt_root.name]
    assert payload["subject"]["supplemental_review_paths"] == []
    # baseline 中另一处字节漂移：即便补丁 hunk 仍可偏移应用，也不复用此路径。
    path = attempt_root / "attempt.json";attempt = json.loads(path.read_text());attempt["baseline_commit"] = "f" * 40;path.write_text(json.dumps(attempt))
    code, payload = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code == 3, payload
    assert payload["subject"]["whole_path_review_required"] == ["tracked.txt"]
    assert payload["subject"]["whole_path_context"]


@pytest.mark.parametrize("ambiguous_preimages", [False, True])
@pytest.mark.parametrize("dependent_patch_count", [1, 2])
def test_patch_dependency_closure_preserves_reverted_whole_paths(tmp_path, monkeypatch, ambiguous_preimages, dependent_patch_count):
    repo = repository(tmp_path / "synthetic-patch-repository")
    shared_candidate = "".join(f"候选第 {number} 行\n" for number in range(40))
    candidate_contents = {"shared.txt": shared_candidate, "separate.txt": "独立候选正文\n",
                          "chained.txt": "后续依赖候选正文\n", "independent.txt": "无依赖候选正文\n"}
    for path, contents in candidate_contents.items():
        (repo / path).write_text(contents)
    git(repo, "add", "--all");git(repo, "commit", "-qm", "test: 合成补丁候选")
    checkpoint_commit = git(repo, "rev-parse", "HEAD")
    snapshot = {"checkpoint_commit": checkpoint_commit, "checkpoint_tree": git(repo, "rev-parse", "HEAD^{tree}")}
    run_dir = tmp_path / "synthetic-patch-review-run"
    facts = {"applied_attempts": []}
    patch_paths = {}

    def record_applied_patch(attempt_id, baseline_commit, updated_contents):
        git(repo, "reset", "--hard", baseline_commit)
        for path, contents in updated_contents.items():
            (repo / path).write_text(contents)
        patch = subprocess.run(["git", "diff", "--binary", "--full-index"], cwd=repo, capture_output=True, check=True).stdout
        patch_path = run_dir / "artifacts/fix-attempts" / attempt_id / "fix.patch"
        patch_path.parent.mkdir(parents=True);patch_path.write_bytes(patch)
        patch_paths[attempt_id] = patch_path
        # 每份补丁都实际检查并应用；不以合成 metadata 冒充合法的补丁链。
        git(repo, "reset", "--hard", baseline_commit)
        git(repo, "apply", "--check", str(patch_path));git(repo, "apply", str(patch_path))
        git(repo, "add", "--all");git(repo, "commit", "-qm", "test: 应用合成补丁 " + attempt_id)
        facts["applied_attempts"].append({"attempt": {"attempt_id": attempt_id, "baseline_commit": baseline_commit},
                                         "result": {"changed_paths": list(updated_contents)}})
        return git(repo, "rev-parse", "HEAD")

    shared_after_a = shared_candidate.replace("候选第 1 行", "补丁 A 第一处修改")
    combined_commit = record_applied_patch("patch-a", checkpoint_commit, {"shared.txt": shared_after_a})
    if ambiguous_preimages:
        shared_after_b = shared_candidate.replace("候选第 38 行", "补丁 B 另一处修改")
        record_applied_patch("patch-b", checkpoint_commit, {"shared.txt": shared_after_b})
        git(repo, "reset", "--hard", combined_commit)
        # A/B 的完整前像相同、hunk 不同；B 在 A 之后仍可真实应用。
        git(repo, "apply", "--check", str(patch_paths["patch-b"]));git(repo, "apply", str(patch_paths["patch-b"]))
        git(repo, "add", "--all");git(repo, "commit", "-qm", "test: 合并合法的分离 hunk")
        combined_commit = git(repo, "rev-parse", "HEAD")
    shared_after_c = (repo / "shared.txt").read_text().replace("候选第 20 行", "补丁 C 依赖修改")
    after_c = record_applied_patch("dependent-patch-c", combined_commit,
                                   {"shared.txt": shared_after_c, "separate.txt": "补丁 C 的跨路径修复\n"})
    after_dependencies = after_c
    reverted_paths = ["separate.txt"]
    if dependent_patch_count == 2:
        after_dependencies = record_applied_patch("dependent-patch-d", after_c,
                                                  {"separate.txt": "补丁 D 的后续修复\n", "chained.txt": "补丁 D 的跨路径修复\n"})
        reverted_paths.append("chained.txt")
    record_applied_patch("independent-patch", checkpoint_commit, {"independent.txt": "独立补丁修复\n"})
    git(repo, "reset", "--hard", after_dependencies)
    git(repo, "apply", "--check", str(patch_paths["independent-patch"]));git(repo, "apply", str(patch_paths["independent-patch"]))
    if ambiguous_preimages:
        for path in reverted_paths:
            (repo / path).write_text(candidate_contents[path])
    git(repo, "add", "--all");git(repo, "commit", "-qm", "test: 固定实际目标")
    target_commit = git(repo, "rev-parse", "HEAD")
    # 若有 D，则 D 先于 C 进入扫描，覆盖逆序的多跳整路径义务。
    facts["applied_attempts"].reverse()
    (run_dir / "artifacts/handoff.md").write_text("- handoff_status: COMPLETED\n- review_status: COMPLETED\n")
    monkeypatch.setattr(supplemental, "validate_original_binding", lambda *args: ({"scope_hash": "a" * 64}, snapshot, facts))
    subject = supplemental.build_supplemental_review_subject(repo, run_dir, {"target_commit": target_commit}, {})
    if ambiguous_preimages:
        required_paths = sorted(["shared.txt", *reverted_paths])
        assert subject["whole_path_review_required"] == required_paths
        assert subject["supplemental_review_paths"] == required_paths
        assert subject["replayed_attempt_ids"] == ["independent-patch"]
        for path in reverted_paths:
            # 回退路径没有 tree delta，仍须出现在补审范围与完整正文上下文。
            entry = next(item for item in subject["path_entries"] if item["path"] == path)
            assert entry["before"] == entry["after"]
            contexts = [item for item in subject["whole_path_context"] if item["path"] == path]
            assert len(contexts) == 2
            assert all(base64.b64decode(item["content_base64"]).decode() == candidate_contents[path] for item in contexts)
    else:
        assert subject["whole_path_review_required"] == []
        assert subject["supplemental_review_paths"] == []
        assert set(subject["replayed_attempt_ids"]) == set(patch_paths)
        assert subject["reconstructed_tree"] == subject["target_tree"]


def test_proof_survives_original_worktree_sources_cache_and_installation_path_drift(tmp_path, capsys, monkeypatch):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    monkeypatch.setattr(supplemental, "recover_double_review", complete_double_review)
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, proof
    for relative in ("artifacts/reviewers", "artifacts/fix-attempts", "artifacts/inputs"):
        import shutil
        shutil.rmtree(run_dir / relative, ignore_errors=True)
    import shutil
    shutil.rmtree(run_dir / supplemental.ARTIFACT_DIRECTORY / "recovery-operations")
    shutil.rmtree(tmp_path / "user-data")
    moved_repo = tmp_path / "relocated-repository"
    git(repo, "worktree", "add", "--quiet", "--detach", str(moved_repo), expected["target_commit"])
    code, result = invoke(capsys, "verify", moved_repo, run_dir, expected, "--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"])
    assert code == 0, result


def test_bounded_recovery_and_machine_evidence_change(tmp_path, capsys, monkeypatch):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    calls = []
    def unavailable(*arguments):
        calls.append(1)
        raise supplemental.MachineEvidencePending("隔离测试：harness 暂时不可用")
    monkeypatch.setattr(supplemental, "recover_double_review", unavailable)
    for _ in range(5):
        code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
        assert code == 3, result
    assert len(calls) == 3
    assert "三次" in result["reason"]
    monkeypatch.setattr(supplemental, "recover_double_review", complete_double_review)
    # 原证据改变触发新输入指纹；不清除旧失败记录。
    path = run_dir / "artifacts/handoff.md";path.write_text(path.read_text() + "\n原始材料补充上下文。\n")
    expected["handoff_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, result
    assert len(list((run_dir / supplemental.ARTIFACT_DIRECTORY / "recovery-operations").glob("*/machine-evidence-recovery-state.json"))) == 2


@pytest.mark.parametrize("mutation", ["binding", "delta", "review", "validation", "bundle", "evidence_digest"])
def test_consumer_rejects_tampering_even_with_new_sidecar(tmp_path, capsys, monkeypatch, mutation):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    monkeypatch.setattr(supplemental, "recover_double_review", complete_double_review)
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, proof
    path = Path(proof["evidence_path"])
    payload = json.loads(path.read_text())
    if mutation == "binding":
        expected = {**expected, "task_id": "foreign-task"}
    elif mutation == "delta":
        payload["subject"]["supplemental_review_paths"] = []
    elif mutation == "review":
        sources = payload["supplemental_review_snapshots"]
        del sources["artifacts/reviewers/independent-reviewer-b/review-result.json"]
    elif mutation == "validation":
        record = payload["validation_snapshots"][0]["record"]
        record["status"] = "failed";record["exit_code"] = 1
    elif mutation == "bundle":
        (path.parent / (payload["git_bundle_sha256"] + ".bundle")).write_bytes("损坏的 Git 对象包".encode())
    digest = proof["evidence_sha256"]
    if mutation in {"delta", "review", "validation"}:
        encoded = canonical_bytes(payload);digest = hashlib.sha256(encoded).hexdigest()
        path = path.parent / (digest + ".json")
        supplemental.durable_publish(path, encoded)
    elif mutation == "evidence_digest":
        digest = "0" * 64
    code, refused = invoke(capsys, "verify", repo, run_dir, expected, "--evidence", str(path), "--evidence-sha256", digest)
    assert code == 4, refused


def test_red_validation_never_produces_completion_proof(tmp_path, capsys):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual="none")
    from validation_result_cache import write_json_with_digest
    path = run_dir / "artifacts/handoff-validation-commands.json"
    payload = json.loads(path.read_text());payload["validation_commands"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(17)"]
    write_json_with_digest(path, payload)
    code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 4, result
    assert "验证失败" in result["reason"]
    assert not list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))
    assert list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("recovery-operations/*/isolated-validation-command-outputs/*.json"))


def test_pending_backlog_fact_uses_retained_sources_after_original_path_disappears(tmp_path, capsys):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual="none", machine_issue=True)
    attempt_root = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    pending_path = tmp_path / "durable-backlog-pending-intent.json"
    pending_path.write_text(json.dumps({"run_dir": str(run_dir), "finding_fingerprints": ["synthetic-finding"]}))
    for name in ("attempt.json", "result.json"):
        path = attempt_root / name;payload = json.loads(path.read_text())
        payload["status"] = "validated_and_submitted_to_kanban_backlog"
        if name == "result.json":
            payload["kanban_backlog_submission"] = {"issue_id": "RVF-001", "attempt_id": attempt_root.name, "groups": [{
                "outcome": "pending_kanban_runtime_unavailable", "pending_prompt_path": str(pending_path),
                "findings": [{"finding_fingerprint": "synthetic-finding"}]}]}
        path.write_text(json.dumps(payload))
    path = run_dir / "artifacts/handoff.md"
    path.write_text("- handoff_status: COMPLETED\n共 1 条，0 真实修复 / 0 误报 / 1 backlog / 0 升级\n")
    expected["handoff_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, proof
    pending_path.unlink()
    code, verified = invoke(capsys, "verify", repo, run_dir, expected, "--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"])
    assert code == 0, verified


def test_isolated_dependency_recovery_reuses_success_and_retains_network_failure(tmp_path, monkeypatch):
    repo = repository(tmp_path / "repo")
    (repo / "package.json").write_text('{"name":"synthetic-dependency-fixture","version":"1.0.0"}')
    (repo / "package-lock.json").write_text('{"lockfileVersion":3}')
    git(repo, "add", "--all");git(repo, "commit", "-qm", "test: 隔离依赖夹具")
    executable_directory = tmp_path / "synthetic-executables"
    executable_directory.mkdir()
    counter = tmp_path / "dependency-execution-counter.txt"
    npm = executable_directory / "npm"
    npm.write_text("#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n"
                   "if '--version' in sys.argv:\n print('10.0.0');raise SystemExit(0)\n"
                   f"with open({str(counter)!r},'a') as output: output.write('execution\\n')\n"
                   "if os.environ.get('SYNTHETIC_NETWORK_UNAVAILABLE'): raise SystemExit(23)\n"
                   "Path('node_modules').mkdir(exist_ok=True)\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable_directory) + os.pathsep + os.environ["PATH"])
    commands = [{"argv": ["npm", "test"]}]
    operation = tmp_path / "persistent-recovery-operation"
    supplemental.prepare_isolated_node_dependencies(repo, commands, operation)
    supplemental.prepare_isolated_node_dependencies(repo, commands, operation)
    assert len(counter.read_text().splitlines()) == 1
    (repo / "node_modules").rmdir()
    monkeypatch.setenv("SYNTHETIC_NETWORK_UNAVAILABLE", "1")
    with pytest.raises(supplemental.MachineEvidencePending, match="安装未完成"):
        supplemental.prepare_isolated_node_dependencies(repo, commands, operation)
    receipts = [json.loads(path.read_text()) for path in (operation / "isolated-node-dependency-preparation/execution-results").glob("*.json")]
    assert {receipt["status"] for receipt in receipts} == {"passed", "awaiting_machine_evidence"}


@pytest.mark.parametrize("scenario", ["normal", "tampered_qualification", "restart_after_both_legs"])
def test_real_dispatch_executor_binds_both_legs_and_keeps_cli_json_clean(tmp_path, capsys, monkeypatch, scenario):
    import dispatch_reviewers
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    monkeypatch.setattr(dispatch_reviewers, "load_registry", lambda path: {})
    monkeypatch.setattr(dispatch_reviewers, "probe_available", lambda registry: ["synthetic"])
    plan = {"schema_version": 1, "status": "planned", "routing_rule": "R1", "warnings": [],
            "reviewers": [{"reviewer_id": name, "harness_id": "synthetic"} for name in ("synthetic-a", "synthetic-b")]}
    monkeypatch.setattr(dispatch_reviewers, "route", lambda *args, **kwargs: plan)
    if scenario == "tampered_qualification":
        original_prepare = supplemental.prepare_supplemental_review_dispatch_qualification
        def prepare_tampered(subject, directory, sources, backlog_sources):
            original_prepare(subject, directory, sources, backlog_sources)
            path = directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json"
            payload = json.loads(path.read_text());payload["subject"]["handoff_sha256"] = "0" * 64
            path.write_bytes(canonical_bytes(payload))
        monkeypatch.setattr(supplemental, "prepare_supplemental_review_dispatch_qualification", prepare_tampered)
    packets = []
    def reviewer_command(reviewer, **kwargs):
        packets.append(kwargs["review_packet"])
        result_path = Path(kwargs["run_dir"]) / "artifacts/reviewers" / reviewer["reviewer_id"] / "review-result.json"
        result = {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "独立检查固定差异。"}
        return [sys.executable, "-c", "from pathlib import Path;"
                f"p=Path({str(result_path)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_text({json.dumps(result,ensure_ascii=False)!r});"
                "print('synthetic reviewer stdout');import sys;print('synthetic reviewer stderr',file=sys.stderr)"]
    monkeypatch.setattr(dispatch_reviewers, "_build_reviewer_command", reviewer_command)
    if scenario == "restart_after_both_legs":
        original_execute = dispatch_reviewers.execute_plan
        interrupted = []
        def interrupt_after_native_leg_receipts(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            if not interrupted:
                interrupted.append(1)
                raise OSError("隔离夹具：双腿已完成，尚未登记整轮 plan")
            return result
        monkeypatch.setattr(dispatch_reviewers, "execute_plan", interrupt_after_native_leg_receipts)
    code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    if scenario == "tampered_qualification":
        assert code == 4, result.get("reason")
        assert packets == []
        return
    if scenario == "restart_after_both_legs":
        assert code == 4, result.get("reason")
        code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, result.get("reason")
    assert len(packets) == 2 and packets[0] == packets[1]
    logs = list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("recovery-operations/*/supplemental-review-run/artifacts/reviewer-command-outputs/*.log"))
    assert len(logs) == 4
    assert all("synthetic reviewer" in path.read_text() for path in logs)


@pytest.mark.parametrize("failure_point", ["before_publish", "after_publish", "sidecar_missing"])
def test_restart_reconciles_evidence_and_does_not_repeat_completed_work(tmp_path, capsys, monkeypatch, failure_point):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    calls = []
    def review(*arguments):
        calls.append(1);complete_double_review(*arguments)
    monkeypatch.setattr(supplemental, "recover_double_review", review)
    original_publish = supplemental.durable_publish
    original_durable_json = supplemental.durable_json
    interrupted = []
    def publish(path, encoded):
        if path.suffix == ".json" and not interrupted:
            interrupted.append(1)
            if failure_point == "before_publish":
                raise OSError("隔离夹具：发布前崩溃")
            if failure_point == "sidecar_missing":
                path.write_bytes(encoded)
                raise OSError("隔离夹具：sidecar 前崩溃")
        return original_publish(path, encoded)
    def result_persistence(path, payload):
        if path.name == "completed-evidence-reference.json" and failure_point == "after_publish":
            raise OSError("隔离夹具：发布后登记回执前崩溃")
        return original_durable_json(path, payload)
    monkeypatch.setattr(supplemental, "durable_publish", publish)
    monkeypatch.setattr(supplemental, "durable_json", result_persistence)
    code, result = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 4, result
    monkeypatch.setattr(supplemental, "durable_publish", original_publish)
    monkeypatch.setattr(supplemental, "durable_json", original_durable_json)
    monkeypatch.setattr(supplemental, "current_boot_session_id", lambda: "another-boot-session")
    code, recovered = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, recovered
    assert len(calls) == 1
    assert len(list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))) == 1
    code, inspected = invoke(capsys, "inspect", repo, run_dir, expected)
    assert code == 0 and inspected["missing_obligations"] == [], inspected


def test_concurrent_recorders_publish_one_proof_without_business_mutations(tmp_path):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual="none")
    before = source_and_index_snapshot(repo)
    command = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_land.py"), *cli_arguments("record", repo, run_dir, expected), "--recover-machine-evidence"]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=45)
        assert process.returncode == 0, stdout + stderr
        results.append(json.loads(stdout))
    assert results[0]["evidence_sha256"] == results[1]["evidence_sha256"]
    assert sorted(result["reused"] for result in results) == [False, True]
    assert source_and_index_snapshot(repo) == before


def test_public_shortcut_passes_exact_binding_without_selecting_latest(tmp_path):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    completed = subprocess.run([sys.executable, str(SCRIPT_DIRECTORY / "rvf_command_line_shortcut_entrypoint.py"),
                                "land", *cli_arguments("inspect", repo, run_dir, expected)],
                               capture_output=True, text=True, cwd=repo, check=False)
    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["binding"] == expected
    arguments = cli_arguments("inspect", repo, run_dir, expected)
    index = arguments.index("--run-id")
    del arguments[index:index + 2]
    refused = subprocess.run([sys.executable, str(SCRIPT_DIRECTORY / "rvf_command_line_shortcut_entrypoint.py"), "land", *arguments],
                             capture_output=True, text=True, cwd=repo, check=False)
    assert refused.returncode == 2
    assert "--run-id" in refused.stderr


def test_backup_ownership_prohibits_record_but_does_not_revoke_historical_fact(tmp_path, capsys):
    repo, run_dir, expected, _snapshot = historical_case(tmp_path, residual="none")
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, proof
    from independent_backup_route import control_directory
    marker = control_directory(repo) / "active-owner"
    marker.write_text("隔离夹具：已接管")
    code, prohibited = invoke(capsys, "record", repo, run_dir, expected)
    assert code == 5, prohibited
    code, verified = invoke(capsys, "verify", repo, run_dir, expected, "--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"])
    assert code == 0, verified
    assert verified["historical_completion"]["status"] == "verified"
    assert verified["current_execution_eligibility"]["status"] == "execution_prohibited"


def export_consumer_contract_fixtures(output_directory: Path) -> None:
    """导出真实可执行、明确标为合成的跨仓正负例；不触碰真实历史任务。"""
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import patch
    output_directory.mkdir(parents=True, exist_ok=False)
    exported = []
    for sample, residual in (("8bd3c-equivalent", "same_path"), ("199e6-equivalent", "199e6")):
        directory = output_directory / sample
        directory.mkdir()
        with patch.dict(os.environ, land_environment(directory), clear=True):
            repo, run_dir, expected, _snapshot = historical_case(directory, residual=residual)
            captured = StringIO()
            with patch.object(supplemental, "recover_double_review", complete_double_review), redirect_stdout(captured):
                exit_code = rvf_land.main(cli_arguments("record", repo, run_dir, expected) + ["--recover-machine-evidence"])
            proof = json.loads(captured.getvalue())
            assert exit_code == 0, proof
            positive = cli_arguments("verify", repo, run_dir, expected) + ["--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"]]
            checks = [{"name": "完整双审及匹配验证", "expected_exit_code": 0, "arguments": positive}]
            foreign = cli_arguments("verify", repo, run_dir, {**expected, "task_id": "foreign-task"}) + positive[-4:]
            checks.append({"name": "消费者task绑定不符", "expected_exit_code": 4, "arguments": foreign})
            checks.append({"name": "消费者摘要不符", "expected_exit_code": 4, "arguments": positive[:-1] + ["0" * 64]})
            payload = json.loads(Path(proof["evidence_path"]).read_text())
            payload["subject"]["supplemental_review_paths"] = []
            encoded = canonical_bytes(payload);digest = hashlib.sha256(encoded).hexdigest()
            corrupted_path = Path(proof["evidence_path"]).parent / (digest + ".json")
            supplemental.durable_publish(corrupted_path, encoded)
            checks.append({"name": "重签摘要仍不能隐藏同路径差异", "expected_exit_code": 4,
                           "arguments": positive[:-4] + ["--evidence", str(corrupted_path), "--evidence-sha256", digest]})
            for check in checks:
                with redirect_stdout(StringIO()):
                    assert rvf_land.main(check["arguments"]) == check["expected_exit_code"]
                check["rvf_argv"] = ["land", *check.pop("arguments")]
            exported.append({"sample": sample, "synthetic_fixture_only": True, "binding": expected, "checks": checks})
    (output_directory / "consumer-contract-fixtures.json").write_text(json.dumps(
        {"schema": "rvf-supplemental-land-completion-consumer-fixtures", "schema_version": 1,
         "synthetic_fixture_only": True, "samples": exported}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出历史 Land 补证的合成消费契约夹具。")
    parser.add_argument("--export-contract-fixtures", type=Path, required=True)
    export_consumer_contract_fixtures(parser.parse_args().export_contract_fixtures.absolute())


@pytest.mark.parametrize("interruption", ["machine_wait", "missing_leg", "plan_registration"])
def test_real_runner_summary_updates_allow_bound_recovery(tmp_path, capsys, monkeypatch, interruption):
    import dispatch_reviewers
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    monkeypatch.setattr(dispatch_reviewers, "load_registry", lambda path: {})
    monkeypatch.setattr(dispatch_reviewers, "probe_available", lambda registry: ["synthetic"])
    monkeypatch.setattr(dispatch_reviewers, "route", lambda *args, **kwargs: {
        "schema_version": 1, "status": "planned", "routing_rule": "R1", "warnings": [],
        "reviewers": [{"reviewer_id": name, "harness_id": "synthetic"} for name in ("synthetic-a", "synthetic-b")]})
    commands = []
    invocation_counts = {}
    def real_runner_command(reviewer, **kwargs):
        reviewer_id = reviewer["reviewer_id"]
        invocation_counts[reviewer_id] = invocation_counts.get(reviewer_id, 0) + 1
        commands.append(reviewer_id)
        fail_first = invocation_counts[reviewer_id] == 1 and (
            interruption == "machine_wait" or (interruption == "missing_leg" and reviewer_id == "synthetic-b"))
        result = {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [],
                  "audit_summary": "合成 harness 独立核对固定差异；真实 runner 校验 canonical 结果。"}
        program = ("import json,os;from pathlib import Path;"
                   "p=Path(os.environ['RVF_REVIEW_RESULT']);p.parent.mkdir(parents=True,exist_ok=True);"
                   f"p.write_text({json.dumps(result,ensure_ascii=False)!r});print('合成 harness 已执行');")
        if fail_first:
            program = "import sys;print('合成 harness 暂时不可用',file=sys.stderr);raise SystemExit(7)"
        config_path = tmp_path / (reviewer_id + ".json")
        config_path.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:" + reviewer_id,
            "command": [sys.executable, "-c", program], "allow_repo_cwd": True, "output_format": "text"}))
        return [sys.executable, str(SCRIPT_DIRECTORY / "run_alternative_reviewer.py"),
                "--config", str(config_path), "--repo", kwargs["repo"], "--review-packet", kwargs["review_packet"],
                "--scope-contract", kwargs["scope_contract"], "--rvf-run-id", kwargs["run_id"],
                "--rvf-run-dir", kwargs["run_dir"], "--reviewer-id", reviewer_id]
    monkeypatch.setattr(dispatch_reviewers, "_build_reviewer_command", real_runner_command)
    original_execute = dispatch_reviewers.execute_plan
    interrupted = []
    def execute_without_fallback(*args, **kwargs):
        result = original_execute(*args, **kwargs, max_fallback_rounds=0)
        if interruption == "plan_registration" and not interrupted:
            interrupted.append(True)
            raise OSError("合成中断：原生逐腿回执已落盘，整轮 plan 尚未登记")
        return result
    monkeypatch.setattr(dispatch_reviewers, "execute_plan", execute_without_fallback)
    code, first = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == (4 if interruption == "plan_registration" else 3), first
    operation = next((run_dir / supplemental.ARTIFACT_DIRECTORY / "recovery-operations").iterdir())
    review_directory = operation / "supplemental-review-run"
    summary_path = review_directory / "summary.json"
    summary_bytes = summary_path.read_bytes()
    summary = json.loads(summary_bytes)
    assert summary["reason_code"] in {"reviewer_completed", "reviewer_failed"}
    assert summary["run_id"].startswith("supplemental-")
    subject = supplemental.read_regular_json_object(
        review_directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json")["subject"]
    supplemental.ensure_supplemental_review_inputs(subject, review_directory, operation / "target-validation-and-review-workspace")
    assert summary_path.read_bytes() == summary_bytes
    code, proof = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 0, proof
    assert len(commands) == {"machine_wait": 4, "missing_leg": 3, "plan_registration": 2}[interruption]
    code, verified = invoke(capsys, "verify", repo, run_dir, expected,
        "--evidence", proof["evidence_path"], "--evidence-sha256", proof["evidence_sha256"])
    assert code == 0, verified


@pytest.mark.parametrize("identity_field", ["run_id", "repo"])
def test_supplemental_summary_rejects_foreign_identity_without_rewriting_it(tmp_path, capsys, identity_field):
    from core.run_ledger.run_ledger import start_run
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    _code, inspected = invoke(capsys, "inspect", repo, run_dir, expected)
    subject = inspected["subject"]
    review_directory = tmp_path / "identity-review-run"
    supplemental.ensure_supplemental_review_inputs(subject, review_directory, repo)
    ledger = start_run("reviewer", repo=repo, cwd=repo, run_id="supplemental-" + subject["supplemental_review_subject_sha256"],
                       run_dir=review_directory)
    ledger.summary(status="failed", reason_code="reviewer_failed", message="合成身份负例，保留真实摘要结构")
    path = review_directory / "summary.json"
    summary = json.loads(path.read_text())
    summary[identity_field] = "foreign-identity"
    path.write_text(json.dumps(summary))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="身份|仓库"):
        supplemental.ensure_supplemental_review_inputs(subject, review_directory, repo)
    assert path.read_bytes() == before


@pytest.mark.parametrize("recovery_kind", ["native_receipts", "completed_plan", "missing_leg", "foreign_receipt"])
def test_exhausted_review_budget_reconciles_native_receipts_before_new_execution(tmp_path, capsys, monkeypatch, recovery_kind):
    import dispatch_reviewers
    repo, run_dir, expected, _snapshot = historical_case(tmp_path)
    probe_calls = []
    reviewer_calls = []
    monkeypatch.setattr(dispatch_reviewers, "load_registry", lambda path: {})
    def probe_with_two_waits(registry):
        probe_calls.append(True)
        if len(probe_calls) <= 2:
            raise supplemental.MachineEvidencePending("合成环境：前两次 harness 证据尚未恢复")
        return ["synthetic"]
    monkeypatch.setattr(dispatch_reviewers, "probe_available", probe_with_two_waits)
    monkeypatch.setattr(dispatch_reviewers, "route", lambda *args, **kwargs: {
        "schema_version": 1, "status": "planned", "routing_rule": "R1", "warnings": [],
        "reviewers": [{"reviewer_id": name, "harness_id": "synthetic"} for name in ("synthetic-a", "synthetic-b")]})
    def real_runner_command(reviewer, **kwargs):
        reviewer_calls.append(reviewer["reviewer_id"])
        result = {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [],
                  "audit_summary": "预算恢复合成 harness 已核对固定差异，真实 runner 校验结果。"}
        program = ("import os;from pathlib import Path;"
                   "p=Path(os.environ['RVF_REVIEW_RESULT']);p.parent.mkdir(parents=True,exist_ok=True);"
                   f"p.write_text({json.dumps(result,ensure_ascii=False)!r});print('合成 harness 完成');")
        if recovery_kind == "missing_leg" and reviewer["reviewer_id"] == "synthetic-b":
            program = "raise SystemExit(7)"
        config = tmp_path / (reviewer["reviewer_id"] + ".json")
        config.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:" + reviewer["reviewer_id"],
            "command": [sys.executable, "-c", program], "output_format": "text", "allow_repo_cwd": True}))
        return [sys.executable, str(SCRIPT_DIRECTORY / "run_alternative_reviewer.py"), "--config", str(config),
            "--repo", kwargs["repo"], "--review-packet", kwargs["review_packet"], "--scope-contract", kwargs["scope_contract"],
            "--rvf-run-id", kwargs["run_id"], "--rvf-run-dir", kwargs["run_dir"], "--reviewer-id", reviewer["reviewer_id"]]
    monkeypatch.setattr(dispatch_reviewers, "_build_reviewer_command", real_runner_command)
    original_execute = dispatch_reviewers.execute_plan
    def execute_with_registration_interruption(*args, **kwargs):
        plan = original_execute(*args, **kwargs, max_fallback_rounds=0)
        if recovery_kind != "completed_plan":
            raise OSError("合成中断：原生逐腿回执已落盘，尚未登记整轮 plan")
        return plan
    monkeypatch.setattr(dispatch_reviewers, "execute_plan", execute_with_registration_interruption)
    original_write = supplemental.atomic_write
    def interrupt_completed_execution_receipt(path, encoded):
        if recovery_kind == "completed_plan" and path.name == "supplemental-review-execution.json" and json.loads(encoded).get("status") == "completed":
            raise OSError("合成中断：整轮 plan 已完成，执行时间回执尚未登记")
        return original_write(path, encoded)
    monkeypatch.setattr(supplemental, "atomic_write", interrupt_completed_execution_receipt)
    for _ in range(2):
        code, pending = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
        assert code == 3, pending
    code, interrupted = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert code == 4, interrupted
    operation = next((run_dir / supplemental.ARTIFACT_DIRECTORY / "recovery-operations").iterdir())
    state_path = operation / "machine-evidence-recovery-state.json"
    state_before = json.loads(state_path.read_text())
    assert state_before["phases"]["double_review"]["attempt_count"] == 3
    review_directory = operation / "supplemental-review-run"
    receipts = list((review_directory / "artifacts/reviewer-command-outputs").glob("*.execution-result.json"))
    assert len(receipts) == 2
    assert sorted(json.loads(path.read_text())["returncode"] for path in receipts) == ([0, 7] if recovery_kind == "missing_leg" else [0, 0])
    assert json.loads((review_directory / "summary.json").read_text())["reason_code"] in {"reviewer_completed", "reviewer_failed"}
    monkeypatch.setattr(supplemental, "atomic_write", original_write)
    if recovery_kind == "foreign_receipt":
        receipt = json.loads(receipts[0].read_text());receipt["packet_sha256"] = "0" * 64
        receipts[0].write_text(json.dumps(receipt))
    calls_before = (len(probe_calls), len(reviewer_calls))
    if recovery_kind == "native_receipts":
        # 原生进程组仍活着时，纯登记也必须先等待旧执行真正退出。
        from independent_backup_route import control_directory, capture_process_identity
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
        reservation = control_directory(repo) / "normal-mutations" / "synthetic-prior-supplemental-process.json"
        supplemental.durable_json(reservation, {"operation": "supplemental-land-completion-record",
            "original_run_directory": str(run_dir.resolve()), "child_process_group_ids": [process.pid],
            "child_process_group_identities": {str(process.pid): capture_process_identity(process.pid)}})
        try:
            code, pending = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
            assert code == 3 and "旧补证子进程" in pending["reason"], pending
            assert (len(probe_calls), len(reviewer_calls)) == calls_before
        finally:
            process.terminate();process.wait(timeout=10)
    code, recovered = invoke(capsys, "record", repo, run_dir, expected, "--recover-machine-evidence")
    assert (len(probe_calls), len(reviewer_calls)) == calls_before
    state_after = json.loads(state_path.read_text())
    assert state_after["phases"]["double_review"]["attempt_count"] == 3
    assert state_after["attempt_history"][:len(state_before["attempt_history"])] == state_before["attempt_history"]
    assert [entry for entry in state_after["attempt_history"] if entry["phase"] == "double_review"] == state_before["attempt_history"]
    if recovery_kind == "missing_leg":
        assert code == 3 and "三次" in recovered["reason"], recovered
        assert not list((run_dir / supplemental.ARTIFACT_DIRECTORY).glob("*.json"))
    elif recovery_kind == "foreign_receipt":
        assert code == 4 and "回执身份" in recovered["reason"], recovered
    else:
        assert code == 0, recovered
        code, verified = invoke(capsys, "verify", repo, run_dir, expected,
            "--evidence", recovered["evidence_path"], "--evidence-sha256", recovered["evidence_sha256"])
        assert code == 0, verified
