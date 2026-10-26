"""以隔离 Kanban/Git/tracker 验证已落地未审提交的发现、冻结及恢复。"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "committed_candidate_investigation_probe",
    ROOT / "docs/investigations/2026-10-19-explicit-rvf-committed-candidate-omission/reproduce_committed_candidate_omission.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

from adapters.cline_kanban import committed_candidate_provenance as provenance
from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
from core.session_scope_allocation import review_highwater_marker as highwater
import stop_decision_candidate_summary as summary_module


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    with probe.isolated_runtime_environment(tmp_path):
        repo, initial = probe.initialize_repository(tmp_path)
        probe.run_git(repo, "checkout", "-q", "-b", "candidate-task")
        event = probe.hook_event(repo)
        probe.capture_baseline(event)
        directory = tmp_path / "isolated-kanban-state/workspaces/registered-project"
        directory.mkdir(parents=True)
        monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_STATE_DIR", str(directory.parents[1]))
        task_id = event["kanban_task_id"]
        (directory / "sessions.json").write_text(json.dumps({task_id: {"taskId": task_id, "workspacePath": str(repo)}}))
        (directory / "board.json").write_text(json.dumps({"columns": [{"cards": [{"id": task_id, "baseRef": "main"}]}]}))
        yield {"repo": repo, "initial": initial, "event": event, "directory": directory}


def publish(scenario, commits=(), *, base_ref="main", initial=None, incarnation=None):
    repo, event, directory = scenario["repo"], scenario["event"], scenario["directory"]
    initial = initial or scenario["initial"]
    creation = {"initialBaseCommit": initial, "startedAt": 1, "incarnationId": initial + ":1",
                "captureSource": "worktree_creation", "observedTaskCommits": [
                    {"commitSha": sha, "stablePatchId": provenance._stable_patch_id(repo, sha)} for sha in commits]}
    if incarnation:
        creation.update(incarnation)
    entry = {"taskId": event["kanban_task_id"], "baseRef": base_ref,
             "incarnations": [creation], "lastWorkspaceGitSnapshot": {"path": str(repo)}}
    (directory / "task-commit-integration-provenance.json").write_text(json.dumps({"schemaVersion": 1, "tasks": {event["kanban_task_id"]: entry}}))
    (directory / "board.json").write_text(json.dumps({"columns": [{"cards": [{"id": event["kanban_task_id"], "baseRef": base_ref}]}]}))


def candidate_summary(scenario, **options):
    event = scenario["event"]
    return summary_module.build_candidate_summary(event=event, repo=event["cwd"], cwd=event["cwd"],
                                                 workspace_guard=lambda **_: None, **options)


def evidence(scenario):
    return candidate_summary(scenario)["committed_scope_evidence"]


@pytest.mark.parametrize("integration", ["ff", "merge"])
@pytest.mark.parametrize("existing_highwater", [False, True])
def test_integrated_own_commits_remain_visible_and_allocatable(scenario, integration, existing_highwater, monkeypatch):
    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "应审内容\n")
    publish(scenario, [own])
    probe.register_committed_work(repo, scenario["initial"])
    probe.run_git(repo, "checkout", "-q", "main")
    if integration == "merge":
        probe.commit_file(repo, "external.txt", "外部工作\n")
        probe.run_git(repo, "merge", "-q", "--no-ff", "candidate-task", "-m", "test: 合入 task")
    else:
        probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.capture_baseline(event)
    if existing_highwater:
        highwater.write_review_highwater(repo=str(repo), task_id=event["kanban_task_id"],
                                        session_id=event["session_id"], reviewed_head=scenario["initial"])
    result = candidate_summary(scenario)
    assert result["candidate_path_count"] == 1
    assert result["committed_round_baseline"] == scenario["initial"]
    assert result["candidate_discovery_complete"] is True
    import stop_review_validate_fix as stop_hook
    from core.run_ledger.run_ledger import start_run
    routed_paths = []
    monkeypatch.setattr(stop_hook, "route_reviewable_scope", lambda event, repo, paths, *args, **kwargs: routed_paths.extend(paths) or "committed-route")
    ledger = start_run("isolated-committed-stop-detection", repo=str(repo), cwd=str(repo))
    assert stop_hook.maybe_route_committed_round_scope(event, str(repo), ledger, str(repo)) == "committed-route"
    assert routed_paths == ["own.txt"]
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id=event["session_id"], run_id="isolated-integration-review", reviewer_id="test-reviewer",
        committed_baseline=result["committed_round_baseline"], committed_base_ref="main",
        committed_scope_evidence=result["committed_scope_evidence"], auto_claim_observed=False,
    )
    assert allocation["status"] == "allocated"
    assert allocation["scope"]["paths"] == ["own.txt"]


@pytest.mark.parametrize("absorption", ["ff", "merge", "rebase"])
def test_base_absorption_does_not_create_task_candidates(scenario, absorption):
    repo = scenario["repo"]
    own = None if absorption == "ff" else probe.commit_file(repo, "own.txt", "本任务\n")
    publish(scenario, [own] if own else [])
    probe.run_git(repo, "checkout", "-q", "main")
    probe.commit_file(repo, "external.txt", "外部任务\n")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    if absorption == "rebase":
        probe.run_git(repo, "rebase", "-q", "main")
    else:
        probe.run_git(repo, "merge", "-q", "main", "-m", "test: 吸收 main")
    probe.capture_baseline(scenario["event"])
    result = candidate_summary(scenario)
    assert result["candidate_path_count"] == (0 if own is None else 1)
    assert result["candidate_discovery_complete"] is True
    assert all(item["path"] != "external.txt" for item in result["candidates"])
    if absorption == "rebase":
        rewritten = probe.run_git(repo, "rev-parse", "HEAD")
        assert rewritten in result["committed_scope_evidence"]["task_owned_commit_shas"]
        probe.run_git(repo, "checkout", "-q", "main")
        probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
        probe.run_git(repo, "checkout", "-q", "candidate-task")
        probe.capture_baseline(scenario["event"])
        assert candidate_summary(scenario)["candidate_path_count"] == 1


def test_non_main_base_and_skip_trailer(scenario):
    repo = scenario["repo"]
    probe.run_git(repo, "branch", "release")
    own = probe.commit_file(repo, "own.txt", "应审\n")
    skipped = probe.commit_file(repo, "skipped.txt", "明确略过\n")
    probe.run_git(repo, "commit", "--amend", "-q", "-m", "test: 略过\n\nRVF-Skip-Review: 隔离控制")
    skipped = probe.run_git(repo, "rev-parse", "HEAD")
    publish(scenario, [own, skipped], base_ref="release")
    probe.run_git(repo, "checkout", "-q", "release")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    result = candidate_summary(scenario)
    assert result["committed_round_base_ref"] == "release"
    assert [item["path"] for item in result["candidates"]] == ["own.txt"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_incarnation", "wrong_worktree", "missing_owned_commits"])
def test_invalid_provenance_remains_unknown_and_recovers_after_restart(scenario, damage, monkeypatch):
    repo, directory = scenario["repo"], scenario["directory"]
    own = probe.commit_file(repo, "own.txt", "未审\n")
    publish(scenario, [own])
    path = directory / "task-commit-integration-provenance.json"
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{")
    else:
        payload = json.loads(path.read_text())
        entry = payload["tasks"][scenario["event"]["kanban_task_id"]]
        if damage == "missing_owned_commits":
            del entry["incarnations"][0]["observedTaskCommits"]
        elif damage == "wrong_incarnation":
            entry["incarnations"][0]["incarnationId"] = "旧 incarnation"
        else:
            entry["lastWorkspaceGitSnapshot"]["path"] = str(directory)
        path.write_text(json.dumps(payload))
    first = evidence(scenario)
    assert first["status"] == "unknown"
    # 模拟全新调用进程：相同输入复用持久 unknown，不重复重量级补证。
    with monkeypatch.context() as patcher:
        patcher.setattr(provenance, "_map_owned_commit_identities", lambda *a: pytest.fail("不应无变化重试"))
        assert evidence(scenario) == first
    publish(scenario, [own])
    assert evidence(scenario)["status"] == "verified"


def test_checkpoint_and_tracker_restore_missing_provenance(scenario):
    repo, event = scenario["repo"], scenario["event"]
    checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}", "-p", scenario["initial"], "-m", "test: 首轮 checkpoint")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    own = probe.commit_file(repo, "own.txt", "已证明本会话归属\n")
    probe.register_committed_work(repo, scenario["initial"])
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    result = candidate_summary(scenario)
    assert result["committed_scope_evidence"]["status"] == "verified"
    assert result["committed_scope_evidence"]["task_owned_commit_shas"] == [own]
    assert result["candidate_path_count"] == 1


def test_checkpoint_and_original_native_commit_receipt_restore_provenance(scenario, tmp_path):
    repo, event = scenario["repo"], scenario["event"]
    checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}", "-p", scenario["initial"], "-m", "test: 首轮")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    own = probe.commit_file(repo, "own.txt", "原始会话提交回执\n")
    transcript = tmp_path / "original-session-commit-receipt.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": event["session_id"], "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "commit-call",
          "arguments": json.dumps({"cmd": "git commit -m 'test: 原始提交'", "workdir": str(repo)})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "commit-call",
          "output": f"Process exited with code 0\n[candidate-task {own[:12]}] test: 原始提交"}},
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    event["transcript_path"] = str(transcript)
    result = evidence(scenario)
    assert result["status"] == "verified"
    assert result["task_owned_commit_shas"] == [own]


def test_reviewed_old_content_does_not_hide_new_hunk(scenario):
    repo, event = scenario["repo"], scenario["event"]
    first = probe.commit_file(repo, "own.txt", "第一段内容\n")
    publish(scenario, [first])
    probe.register_committed_work(repo, scenario["initial"])
    allocated = tracker.allocate_review_scope(repo=repo, session_id=event["session_id"], run_id="first-review",
        reviewer_id="test-reviewer", auto_claim_observed=False, committed_baseline=scenario["initial"],
        committed_base_ref="main", committed_scope_evidence=evidence(scenario))
    tracker.complete_review_scope(repo=repo, lease_id=allocated["lease_id"], scope_hash=allocated["scope_hash"], run_id="first-review")
    assert candidate_summary(scenario)["candidate_path_count"] == 0
    second = probe.commit_file(repo, "own.txt", "第一段内容\n新增未审内容\n")
    publish(scenario, [first, second])
    assert candidate_summary(scenario)["candidate_path_count"] == 1


def test_ambiguous_patch_id_does_not_guess_ownership(scenario):
    repo = scenario["repo"]
    own = probe.commit_file(repo, "own.txt", "相同补丁\n")
    publish(scenario, [own])
    probe.run_git(repo, "reset", "--hard", scenario["initial"])
    first = probe.commit_file(repo, "own.txt", "相同补丁\n")
    # 改变提交元数据，保证第一个候选也不是原 SHA。
    probe.run_git(repo, "commit", "--amend", "-q", "-m", "test: 重写身份")
    probe.run_git(repo, "revert", "--no-edit", "HEAD")
    second = probe.commit_file(repo, "own.txt", "相同补丁\n")
    assert first != second
    result = evidence(scenario)
    assert result["status"] == "unknown"
    assert "唯一映射" in result["diagnostic"]


def test_frozen_snapshot_keeps_scope_after_base_moves(scenario):
    from core.review_start_boundary.review_candidate_snapshot import freeze_review_candidate_snapshot
    from committed_candidate_scope import committed_base_exclusion_for_scope
    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "固定候选\n")
    publish(scenario, [own])
    current = candidate_summary(scenario)
    payload = {"token": "1234567890abcdef", "session_id": event["session_id"],
               "candidate_fingerprint": current["candidate_fingerprint"], "candidate_summary": current}
    def build(fixed):
        return candidate_summary(scenario, committed_scope_evidence=fixed["committed_scope_evidence"],
                                 committed_baseline_resolver=lambda *_: fixed["comparison_base_tip"],
                                 committed_base_ref_resolver=lambda *_: fixed["comparison_base_ref"],
                                 fixed_committed_round_base_tip=fixed["comparison_base_ref_tip"])
    frozen = freeze_review_candidate_snapshot(token_payload=payload, repo=str(repo), candidate_summary_builder=build)
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.commit_file(repo, "external.txt", "冻结后 main 新增\n")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    assert build(frozen)["candidate_fingerprint"] == current["candidate_fingerprint"]
    assert freeze_review_candidate_snapshot(token_payload=payload, repo=str(repo), candidate_summary_builder=build) == frozen
    from core.review_start_boundary.review_candidate_snapshot import verify_review_candidate_snapshot
    changed = json.loads(json.dumps(frozen))
    changed["committed_scope_evidence"]["task_owned_commit_shas"] = []
    with pytest.raises(ValueError, match="绑定|证据"):
        verify_review_candidate_snapshot(changed, token_payload=payload, repo=str(repo), candidate_summary_builder=lambda: build(changed))


def test_task_provenance_does_not_grant_new_session_ownership(scenario, tmp_path):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "另一个 session 的 task 工作\n")
    publish(scenario, [own])
    transcript = tmp_path / "new-session.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "new-session", "cwd": str(repo)}}) + "\n")
    scope_evidence = evidence(scenario)
    manifest = build_manifest(repo, transcript, tracker_enabled=False, committed_baseline=scenario["initial"],
                              committed_base_ref="main", committed_scope_evidence=scope_evidence)
    assert manifest["owned_committed_round_paths"] == []


def test_integrated_candidates_build_the_same_manifest_allocator_and_packet(scenario, tmp_path, monkeypatch):
    import prepare_review_run
    import review_start_boundary_gate as boundary_gate
    import rvf_agent_decision_cli as decision_cli
    import rvf_decision_token_store as tokens
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified, PENDING

    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "应进入正式 packet 的已提交未审内容\n")
    publish(scenario, [own])
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.capture_baseline(event)
    transcript = tmp_path / "original-write-session.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in [
        {"type": "session_meta", "payload": {"id": event["session_id"], "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "write-own",
          "input": "*** Begin Patch\n*** Add File: own.txt\n+应进入正式 packet 的已提交未审内容\n*** End Patch\n"}},
    ]) + "\n")
    event["transcript_path"] = str(transcript)
    current = candidate_summary(scenario)
    current["transcript_path"] = str(transcript)
    token = tokens.mint_decision_token(worktree=str(repo), session_id=event["session_id"],
        candidate_summary=current, candidate_fingerprint=current["candidate_fingerprint"]).token
    monkeypatch.setenv("CODEX_THREAD_ID", event["session_id"])
    def no_optional_native_boundary(*args, **kwargs):
        raise ReviewStartBoundaryNotVerified(PENDING, "隔离测试没有原生回退能力")
    monkeypatch.setattr(boundary_gate, "_read_or_reconcile_host_boundary_receipt", no_optional_native_boundary)
    scope_path = tmp_path / "allocated-tracker-scope.json"
    allocation = decision_cli.allocate_agentic_initiation_tracker_review_scope(
        repo=str(repo), session_id=event["session_id"], transcript_path=str(transcript), run_id="integrated-review",
        output_scope_path=scope_path, committed_baseline=current["committed_round_baseline"],
        committed_base_ref="main", committed_scope_evidence=current["committed_scope_evidence"],
    )
    assert allocation["status"] == "allocated", allocation
    scope = json.loads(scope_path.read_text())
    assert scope["paths"] == ["own.txt"]
    context = tmp_path / "approved-review-context.md"
    context.write_text("审查本会话已提交未审的 own.txt。\n")
    prepared = prepare_review_run.prepare_run(
        repo=repo, session_context=context, session_manifest=None, transcript=transcript,
        base_dir=tmp_path / "prepared-runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=[], background_files=[], exclude_path_prefixes=[], tracker_scope=scope_path,
        committed_baseline=current["committed_round_baseline"], committed_base_ref=current["committed_scope_evidence"]["base_tip_commit"],
        decision_token=token,
    )
    packet = Path(prepared["review_packet"]).read_text()
    assert "应进入正式 packet 的已提交未审内容" in packet
    contract = json.loads(Path(prepared["scope_contract"]).read_text())
    assert contract["review_candidate_snapshot"]["committed_scope_evidence"] == current["committed_scope_evidence"]


def test_manual_unknown_is_developer_only(scenario, monkeypatch):
    import rvf_user_prompt_submit as submit
    repo = scenario["repo"]
    own = probe.commit_file(repo, "own.txt", "待补证\n")
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    monkeypatch.setattr(summary_module, "_default_workspace_guard", lambda **_: None)
    result = submit.inspect_user_prompt_submit({**scenario["event"], "prompt": "$review-validate-fix:review-validate-fix"})
    assert result["status"] == "manual_decision_candidate_evidence_pending"
    assert "hookSpecificOutput" not in result
    assert result.get("decision") != "block"


def test_unknown_preserves_committed_records_and_allows_independent_dirty_work(scenario):
    repo, event = scenario["repo"], scenario["event"]
    probe.commit_file(repo, "own.txt", "不能因缺证据而 supersede\n")
    probe.register_committed_work(repo, scenario["initial"])
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.capture_baseline(event)
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    arguments = dict(repo=repo, session_id=event["session_id"], run_id="unknown-recovery", reviewer_id="test-reviewer",
                     auto_claim_observed=False, committed_baseline=probe.run_git(repo, "rev-parse", "HEAD"),
                     committed_base_ref="main", committed_scope_evidence=unknown)
    assert tracker.allocate_review_scope(**arguments)["status"] == "evidence_pending"
    (repo / "independent.txt").write_text("独立的未提交工作\n")
    tracker.register_claims(repo=repo, session_id=event["session_id"], run_id="independent-edit", worktree=repo,
        branch=None, owned_paths=["independent.txt"], apply_patch_paths={"independent.txt"}, exec_only_paths=set())
    allocated = tracker.allocate_review_scope(**arguments)
    assert allocated["status"] == "allocated"
    assert allocated["scope"]["paths"] == ["independent.txt"]


def test_previous_incarnation_highwater_cannot_expand_the_creation_window(scenario):
    repo, event = scenario["repo"], scenario["event"]
    new_initial = probe.commit_file(repo, "previous-incarnation.txt", "前一 incarnation 的工作\n")
    own = probe.commit_file(repo, "own.txt", "当前 incarnation\n")
    publish(scenario, [own], initial=new_initial)
    highwater.write_review_highwater(repo=str(repo), task_id=event["kanban_task_id"], session_id=event["session_id"],
                                    reviewed_head=scenario["initial"])
    result = candidate_summary(scenario)
    assert result["committed_round_baseline"] == new_initial
    assert [candidate["path"] for candidate in result["candidates"]] == ["own.txt"]


def test_unborn_head_keeps_independent_dirty_candidates(tmp_path, monkeypatch):
    with probe.isolated_runtime_environment(tmp_path):
        repo = tmp_path / "repository-without-first-commit"
        repo.mkdir()
        probe.run_git(repo, "init", "-q", "-b", "main")
        (repo / "first-file.txt").write_text("尚未首次提交的独立工作\n")
        event = probe.hook_event(repo)
        result = summary_module.build_candidate_summary(event=event, repo=str(repo), cwd=str(repo), workspace_guard=lambda **_: None)
        assert result["candidate_path_count"] == 1
        assert result["candidate_discovery_complete"] is True
        assert result["committed_scope_evidence"]["status"] == "unborn_head"


def test_identical_real_prompt_retries_unknown_evidence_once(scenario, monkeypatch):
    repo, event = scenario["repo"], scenario["event"]
    checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}", "-p", scenario["initial"], "-m", "test: 首轮")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    probe.commit_file(repo, "own.txt", "待补证\n")
    attempts = []
    monkeypatch.setattr(provenance, "_tracker_proven_commit_shas", lambda *args: attempts.append(1) or [])
    assert evidence(scenario)["status"] == "unknown"
    assert evidence(scenario)["status"] == "unknown"
    assert len(attempts) == 1
    # HEAD 和 prompt 文本都不变；真实 prompt 的 captured_at 仍能解除上次 unknown。
    probe.capture_baseline(event)
    assert evidence(scenario)["status"] == "unknown"
    assert evidence(scenario)["status"] == "unknown"
    assert len(attempts) == 2


def test_stop_without_any_window_seed_retains_unknown(scenario):
    import stop_review_validate_fix as stop_hook
    from core.run_ledger.run_ledger import start_run
    from core.session_scope_allocation.round_baseline_marker import clear_round_baseline_marker
    repo, event = scenario["repo"], scenario["event"]
    clear_round_baseline_marker(task_id=event["kanban_task_id"], session_id=event["session_id"], repo=repo)
    probe.run_git(repo, "branch", "-D", "main")
    ledger = start_run("isolated-unknown-window-stop", repo=str(repo), cwd=str(repo))
    decision = stop_hook.maybe_route_committed_round_scope(event, str(repo), ledger, str(repo))
    assert decision.reason_code == "committed_candidate_evidence_pending"
