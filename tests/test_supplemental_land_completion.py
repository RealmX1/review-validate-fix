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
