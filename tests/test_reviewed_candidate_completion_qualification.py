"""结构化完成事实、提交前资格与干净候选恢复的隔离回归。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_reviewed_task_candidate_evidence import (
    SCRIPT_DIRECTORY, canonical_bytes, create_completed_run, git, opted_in_repo_with_land_commit,
    record_terminal_fix_attempt, repository, rewrite_contract, run_writer,
    write_merge_dispositions_and_upsert_fix_issues,
)
from test_rvf_land_single_step_prepare_and_commit import commit, prepare, record_validation, run_land

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import write_reviewed_task_candidate_evidence as evidence


EXPANSION_PATHS = [
    "test/integration/codex-native-idle-user-turn-classification-verification-script.integration.test.ts",
    "test/runtime/agent-session-history/kimi-native-conversation-activity-reader.test.ts",
]
C4C82_EXPANSION_PATHS = [
    "web-ui/src/components/detail-panels/cross-project-recently-active-task-switcher-host-context.tsx",
    "web-ui/src/hooks/use-cross-project-recently-active-task-switcher-host.ts",
    "web-ui/src/hooks/use-cross-project-task-open-in-project.ts",
]
UNREVIEWED_199E6_FALLOUT_PATH = "test/runtime/terminal/prepare-agent-launch-asserted-relation-injection-receipt-only-when-relation-text-reaches-launch.test.ts"
SUCCESSFUL_38FD3_CANDIDATE_PATHS = [
    "docs/context-cross-project-recently-active-task-switcher.md",
    "web-ui/src/App.tsx",
    "web-ui/src/components/card-detail-view.tsx",
    "web-ui/src/components/detail-panels/cross-project-recently-active-task-switcher-section-blocks.tsx",
    "web-ui/src/hooks/use-all-projects-focus-view-adjacent-task-keyboard-navigation.test.tsx",
    "web-ui/src/hooks/use-all-projects-focus-view-adjacent-task-keyboard-navigation.ts",
    "web-ui/src/hooks/use-cross-project-recently-active-task-switcher-sections.ts",
    "web-ui/src/hooks/use-cross-project-task-switcher-focused-task-visibility.ts",
    "web-ui/src/state/cross-project-task-switcher-focus-view-visible-task-rows.ts",
    "web-ui/src/utils/adjacent-task-card-navigation-hotkey-event.ts",
    "web-ui/tests/adjacent-task-card-alt-arrow-hotkey-passthrough.spec.ts",
    "web-ui/tests/all-projects-adjacent-task-keyboard-navigation-browser-harness.tsx",
    "web-ui/tests/all-projects-adjacent-task-keyboard-navigation.spec.ts",
]


def completed_machine_issue_run(tmp_path: Path, count: int = 1, *, expansion_paths: list[str] | None = None) -> tuple[Path, Path]:
    repo = repository(tmp_path / "repo")
    for path in expansion_paths or []:
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("reviewed expansion\n")
    run_dir = create_completed_run(tmp_path, repo)
    result_path = run_dir / "artifacts/reviewers/reviewer-a/review-result.json"
    result = {"schema_version": 1, "kind": "issues", "requests": [], "issues": [
        {"path": "tracked.txt", "line": index + 1, "kind": "REAL", "severity": "medium", "message": f"issue {index + 1}"}
        for index in range(count)
    ]}
    result_path.write_text(json.dumps(result))
    ids = [f"RVF-{index + 1:03d}" for index in range(count)]
    decisions = {"dispositions": [
        {"reviewer_id": "reviewer-a", "index": index + 1, "disposition": "adopted", "issue_id": issue_id}
        for index, issue_id in enumerate(ids)
    ]}
    write_merge_dispositions_and_upsert_fix_issues(tmp_path, repo, run_dir, decisions, ids)
    for index, issue_id in enumerate(ids):
        changed = [expansion_paths[index]] if expansion_paths and index < len(expansion_paths) else []
        metadata = {"expanded_paths": changed, "declared_paths": changed, "reason": "该修复需要直接测试"} if changed else {}
        record_terminal_fix_attempt(run_dir, issue_id, changed_paths=changed, scope_expansion=metadata)
    (run_dir / "artifacts/handoff.md").write_text(
        f"- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n共 {count} 条，{count} 真实修复 / 0 误报 / 0 升级\n"
    )
    return repo, run_dir


def source_and_index_snapshot(repo: Path) -> dict[str, str]:
    paths = [path for path in repo.rglob("*") if path.is_file() and ".git" not in path.relative_to(repo).parts]
    index = Path(git(repo, "rev-parse", "--absolute-git-dir")) / "index"
    return {"HEAD": git(repo, "rev-parse", "HEAD"), "INDEX": hashlib.sha256(index.read_bytes()).hexdigest(),
            **{str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}


@pytest.mark.parametrize("count,summary", [
    (4, "共 4 条：真实修复 4 条，误报 0 条，升级 0 条。"),
    (5, "共 5 条，5 真实修复（已应用）/ 0 误报 / 0 待用户决策的升级；验证完成。"),
    (3, "共 3 条（由 5 条 reviewer issue 合并），3 真实修复 / 0 误报 / 0 升级"),
    (2, "共 2 条（由 3 条 reviewer issue 合并）：2 条真实修复，0 误报，0 升级。"),
    (4, "共 4 条 canonical issue，4 真实修复 / 0 误报 / 0 转 backlog / 0 升级"),
    (6, "合计 6 条 canonical issue：6 真实修复 / 0 误报 / 0 backlog / 0 升级"),
])
def test_structured_facts_accept_the_six_original_summary_formats(tmp_path: Path, count: int, summary: str) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path, count)
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text("- handoff_status: COMPLETED\n- review_status: COMPLETED\n" + summary +
                       "\n本轮无 `[elevated]` 条目；标题提到 [fixed] / [false_positive] 不构成结果条目。\n")
    written = run_writer(repo, run_dir)
    assert written.returncode == 0, written.stderr
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["review"]["canonical_issue_count"] == count


@pytest.mark.parametrize("mutation", ["summary", "elevated", "failed", "unapplied", "newer_failed", "newer_started", "missing_result", "stale_dispositions", "foreign_issue"])
def test_precommit_refusal_preserves_head_index_worktree_and_formal_evidence(tmp_path: Path, mutation: str) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path)
    (repo / "tracked.txt").write_text("reviewed change\n")
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable, "-c", "print('passed')").returncode == 0
    attempt_dir = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    if mutation in {"summary", "elevated", "failed"}:
        handoff = run_dir / "artifacts/handoff.md"
        text = handoff.read_text()
        text = text.replace("共 1 条", "共 2 条") if mutation == "summary" else text + f"\n- **[{mutation}]** RVF-001\n"
        handoff.write_text(text)
    elif mutation == "unapplied":
        path = attempt_dir / "attempt.json"
        payload = json.loads(path.read_text());payload["status"] = "fixed";path.write_text(json.dumps(payload))
    elif mutation in {"newer_failed", "newer_started"}:
        newer = record_terminal_fix_attempt(run_dir, "RVF-001", status="failed", attempt_number=2)
        if mutation == "newer_started":
            path = newer / "attempt.json";payload = json.loads(path.read_text());payload["status"] = "started";path.write_text(json.dumps(payload))
            (newer / "result.json").unlink()
        os.utime(attempt_dir / "result.json", (2_000_000_000, 2_000_000_000))
    elif mutation == "missing_result":
        (attempt_dir / "result.json").unlink()
    elif mutation == "stale_dispositions":
        path = run_dir / "artifacts/reviewers/reviewer-a/review-result.json"
        payload = json.loads(path.read_text());payload["issues"][0]["message"] = "redispatched";path.write_text(json.dumps(payload))
    else:
        path = run_dir / "artifacts/fix-issues/RVF-001.json"
        payload = json.loads(path.read_text());payload["run_id"] = "another-run";path.write_text(json.dumps(payload))
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    before = source_and_index_snapshot(repo)
    contract = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 28, checked.stdout + checked.stderr
    refused = commit(tmp_path, repo, run_dir)
    assert refused.returncode in {27, 28}, refused.stdout + refused.stderr
    assert source_and_index_snapshot(repo) == before
    assert (run_dir / "artifacts/inputs/scope.contract.json").read_bytes() == contract
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()
    assert not (Path(git(repo, "rev-parse", "--absolute-git-dir")) / evidence.EVIDENCE_RELATIVE_PATH).exists()
    receipt = json.loads((run_dir / "artifacts/rvf-land-commit-result.json").read_text())
    assert receipt["head_oid_before_commit"] == receipt["head_oid_after_commit"] == before["HEAD"]


def test_public_dry_run_is_read_only_and_clean_commit_recovery_is_idempotent(tmp_path: Path) -> None:
    repo, run_dir, reviewed_tip, landed_tip = opted_in_repo_with_land_commit(tmp_path)
    rewrite_contract(run_dir, candidate_task_tip=reviewed_tip)
    before = source_and_index_snapshot(repo)
    artifacts_before = {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    checked = run_land(tmp_path, repo, "check-evidence", "--repo", str(repo), "--run-dir", str(run_dir))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["status"] == "eligible"
    assert source_and_index_snapshot(repo) == before
    assert {str(path): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()} == artifacts_before
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    first = commit(tmp_path, repo, run_dir);assert first.returncode == 0, first.stdout + first.stderr
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "nothing_to_commit"
    first_payload, path, digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert first_payload["subject"]["reviewed_task_tip"] == reviewed_tip
    for _ in range(2):
        assert prepare(tmp_path, repo, run_dir).returncode == 0
        repeated = commit(tmp_path, repo, run_dir);assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert json.loads(repeated.stdout)["candidate_evidence"]["status"] == "reused"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert git(repo, "rev-parse", "HEAD") == landed_tip
    assert not (path.parent / "immutable").exists()


@pytest.mark.parametrize("expansion_paths,canonical_count", [(EXPANSION_PATHS, 3), (C4C82_EXPANSION_PATHS, 4)], ids=["46d51", "c4c82"])
def test_declared_applied_expansion_paths_generate_bound_v2_evidence(tmp_path: Path, expansion_paths: list[str], canonical_count: int) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path, canonical_count, expansion_paths=expansion_paths)
    frozen = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert result["candidate_evidence_qualification"]["consumer_contract_update_required"] is True
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == 2
    receipt_path = Path(payload["scope"]["applied_scope_expansion_receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    assert {path for entry in receipt["expansions"] for path in entry["paths"]} == set(expansion_paths)
    reviewed_contract = json.loads((run_dir / "artifacts/inputs/scope.contract.reviewed.json").read_text())
    assert canonical_bytes(reviewed_contract) == canonical_bytes(json.loads(frozen))
    assert reviewed_contract["primary_files"] == ["tracked.txt"]
    again = evidence.write_evidence(repo, run_dir)
    assert again["status"] == "reused"
    consumer = Path(os.environ.get("RVF_TEST_HAND_BACK_CONSUMER_SCRIPT", str(Path.home() / ".codex/skills/base-branch-sync/scripts/base-branch-sync-evidence-aware-optimistic-handback.py")))
    if consumer.is_file():
        inspected = subprocess.run([sys.executable, str(consumer), "inspect-reviewed-candidate-evidence", "--task-worktree", str(repo)],
                                   capture_output=True, text=True)
        assert inspected.returncode != 0, inspected.stdout + inspected.stderr
        assert "schema_version" in inspected.stdout + inspected.stderr
    receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="canonical JSON|digest"):
        evidence.validated_reviewed_task_candidate_evidence(repo)


def test_confirmed_background_expansion_is_stageable_and_receipted(tmp_path: Path) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path, expansion_paths=EXPANSION_PATHS[:1])
    rewrite_contract(run_dir, background_files=EXPANSION_PATHS[:1], protected_files=EXPANSION_PATHS[:1])
    directory = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    metadata = {"acknowledged_protected_or_background_paths": EXPANSION_PATHS[:1], "reason": "主会话已核对归属与内容"}
    for name in ("attempt.json", "result.json"):
        path = directory / name;payload = json.loads(path.read_text());payload["scope_expansion"] = metadata
        if name == "attempt.json":
            payload["confirmed_acknowledged_protected_or_background_paths"] = EXPANSION_PATHS[:1]
        path.write_text(json.dumps(payload))
    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert EXPANSION_PATHS[0] in json.loads(prepared.stdout)["stageable_dirty_paths"]
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    payload, _path, _digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == 2


@pytest.mark.parametrize("run_id,count", [("rvf-review-start-5a0e72d65bd5c0c4", 4), ("rvf-review-start-780c3041b3ea0ec8", 6)])
def test_original_handoff_bytes_are_accepted_with_complete_isolated_machine_facts(tmp_path: Path, run_id: str, count: int) -> None:
    source = Path("/Users/bominzhang/.rvf/user-local-data/run-logs/runs") / run_id / "artifacts/handoff.md"
    if not source.is_file():
        pytest.skip("本机没有该真实样本；等价原文格式由固定 fixture 覆盖")
    original_bytes = source.read_bytes()
    repo, run_dir = completed_machine_issue_run(tmp_path, count)
    handoff = run_dir / "artifacts/handoff.md";handoff.write_bytes(original_bytes)
    written = run_writer(repo, run_dir)
    assert written.returncode == 0, written.stderr
    assert handoff.read_bytes() == original_bytes == source.read_bytes()


@pytest.mark.parametrize("mutation", ["undeclared", "not_applied", "missing_confirmation", "wrong_patch", "extra_path"])
def test_expansion_never_admits_unproven_paths(tmp_path: Path, mutation: str) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path, expansion_paths=EXPANSION_PATHS[:1])
    attempt_dir = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    a = json.loads((attempt_dir / "attempt.json").read_text());r = json.loads((attempt_dir / "result.json").read_text())
    if mutation == "undeclared":
        a["scope_expansion"]["declared_paths"] = []
    elif mutation == "not_applied":
        a["status"] = "fixed"
    elif mutation == "missing_confirmation":
        a["scope_expansion"] = {"acknowledged_protected_or_background_paths": EXPANSION_PATHS[:1], "reason": "需要核对"}
    elif mutation == "wrong_patch":
        r["fix_patch_path"] = str(tmp_path / "foreign.patch")
    else:
        fallout = repo / UNREVIEWED_199E6_FALLOUT_PATH
        fallout.parent.mkdir(parents=True, exist_ok=True)
        fallout.write_text("unreviewed\n")
    r["scope_expansion"] = a["scope_expansion"]
    (attempt_dir / "attempt.json").write_text(json.dumps(a));(attempt_dir / "result.json").write_text(json.dumps(r))
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable, "-c", "print('passed')").returncode == 0
    extra = ["--extra-path", str(repo / UNREVIEWED_199E6_FALLOUT_PATH)] if mutation == "extra_path" else (
        ["--extra-path", str(repo / EXPANSION_PATHS[0])] if mutation == "not_applied" else []
    )
    assert prepare(tmp_path, repo, run_dir, *extra).returncode == 0
    before = source_and_index_snapshot(repo)
    rejected = commit(tmp_path, repo, run_dir, *extra)
    assert rejected.returncode == 28, rejected.stdout + rejected.stderr
    assert source_and_index_snapshot(repo) == before


@pytest.mark.parametrize("leg_count,plan_status", [(2, "planned"), (1, "planned"), (3, "planned"), (2, "failed")])
def test_r3_requires_exactly_two_valid_mimic_results(tmp_path: Path, leg_count: int, plan_status: str) -> None:
    repo = repository(tmp_path / "repo");run_dir = create_completed_run(tmp_path, repo)
    root = run_dir / "artifacts/reviewers"
    (root / "reviewer-a").rename(root / "codex-mimic-reviewer-a")
    (root / "reviewer-b").rename(root / "codex-mimic-reviewer-b")
    if leg_count == 1:
        (root / "codex-mimic-reviewer-b/review-result.json").unlink()
    if leg_count == 3:
        (root / "unexpected-reviewer").mkdir()
        (root / "unexpected-reviewer/review-result.json").write_bytes((root / "codex-mimic-reviewer-a/review-result.json").read_bytes())
    (root / "reviewer-plan.json").write_text(json.dumps({"schema_version": 1, "status": plan_status,
        "routing_rule": "R3", "needs_last_resort_fallback": True, "reviewers": []}))
    written = run_writer(repo, run_dir)
    assert (written.returncode == 0) == (leg_count == 2 and plan_status == "planned"), written.stderr


def test_global_ignore_does_not_stage_local_settings_or_hide_tracked_changes(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    local_settings = repo / ".claude/settings.local.json";local_settings.parent.mkdir();local_settings.write_text("{}\n")
    excludes = tmp_path / "user-global-excludes";excludes.write_text("**/.claude/settings.local.json\ntracked.txt\n")
    git(repo, "config", "core.excludesFile", str(excludes))
    (repo / "tracked.txt").write_text("reviewed change\n")
    run_dir = create_completed_run(tmp_path, repo)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir);assert landed.returncode == 0, landed.stdout + landed.stderr
    assert json.loads(landed.stdout)["candidate_evidence"]["status"] == "written"
    assert ".claude/settings.local.json" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    (repo / "tracked.txt").write_text("unreviewed change\n")
    assert run_writer(repo, run_dir).returncode != 0


def test_already_staged_deletion_is_committed_and_result_is_persistent(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo");git(repo, "rm", "tracked.txt")
    run_dir = create_completed_run(tmp_path, repo)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir);assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert result["candidate_evidence"]["status"] == "written"
    path = Path(result["land_commit_result_path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["land_commit_result_sha256"]
    receipt = json.loads(path.read_text())
    assert receipt["result"]["commit_oid"] == git(repo, "rev-parse", "HEAD")
    assert receipt["result"]["staged_paths"] == ["tracked.txt"]


def test_changed_qualification_artifacts_refuse_before_contract_or_evidence_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, run_dir, reviewed_tip, _landed = opted_in_repo_with_land_commit(tmp_path)
    rewrite_contract(run_dir, candidate_task_tip=reviewed_tip)
    prepared = evidence.prepare_candidate_evidence(repo, run_dir)
    handoff = run_dir / "artifacts/handoff.md";handoff.write_text(handoff.read_text() + "\n并发更新\n")
    original_contract = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    monkeypatch.setattr(evidence, "prepare_candidate_evidence", lambda *_args, **_kwargs: prepared)
    with pytest.raises(evidence.CandidateQualificationRefused) as refusal:
        evidence.write_evidence(repo, run_dir)
    assert refusal.value.reason_code == "qualification_artifacts_changed"
    assert (run_dir / "artifacts/inputs/scope.contract.json").read_bytes() == original_contract
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()
    assert not prepared["evidence_path"].exists()


def test_public_shortcut_dispatches_read_only_qualification_without_run_pointer(tmp_path: Path) -> None:
    from test_rvf_land_single_step_prepare_and_commit import land_environment

    repo = repository(tmp_path / "repo");run_dir = create_completed_run(tmp_path, repo)
    env = land_environment(tmp_path)
    completed = subprocess.run([sys.executable, str(SCRIPT_DIRECTORY / "rvf_command_line_shortcut_entrypoint.py"), "land", "check-evidence",
                                "--repo", str(repo), "--run-dir", str(run_dir)], env=env, capture_output=True, text=True, cwd=repo)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["status"] == "eligible"
    assert not (run_dir / "artifacts/rvf-land-prepare-plan.json").exists()
    assert not (Path(env["RVF_USER_LOCAL_DATA_ROOT"]) / "rvf-land-most-recent-prepare-run-directory-pointer-by-repository").exists()


@pytest.mark.parametrize("state", ["pending", "created_after_retry", "duplicate_pending", "preview"])
def test_backlog_completion_checks_real_durable_submission_states(tmp_path: Path, state: str) -> None:
    from reviewed_candidate_completion_facts import require_durable_backlog_submission

    run_dir = tmp_path / "run";run_dir.mkdir()
    pending_path = tmp_path / "persisted-pending-submission.json"
    pending_path.write_text(json.dumps({"run_dir": str(run_dir), "finding_fingerprints": ["finding-sha256"]}))
    ledger = tmp_path / "submission-ledger.jsonl"
    ledger.write_text(json.dumps({"finding_fingerprint": "finding-sha256", "outcome": "pending_kanban_runtime_unavailable",
                                  "pending_prompt_path": str(pending_path)}) + "\n")
    finding = {"finding_fingerprint": "finding-sha256", "outcome": "pending_kanban_runtime_unavailable"}
    group = {"outcome": "pending_kanban_runtime_unavailable", "pending_prompt_path": str(pending_path), "findings": [finding]}
    if state == "created_after_retry":
        pending_path.unlink()
        ledger.write_text(json.dumps({"finding_fingerprint": "finding-sha256", "outcome": "created_kanban_backlog_task",
                                      "kanban_task_id": "actual-submission-fixture-id"}) + "\n")
    elif state == "duplicate_pending":
        group["outcome"] = "nothing_new_to_submit"
        finding["outcome"] = "skipped_already_submitted_identical_finding"
    elif state == "preview":
        group["outcome"] = "previewed_without_creating_kanban_task"
    result = {"run_id": "bound-run", "issue_id": "RVF-001", "attempt_id": "attempt-1",
              "kanban_backlog_submission": {"issue_id": "RVF-001", "attempt_id": "attempt-1", "ledger_path": str(ledger), "groups": [group]}}
    if state == "preview":
        with pytest.raises(ValueError):
            require_durable_backlog_submission(result, run_dir)
    else:
        require_durable_backlog_submission(result, run_dir)


def test_precommit_refusal_preserves_existing_formal_evidence_bytes(tmp_path: Path) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path)
    written = evidence.write_evidence(repo, run_dir)
    path = Path(written["candidate_evidence_path"])
    before = {path: path.read_bytes(), Path(str(path) + ".sha256"): Path(str(path) + ".sha256").read_bytes()}
    (repo / "tracked.txt").write_text("candidate updated\n")
    handoff = run_dir / "artifacts/handoff.md";handoff.write_text(handoff.read_text().replace("共 1 条", "共 2 条"))
    assert record_validation(tmp_path, repo, run_dir, "targeted-contract-tests", sys.executable, "-c", "print('passed')").returncode == 0
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    snapshot = source_and_index_snapshot(repo)
    refused = commit(tmp_path, repo, run_dir)
    assert refused.returncode == 28, refused.stdout + refused.stderr
    assert source_and_index_snapshot(repo) == snapshot
    assert all(artifact.read_bytes() == content for artifact, content in before.items())
    assert not (path.parent / "immutable").exists()


def test_invalid_integration_receipt_refuses_before_restamping_contract(tmp_path: Path) -> None:
    repo, run_dir, reviewed_tip, _landed = opted_in_repo_with_land_commit(tmp_path)
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    receipt_path = git_dir / "base-branch-sync/reviewed-task-handback-integration-candidate.json"
    receipt_digest = evidence.write_json_with_digest(receipt_path, {"candidate_task_tip": reviewed_tip, "base_ref": "incorrect-ref"})
    rewrite_contract(run_dir, candidate_task_tip=reviewed_tip, integration_candidate_receipt_path=str(receipt_path),
                     integration_candidate_receipt_digest=receipt_digest)
    before = (run_dir / "artifacts/inputs/scope.contract.json").read_bytes()
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    refused = commit(tmp_path, repo, run_dir)
    assert refused.returncode == 28, refused.stdout + refused.stderr
    assert json.loads(refused.stdout)["reason_code"] == "candidate_receipt_invalid"
    assert (run_dir / "artifacts/inputs/scope.contract.json").read_bytes() == before
    assert not (run_dir / "artifacts/inputs/scope.contract.reviewed.json").exists()


def test_false_positive_count_comes_from_terminal_result_instead_of_handoff_tags(tmp_path: Path) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path)
    directory = run_dir / "artifacts/fix-attempts/attempt-RVF-001-1"
    for name in ("attempt.json", "result.json"):
        path = directory / name;payload = json.loads(path.read_text());payload["status"] = "false_positive";path.write_text(json.dumps(payload))
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text("- handoff_status: COMPLETED\n- review_status: COMPLETED\n共 1 条，0 真实修复 / 1 误报 / 0 升级\n"
                       "- **[false_positive]** RVF-001\n正文里的 [fixed] 不构成实际修复。\n")
    qualification = evidence.candidate_evidence_qualification(repo, run_dir)
    assert qualification["outcome_counts"] == {"fixed": 0, "false_positive": 1, "submitted_to_kanban_backlog": 0}
    assert run_writer(repo, run_dir).returncode == 0


def test_result_persistence_failure_preserves_the_true_committed_result_in_json(tmp_path: Path) -> None:
    from test_rvf_land_single_step_prepare_and_commit import reviewed_dirty_run

    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    before = git(repo, "rev-parse", "HEAD")
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    (run_dir / "artifacts/rvf-land-commit-execution-results").write_text("此 fixture 模拟无法创建结果目录\n")
    completed = commit(tmp_path, repo, run_dir)
    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["status"] == "committed"
    assert result["reason_code"] == "land_commit_result_persistence_failed"
    assert result["commit_oid"] == git(repo, "rev-parse", "HEAD") != before
    assert result["candidate_evidence"]["status"] == "written"


def test_successful_38fd3_clean_path_keeps_v1_six_validations_and_idempotent_formal_land(tmp_path: Path) -> None:
    from test_rvf_land_single_step_prepare_and_commit import counting_validation_command, counter_value

    repo = repository(tmp_path / "repo")
    for path in SUCCESSFUL_38FD3_CANDIDATE_PATHS:
        destination = repo / path;destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("该 fixture 复用真实路径归属；文件内容属于隔离测试。\n")
    run_dir = create_completed_run(tmp_path, repo)
    rewrite_contract(run_dir, primary_files=SUCCESSFUL_38FD3_CANDIDATE_PATHS,
                     fix_allowlist=SUCCESSFUL_38FD3_CANDIDATE_PATHS, task_change_paths=[])
    (run_dir / "artifacts/handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n"
        "共 0 条，0 真实修复 / 0 误报 / 0 转 backlog / 0 升级。\n"
    )
    counter = tmp_path / "successful-control-validation-execution-count.txt"
    for index in range(2, 7):
        checked = record_validation(tmp_path, repo, run_dir, f"successful-control-validation-{index}", *counting_validation_command(counter))
        assert checked.returncode == 0, checked.stderr
    assert counter_value(counter) == 5
    assert not (run_dir / "artifacts/fix-issues").exists()
    assert not (run_dir / "artifacts/fix-attempts").exists()
    reviewed_tip = git(repo, "rev-parse", "HEAD")
    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    assert set(report["stageable_dirty_paths"]) == set(SUCCESSFUL_38FD3_CANDIDATE_PATHS)
    assert report["stageable_dirty_paths_outside_candidate_evidence_landing_universe"] == []
    assert report["candidate_evidence_qualification"]["canonical_issue_count"] == 0
    assert report["candidate_evidence_qualification"]["evidence_schema_version"] == 1
    landed = commit(tmp_path, repo, run_dir);assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert result["status"] == "committed"
    assert result["validation"]["mode"] == "reused_run_validation_index_with_zero_execution"
    payload, _path, digest = evidence.validated_reviewed_task_candidate_evidence(repo)
    assert payload["schema_version"] == 1
    assert payload["review"]["canonical_issue_count"] == 0
    assert payload["review"]["conclusion"] == "no_issues"
    assert len(payload["validation_records"]) == 6
    assert payload["subject"]["reviewed_task_tip"] == reviewed_tip
    committed_tip = git(repo, "rev-parse", "HEAD")
    assert committed_tip != reviewed_tip
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    repeated = commit(tmp_path, repo, run_dir);assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "nothing_to_commit"
    assert json.loads(repeated.stdout)["candidate_evidence"]["status"] == "reused"
    assert evidence.validated_reviewed_task_candidate_evidence(repo)[2] == digest
    assert git(repo, "rev-parse", "HEAD") == committed_tip
    assert counter_value(counter) == 5


@pytest.mark.parametrize("field", ["protected_files", "excluded_path_prefixes"])
def test_scope_guard_fields_cannot_disagree_with_the_frozen_canonical_scope(tmp_path: Path, field: str) -> None:
    repo, run_dir = completed_machine_issue_run(tmp_path, expansion_paths=EXPANSION_PATHS[:1])
    rewrite_contract(run_dir, **{field: EXPANSION_PATHS[:1]})
    path = run_dir / "artifacts/inputs/scope.contract.json"
    contract = json.loads(path.read_text());contract[field] = [];path.write_text(json.dumps(contract))
    before = source_and_index_snapshot(repo)
    result = evidence.candidate_evidence_qualification(repo, run_dir, pending_stage_paths=set(EXPANSION_PATHS[:1]))
    assert result["status"] == "refused_fail_closed"
    assert result["reason_code"] == "candidate_identity_invalid"
    assert source_and_index_snapshot(repo) == before
