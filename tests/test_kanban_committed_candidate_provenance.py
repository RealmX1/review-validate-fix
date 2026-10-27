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
    head = probe.run_git(repo, "rev-parse", "HEAD")
    base_tip = probe.run_git(repo, "rev-parse", base_ref)
    mapped_owned, _ = provenance._map_owned_commit_identities(repo, initial, creation["observedTaskCommits"])
    reachable_owned = [{"commitSha": sha, "stablePatchId": provenance._stable_patch_id(repo, sha)}
                       for sha in mapped_owned]
    entry = {"taskId": event["kanban_task_id"], "baseRef": base_ref,
             "incarnations": [creation], "lastWorkspaceGitSnapshot": {
                 "path": str(repo), "headCommit": head, "baseRefTipCommit": base_tip,
                 "commitsAheadOfBaseRef": int(probe.run_git(repo, "rev-list", "--count", f"{base_tip}..HEAD")),
                 "commitsBehindBaseRef": int(probe.run_git(repo, "rev-list", "--count", f"HEAD..{base_tip}")),
                 "taskCommitsIntegratedIntoBaseRef": sum(
                     provenance.commit_is_ancestor(repo, identity["commitSha"], base_tip) for identity in reachable_owned),
                 "taskOwnedNonMergeCommitsReachableFromLastKnownTaskHead": reachable_owned,
                 "isDetached": not bool(probe.run_git(repo, "branch", "--show-current")), "observedAt": 2,
             }}
    (directory / "task-commit-integration-provenance.json").write_text(json.dumps({"schemaVersion": 1, "tasks": {event["kanban_task_id"]: entry}}))
    (directory / "board.json").write_text(json.dumps({"columns": [{"cards": [{"id": event["kanban_task_id"], "baseRef": base_ref}]}]}))


def candidate_summary(scenario, **options):
    event = scenario["event"]
    return summary_module.build_candidate_summary(event=event, repo=event["cwd"], cwd=event["cwd"],
                                                 workspace_guard=lambda **_: None, **options)


def evidence(scenario):
    return candidate_summary(scenario)["committed_scope_evidence"]


def use_fresh_detached_task_worktree(scenario):
    base_checkout = scenario["repo"]
    linked_task_worktree = base_checkout.parent / "fast-forward-only-task-worktree"
    probe.run_git(base_checkout, "worktree", "add", "-q", "--detach", str(linked_task_worktree), scenario["initial"])
    scenario["repo"] = linked_task_worktree
    scenario["event"]["cwd"] = str(linked_task_worktree)
    task_id = scenario["event"]["kanban_task_id"]
    (scenario["directory"] / "sessions.json").write_text(json.dumps({task_id: {
        "taskId": task_id, "workspacePath": str(linked_task_worktree)}}))
    probe.capture_baseline(scenario["event"])
    return base_checkout


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
    base_checkout = repo
    if absorption == "ff":
        # 外部 base 提交必须在另一个 checkout 产生，真实 task HEAD 才能证明只有 FF 吸收。
        base_checkout = use_fresh_detached_task_worktree(scenario)
        repo = scenario["repo"]
    own = None if absorption == "ff" else probe.commit_file(repo, "own.txt", "本任务\n")
    publish(scenario, [own] if own else [])
    probe.run_git(base_checkout, "checkout", "-q", "main")
    probe.commit_file(base_checkout, "external.txt", "外部任务\n")
    if absorption != "ff":
        probe.run_git(repo, "checkout", "-q", "candidate-task")
    if absorption == "rebase":
        probe.run_git(repo, "rebase", "-q", "main")
    elif absorption == "ff":
        probe.run_git(repo, "merge", "-q", "--ff-only", "main")
    else:
        probe.run_git(repo, "merge", "-q", "main", "-m", "test: 吸收 main")
    if own:
        # 当前完整 snapshot 保留已观察的自有身份，外部 base 提交不会因此成为 task 所有。
        publish(scenario, [own])
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


def resolve_live_evidence_in_fresh_process(scenario):
    import subprocess

    command = (
        "import json, sys; from pathlib import Path; "
        "from adapters.cline_kanban.committed_candidate_provenance import resolve_kanban_committed_candidate_scope_evidence; "
        "event = json.loads(sys.argv[1]); "
        "print(json.dumps(resolve_kanban_committed_candidate_scope_evidence("
        "Path(event['cwd']), event['kanban_task_id'], event['session_id'], event=event)))"
    )
    return json.loads(subprocess.check_output(
        [sys.executable, "-c", command, json.dumps(scenario["event"])], cwd=ROOT, text=True,
    ))


@pytest.mark.parametrize("base_ref", ["main", "release"])
@pytest.mark.parametrize("fresh_process", [False, True])
def test_live_base_oid_change_refreshes_persisted_discovery_without_new_prompt(scenario, base_ref, fresh_process, monkeypatch):
    repo, event, directory = scenario["repo"], scenario["event"], scenario["directory"]
    if base_ref != "main":
        probe.run_git(repo, "branch", base_ref)
    external = probe.commit_file(repo, "external.txt", "稍后才进入 base 的外部提交\n")
    own = probe.commit_file(repo, "own.txt", "始终保留候选资格的 task 提交\n")
    publish(scenario, [own], base_ref=base_ref)
    event["hook_event_name"] = "Stop"
    before = candidate_summary(scenario)
    assert before["committed_scope_evidence"]["status"] == "verified"
    assert {candidate["path"] for candidate in before["candidates"]} == {"external.txt", "own.txt"}
    source_bytes = {path: path.read_bytes() for path in directory.iterdir()}
    probe.run_git(repo, "update-ref", f"refs/heads/{base_ref}", external)
    assert probe.run_git(repo, "rev-parse", "HEAD") == own
    assert {path: path.read_bytes() for path in directory.iterdir()} == source_bytes
    after = resolve_live_evidence_in_fresh_process(scenario) if fresh_process else evidence(scenario)
    assert after["base_tip_commit"] == external
    assert after["base_ref"] == base_ref
    assert after["source_observation_digest"] != before["committed_scope_evidence"]["source_observation_digest"]
    refreshed = candidate_summary(scenario)
    assert refreshed["candidate_discovery_complete"] is True
    assert [candidate["path"] for candidate in refreshed["candidates"]] == ["own.txt"]
    with monkeypatch.context() as patcher:
        patcher.setattr(provenance, "_map_owned_commit_identities", lambda *args: pytest.fail("base 不变时应复用新观察缓存"))
        assert evidence(scenario) == after


@pytest.mark.parametrize("base_ref", ["main", "release"])
def test_missing_live_base_recovers_from_persistent_unknown_when_ref_appears(scenario, base_ref, monkeypatch):
    repo, event, directory = scenario["repo"], scenario["event"], scenario["directory"]
    if base_ref != "main":
        probe.run_git(repo, "branch", base_ref)
    own = probe.commit_file(repo, "own.txt", "base 恢复后仍应审查的工作\n")
    publish(scenario, [own], base_ref=base_ref)
    probe.run_git(repo, "update-ref", "-d", f"refs/heads/{base_ref}")
    (repo / "independent.txt").write_text("base 缺失时仍可推进的独立 dirty 工作\n")
    tracker.register_claims(repo=repo, session_id=event["session_id"], run_id="missing-base-independent-edit", worktree=repo,
        branch=None, owned_paths=["independent.txt"], apply_patch_paths={"independent.txt"}, exec_only_paths=set())
    event["hook_event_name"] = "Stop"
    unavailable = candidate_summary(scenario)
    assert unavailable["committed_scope_evidence"]["status"] == "unknown"
    assert unavailable["candidate_discovery_complete"] is False
    assert "independent.txt" in {candidate["path"] for candidate in unavailable["candidates"]}
    # 摘要可能完成 tracker 的 dirty 观察；先让后续缓存证明建立在稳定输入上。
    unavailable_evidence = resolve_live_evidence_in_fresh_process(scenario)
    assert unavailable_evidence["status"] == "unknown"
    assert resolve_live_evidence_in_fresh_process(scenario) == unavailable_evidence
    source_bytes = {path: path.read_bytes() for path in directory.iterdir()}
    probe.run_git(repo, "update-ref", f"refs/heads/{base_ref}", own)
    assert {path: path.read_bytes() for path in directory.iterdir()} == source_bytes
    recovered = resolve_live_evidence_in_fresh_process(scenario)
    assert recovered["status"] == "verified"
    assert recovered["base_tip_commit"] == own
    assert recovered["source_observation_digest"] != unavailable_evidence["source_observation_digest"]
    assert recovered["task_owned_commit_shas"] == [own]
    with monkeypatch.context() as patcher:
        patcher.setattr(provenance, "_map_owned_commit_identities", lambda *args: pytest.fail("恢复后来源不变时应复用缓存"))
        assert evidence(scenario) == recovered
    assert {candidate["path"] for candidate in candidate_summary(scenario)["candidates"]} == {"own.txt", "independent.txt"}


def test_frozen_scope_keeps_old_base_oid_while_live_discovery_refreshes(scenario, monkeypatch):
    from core.review_start_boundary.review_candidate_snapshot import freeze_review_candidate_snapshot

    repo, event = scenario["repo"], scenario["event"]
    external = probe.commit_file(repo, "external.txt", "冻结后才进入 main 的提交\n")
    own = probe.commit_file(repo, "own.txt", "本任务提交\n")
    publish(scenario, [own])
    event["hook_event_name"] = "Stop"
    current = candidate_summary(scenario)
    token_payload = {"token": "fedcba9876543210", "session_id": event["session_id"],
                     "candidate_fingerprint": current["candidate_fingerprint"], "candidate_summary": current}

    def build_frozen_candidate_summary(snapshot):
        return candidate_summary(scenario, committed_scope_evidence=snapshot["committed_scope_evidence"],
            committed_baseline_resolver=lambda *_: snapshot["comparison_base_tip"],
            committed_base_ref_resolver=lambda *_: snapshot["comparison_base_ref"],
            fixed_committed_round_base_tip=snapshot["comparison_base_ref_tip"])

    frozen = freeze_review_candidate_snapshot(token_payload=token_payload, repo=str(repo),
                                              candidate_summary_builder=build_frozen_candidate_summary)
    probe.run_git(repo, "update-ref", "refs/heads/main", external)
    live = candidate_summary(scenario)
    assert live["committed_scope_evidence"]["base_tip_commit"] == external
    assert [candidate["path"] for candidate in live["candidates"]] == ["own.txt"]
    with monkeypatch.context() as patcher:
        patcher.setattr(provenance, "resolve_kanban_committed_candidate_scope_evidence", lambda *args, **kwargs: pytest.fail("冻结 scope 不应重新观察 live base"))
        frozen_result = build_frozen_candidate_summary(frozen)
        assert frozen_result["committed_scope_evidence"]["base_tip_commit"] == scenario["initial"]
        assert {candidate["path"] for candidate in frozen_result["candidates"]} == {"external.txt", "own.txt"}
        assert frozen_result["candidate_fingerprint"] == current["candidate_fingerprint"]
        assert freeze_review_candidate_snapshot(token_payload=token_payload, repo=str(repo),
            candidate_summary_builder=build_frozen_candidate_summary) == frozen


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


@pytest.mark.parametrize("previously_observed", [False, True])
@pytest.mark.parametrize("creation_checkpoint", [False, True])
def test_stale_provenance_recovers_omitted_integrated_commit(scenario, previously_observed, creation_checkpoint):
    repo, event = scenario["repo"], scenario["event"]
    if creation_checkpoint:
        checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}",
                                   "-p", scenario["initial"], "-m", "test: 创建点 checkpoint")
        encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
        probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    previous = probe.commit_file(repo, "previous.txt", "此前已观察的 task 工作\n") if previously_observed else None
    publish(scenario, [previous] if previous else [])
    before_own = probe.run_git(repo, "rev-parse", "HEAD")
    own = probe.commit_file(repo, "own.txt", "旧 snapshot 未观察到的本会话工作\n")
    probe.register_committed_work(repo, before_own)
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.capture_baseline(event)
    result = candidate_summary(scenario)
    assert result["committed_scope_evidence"]["status"] == "verified"
    assert set(result["committed_scope_evidence"]["task_owned_commit_shas"]) == {own, *([previous] if previous else [])}
    assert any(candidate["path"] == "own.txt" for candidate in result["candidates"])
    assert result["candidate_discovery_complete"] is True


@pytest.mark.parametrize("observation_damage", ["stale_snapshot", "missing_snapshot", "missing_snapshot_head", "incomplete_identities", "empty_current_snapshot"])
def test_incomplete_observation_remains_unknown_until_tracker_changes(scenario, observation_damage, monkeypatch):
    repo, event, directory = scenario["repo"], scenario["event"], scenario["directory"]
    publish(scenario)
    own = probe.commit_file(repo, "own.txt", "尚未取得独立归属证据\n")
    if observation_damage != "stale_snapshot":
        publish(scenario, [] if observation_damage == "empty_current_snapshot" else [own])
        path = directory / "task-commit-integration-provenance.json"
        payload = json.loads(path.read_text())
        entry = payload["tasks"][event["kanban_task_id"]]
        entry["incarnations"][0]["observedTaskCommits"] = []
        if observation_damage == "missing_snapshot":
            entry["lastWorkspaceGitSnapshot"] = None
        elif observation_damage == "missing_snapshot_head":
            del entry["lastWorkspaceGitSnapshot"]["headCommit"]
        path.write_text(json.dumps(payload))
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.capture_baseline(event)
    first = candidate_summary(scenario)
    assert first["committed_scope_evidence"]["status"] == "unknown"
    assert first["candidate_discovery_complete"] is False
    with monkeypatch.context() as patcher:
        patcher.setattr(provenance, "_tracker_proven_commit_shas", lambda *args: pytest.fail("输入未变时不能反复补证"))
        assert evidence(scenario) == first["committed_scope_evidence"]
    probe.register_committed_work(repo, scenario["initial"])
    recovered = candidate_summary(scenario)
    assert recovered["committed_scope_evidence"]["status"] == "verified"
    assert recovered["committed_scope_evidence"]["task_owned_commit_shas"] == [own]
    assert recovered["candidate_discovery_complete"] is True
    assert [candidate["path"] for candidate in recovered["candidates"]] == ["own.txt"]


@pytest.mark.parametrize("checkpoint_damage", ["completed_turn_head", "missing_creation_reflog", "truncated_creation_reflog"])
def test_first_completed_turn_checkpoint_does_not_replace_creation_anchor(scenario, checkpoint_damage):
    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "首轮结束前已提交的工作\n")
    probe.register_committed_work(repo, scenario["initial"])
    parent = own if checkpoint_damage == "completed_turn_head" else scenario["initial"]
    checkpoint = probe.run_git(repo, "commit-tree", own + "^{tree}", "-p", parent,
                               "-m", "kanban checkpoint task:" + event["kanban_task_id"] + " turn:1")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    head_reflog = Path(probe.run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "logs/HEAD"))
    if checkpoint_damage == "missing_creation_reflog":
        head_reflog.unlink()
    elif checkpoint_damage == "truncated_creation_reflog":
        head_reflog.write_text("\n".join(head_reflog.read_text().splitlines()[1:]) + "\n")
    result = candidate_summary(scenario)
    assert result["committed_scope_evidence"]["status"] == "unknown"
    assert result["candidate_discovery_complete"] is False
    publish(scenario, [own])
    restored = evidence(scenario)
    assert restored["status"] == "verified"
    assert restored["initial_base_commit"] == scenario["initial"]
    assert restored["task_owned_commit_shas"] == [own]


def test_incomplete_provenance_does_not_use_another_sessions_strong_claim(scenario):
    repo = scenario["repo"]
    publish(scenario)
    probe.commit_file(repo, "own.txt", "另一个 session 的强归属\n")
    probe.register_committed_work(repo, scenario["initial"], owner="another-session")
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    result = candidate_summary(scenario)
    assert result["committed_scope_evidence"]["status"] == "unknown"
    assert result["committed_scope_evidence"]["task_owned_commit_shas"] == []
    assert result["candidate_discovery_complete"] is False


@pytest.mark.parametrize("proof_damage", ["target_outside_base", "missing_reflog", "truncated_reflog"])
def test_zero_owned_history_requires_complete_base_absorption_proof(scenario, proof_damage):
    base_checkout = use_fresh_detached_task_worktree(scenario)
    repo = scenario["repo"]
    publish(scenario)
    if proof_damage != "target_outside_base":
        probe.run_git(base_checkout, "checkout", "-q", "main")
    target = probe.commit_file(base_checkout, "external.txt", "外部 ref 上的提交\n")
    probe.run_git(repo, "merge", "-q", "--ff-only", target)
    head_reflog = Path(probe.run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "logs/HEAD"))
    complete_reflog = head_reflog.read_text()
    if proof_damage == "missing_reflog":
        head_reflog.unlink()
    elif proof_damage == "truncated_reflog":
        head_reflog.write_text("\n".join(complete_reflog.splitlines()[1:]) + "\n")
    result = candidate_summary(scenario)
    assert result["committed_scope_evidence"]["status"] == "unknown"
    assert result["candidate_discovery_complete"] is False
    if proof_damage != "target_outside_base":
        head_reflog.write_text(complete_reflog)
        recovered = candidate_summary(scenario)
        assert recovered["committed_scope_evidence"]["status"] == "verified"
        assert recovered["committed_scope_evidence"]["task_owned_commit_shas"] == []
        assert recovered["candidate_discovery_complete"] is True


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


@pytest.mark.parametrize("stale_provenance", [False, True])
def test_checkpoint_and_original_native_commit_receipt_restore_provenance(scenario, tmp_path, stale_provenance):
    repo, event = scenario["repo"], scenario["event"]
    checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}", "-p", scenario["initial"], "-m", "test: 首轮")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    if stale_provenance:
        publish(scenario)
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


@pytest.mark.parametrize("owned_commit_count,external_line_index,dirty_same_path", [(1, 50, False), (2, 8, False), (2, 50, True)])
def test_same_path_external_base_hunk_is_excluded_from_every_committed_consumer(
    scenario, tmp_path, owned_commit_count, external_line_index, dirty_same_path,
):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    import build_review_packet

    repo, event = scenario["repo"], scenario["event"]
    original_lines = [f"原始第 {index} 行\n" for index in range(1, 71)]
    initial = probe.commit_file(repo, "shared.txt", "".join(original_lines))
    scenario["initial"] = initial
    probe.run_git(repo, "branch", "-f", "main", initial)
    owned_lines = list(original_lines)
    owned_lines[2] = "本 task 应审修改\n"
    own = probe.commit_file(repo, "shared.txt", "".join(owned_lines))
    owned_commits = [own]
    if owned_commit_count == 2:
        owned_lines[3] = "本 task 累计的第二处应审修改\n"
        owned_commits.append(probe.commit_file(repo, "shared.txt", "".join(owned_lines)))
    publish(scenario, owned_commits)
    owned_before_absorption = tracker._classify_committed_path(repo, "shared.txt", initial)
    original_unit_ids = {spec.unit_id for spec in tracker._specs_from_observation(owned_before_absorption, "shared.txt")}
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    external_lines = list(owned_lines)
    external_lines[external_line_index] = "外部 base 不应认领的修改\n"
    probe.commit_file(repo, "shared.txt", "".join(external_lines))
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.run_git(repo, "merge", "-q", "--ff-only", "main")
    if dirty_same_path:
        external_lines[29] = "本 session 仍未提交的同路径修改\n"
        (repo / "shared.txt").write_text("".join(external_lines))
        original_unit_ids.update(spec.unit_id for spec in tracker._specs_from_observation(tracker._classify_path(repo, "shared.txt"), "shared.txt"))
    # 当前快照覆盖吸收后的 HEAD；外部提交仍不进入自有集合。
    publish(scenario, owned_commits)
    transcript = tmp_path / "original-owned-shared-path-session.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in [
        {"type": "session_meta", "payload": {"id": event["session_id"], "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "write-shared",
          "input": "*** Begin Patch\n*** Update File: shared.txt\n@@\n-原始第 3 行\n+本 task 应审修改\n*** End Patch\n"}},
    ]) + "\n")
    event["transcript_path"] = str(transcript)
    current = candidate_summary(scenario)
    assert current["candidate_discovery_complete"] is True
    assert current["committed_scope_evidence"]["status"] == "verified"
    assert set(current["committed_scope_evidence"]["task_owned_commit_shas"]) == set(owned_commits)
    manifest = build_manifest(repo, transcript, committed_baseline=initial, committed_base_ref="main",
                              committed_scope_evidence=current["committed_scope_evidence"])
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id=event["session_id"], run_id="same-path-projection", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=initial, committed_base_ref="main",
        committed_scope_evidence=current["committed_scope_evidence"],
    )
    assert allocation["status"] == "allocated", allocation
    assert set(allocation["scope"]["unit_ids"]) == original_unit_ids
    assert manifest["owned_committed_round_paths"] == ["shared.txt"]
    candidate = next(item for item in current["candidates"] if item["path"] == "shared.txt")
    assert candidate["nonblank_added_lines"] == owned_commit_count + int(dirty_same_path)
    assert candidate["nonblank_deleted_lines"] == owned_commit_count + int(dirty_same_path)
    manifest["tracker"]["tracker_scope"] = allocation["scope"]
    manifest_path = tmp_path / "projected-session-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    context = tmp_path / "same-path-review-context.md"
    context.write_text("审查本 session 修改 shared.txt 的内容。\n")
    packet, _ = build_review_packet.build_packet(
        repo=repo, session_context=context, max_file_bytes=10000,
        primary_files=[], background_files=[], exclude_prefixes=[], session_manifest_path=manifest_path,
    )
    allocated_diff = packet.split("```diff", 1)[1].split("```", 1)[0]
    assert "+本 task 应审修改" in allocated_diff
    assert "外部 base 不应认领的修改" not in allocated_diff
    if dirty_same_path:
        assert "+本 session 仍未提交的同路径修改" in packet
    probe.run_git(repo, "add", "--", "shared.txt")
    frozen_tree = probe.run_git(repo, "write-tree")
    frozen_commit = probe.run_git(repo, "commit-tree", frozen_tree, "-p", "HEAD", "-m", "test: 冻结已分配 packet")
    probe.run_git(repo, "reset", "-q", "HEAD", "--", "shared.txt")
    if dirty_same_path:
        external_lines[29] = "冻结后的新改动不得进入旧 packet\n"
        (repo / "shared.txt").write_text("".join(external_lines))
    frozen_packet, _ = build_review_packet.build_packet(
        repo=repo, session_context=context, max_file_bytes=10000,
        primary_files=[], background_files=[], exclude_prefixes=[], session_manifest_path=manifest_path,
        frozen_candidate_commit=frozen_commit,
    )
    frozen_allocated_diff = frozen_packet.split("```diff", 1)[1].split("```", 1)[0]
    assert "+本 task 应审修改" in frozen_allocated_diff
    assert "外部 base 不应认领的修改" not in frozen_allocated_diff
    assert "冻结后的新改动不得进入旧 packet" not in frozen_packet
    if dirty_same_path:
        assert "+本 session 仍未提交的同路径修改" in frozen_allocated_diff
    tracker.complete_review_scope(repo=repo, lease_id=allocation["lease_id"], scope_hash=allocation["scope_hash"], run_id="same-path-projection")
    assert candidate_summary(scenario)["candidate_path_count"] == int(dirty_same_path)


def test_overlapping_external_base_projection_remains_unknown_and_keeps_independent_dirty_work(scenario, tmp_path):
    from committed_candidate_scope import resolve_committed_candidate_scope
    from core.session_scope_allocation.session_change_manifest import build_manifest
    import stop_review_validate_fix as stop_hook
    from core.run_ledger.run_ledger import start_run

    repo, event = scenario["repo"], scenario["event"]
    original = [f"共享第 {index} 行\n" for index in range(1, 71)]
    initial = probe.commit_file(repo, "shared.txt", "".join(original))
    scenario["initial"] = initial
    probe.run_git(repo, "branch", "-f", "main", initial)
    owned = list(original)
    owned[2] = "本 task 的原始修改\n"
    own = probe.commit_file(repo, "shared.txt", "".join(owned))
    publish(scenario, [own])
    tracker.register_claims(repo=repo, session_id=event["session_id"], run_id="before-overlap", worktree=repo,
        branch=None, owned_paths=["shared.txt"], apply_patch_paths={"shared.txt"}, exec_only_paths=set(),
        committed_paths={"shared.txt"}, committed_baseline=initial, committed_base_ref="main",
        committed_scope_evidence=evidence(scenario))
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    overlapping = list(owned)
    overlapping[2] = "外部 base 覆写了相同位置\n"
    probe.commit_file(repo, "shared.txt", "".join(overlapping))
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.run_git(repo, "merge", "-q", "--ff-only", "main")
    publish(scenario, [own])
    _, _, verified = resolve_committed_candidate_scope(repo, event["kanban_task_id"], event["session_id"], event=event)
    assert verified["status"] == "verified"
    current = candidate_summary(scenario)
    assert current["candidate_discovery_complete"] is False
    assert current["committed_scope_evidence"]["status"] == "unknown"
    arguments = dict(repo=repo, session_id=event["session_id"], run_id="overlapping-projection", reviewer_id="test-reviewer",
                     auto_claim_observed=False, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=verified)
    assert tracker.allocate_review_scope(**arguments)["status"] == "evidence_pending"
    ledger = start_run("overlapping-projection-stop", repo=str(repo), cwd=str(repo))
    assert stop_hook.maybe_route_committed_round_scope(event, str(repo), ledger, str(repo)).reason_code == "committed_candidate_evidence_pending"
    (repo / "independent.txt").write_text("独立 dirty 修改仍可审\n")
    transcript = tmp_path / "independent-dirty-during-overlap.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in [
        {"type": "session_meta", "payload": {"id": event["session_id"], "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "write-independent",
          "input": "*** Begin Patch\n*** Add File: independent.txt\n+独立 dirty 修改仍可审\n*** End Patch\n"}},
    ]) + "\n")
    manifest = build_manifest(repo, transcript, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=verified)
    assert manifest["committed_scope_evidence"]["status"] == "unknown"
    assert manifest["owned_dirty_paths"] == ["independent.txt"]
    allocation = tracker.allocate_review_scope(**arguments)
    assert allocation["status"] == "allocated"
    assert allocation["scope"]["paths"] == ["independent.txt"]
    recovered = probe.commit_file(repo, "shared.txt", "".join(owned))
    publish(scenario, [own, recovered])
    assert candidate_summary(scenario)["candidate_discovery_complete"] is True


def test_reviewed_committed_hunk_survives_base_absorption_and_new_owned_hunk_is_allocated(scenario):
    repo, event = scenario["repo"], scenario["event"]
    content = [f"共享原始第 {index} 行\n" for index in range(1, 71)]
    initial = probe.commit_file(repo, "shared.txt", "".join(content))
    scenario["initial"] = initial
    probe.run_git(repo, "branch", "-f", "main", initial)
    content[2] = "第一处已审自有修改\n"
    first = probe.commit_file(repo, "shared.txt", "".join(content))
    publish(scenario, [first])
    def register_and_allocate(run_id):
        scope_evidence = evidence(scenario)
        tracker.register_claims(repo=repo, session_id=event["session_id"], run_id=run_id, worktree=repo,
            branch=None, owned_paths=["shared.txt"], apply_patch_paths={"shared.txt"}, exec_only_paths=set(),
            committed_paths={"shared.txt"}, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=scope_evidence)
        return tracker.allocate_review_scope(repo=repo, session_id=event["session_id"], run_id=run_id, reviewer_id="test-reviewer",
            auto_claim_observed=False, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=scope_evidence)
    first_scope = register_and_allocate("first-owned-hunk-review")
    tracker.complete_review_scope(repo=repo, lease_id=first_scope["lease_id"], scope_hash=first_scope["scope_hash"], run_id="first-owned-hunk-review")
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    content[50] = "外部 base 修改\n"
    probe.commit_file(repo, "shared.txt", "".join(content))
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.run_git(repo, "merge", "-q", "--ff-only", "main")
    publish(scenario, [first])
    reviewed_summary = candidate_summary(scenario)
    assert reviewed_summary["candidate_discovery_complete"] is True
    assert reviewed_summary["committed_scope_evidence"]["status"] == "verified"
    assert reviewed_summary["candidate_path_count"] == 0
    content[29] = "第二处新增自有修改\n"
    second = probe.commit_file(repo, "shared.txt", "".join(content))
    content[59] = "同路径明确跳过的修改\n"
    probe.commit_file(repo, "shared.txt", "".join(content))
    probe.run_git(repo, "commit", "--amend", "-q", "-m", "test: 略过同路径 hunk\n\nRVF-Skip-Review: 隔离控制")
    skipped = probe.run_git(repo, "rev-parse", "HEAD")
    publish(scenario, [first, second, skipped])
    second_scope = register_and_allocate("second-owned-hunk-review")
    assert second_scope["status"] == "allocated"
    assert len(second_scope["scope"]["unit_ids"]) == 1
    assert set(second_scope["scope"]["unit_ids"]).isdisjoint(first_scope["scope"]["unit_ids"])
    assert second_scope["scope"]["hunks"][0]["hunk_header"].startswith("@@ -27,")


@pytest.mark.parametrize("path_change", ["added_text", "added_empty", "deleted", "added_binary"])
def test_committed_projection_preserves_whole_path_unit_identity(scenario, path_change):
    repo = scenario["repo"]
    if path_change == "deleted":
        initial = probe.commit_file(repo, "own.txt", "删除前内容\n")
        scenario["initial"] = initial
        probe.run_git(repo, "branch", "-f", "main", initial)
        (repo / "own.txt").unlink()
    elif path_change == "added_binary":
        (repo / "own.txt").write_bytes(b"\0\x01\x02")
    else:
        (repo / "own.txt").write_text("自有新增内容\n" if path_change == "added_text" else "")
    probe.run_git(repo, "add", "--", "own.txt")
    probe.run_git(repo, "commit", "-q", "-m", "test: 整路径变更")
    own = probe.run_git(repo, "rev-parse", "HEAD")
    expected = {spec.unit_id for spec in tracker._specs_from_observation(
        tracker._classify_committed_path(repo, "own.txt", scenario["initial"]), "own.txt")}
    publish(scenario, [own])
    probe.run_git(repo, "checkout", "-q", "main")
    probe.run_git(repo, "merge", "-q", "--ff-only", "candidate-task")
    probe.commit_file(repo, "external.txt", "外部独立路径\n")
    probe.run_git(repo, "checkout", "-q", "candidate-task")
    probe.run_git(repo, "merge", "-q", "--ff-only", "main")
    publish(scenario, [own])
    current = candidate_summary(scenario)
    assert current["candidate_discovery_complete"] is True
    assert current["committed_scope_evidence"]["status"] == "verified"
    assert current["committed_scope_evidence"]["task_owned_commit_shas"] == [own]
    projected = tracker._classify_committed_path(repo, "own.txt", scenario["initial"], committed_base_ref="main", committed_scope_evidence=current["committed_scope_evidence"])
    assert {spec.unit_id for spec in tracker._specs_from_observation(projected, "own.txt")} == expected
    assert [item["path"] for item in current["candidates"]] == ["own.txt"]


@pytest.mark.parametrize("tracker_enabled", [False, True])
def test_unknown_committed_scope_does_not_grant_a_metadata_only_session_ownership(scenario, tmp_path, tracker_enabled):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo = scenario["repo"]
    probe.commit_file(repo, "own.txt", "旧 session 已提交但尚未审查的内容\n")
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    transcript = tmp_path / "metadata-only-new-session.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "metadata-only-new-session", "cwd": str(repo)}}) + "\n")
    manifest = build_manifest(repo, transcript, tracker_enabled=tracker_enabled, committed_baseline=scenario["initial"],
                              committed_base_ref="main", committed_scope_evidence=unknown)
    assert manifest["owned_committed_round_paths"] == []
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id="metadata-only-new-session", run_id="unknown-new-session", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown,
    )
    assert allocation["status"] == "evidence_pending"


@pytest.mark.parametrize("claim_evidence,claiming_session,carry_prior_scope,expected_status", [
    ("committed_round_git", "unknown-allocation-session", False, "evidence_pending"),
    ("kanban_task_worktree", "unknown-allocation-session", False, "evidence_pending"),
    ("apply_patch", "another-session", True, "evidence_pending"),
    ("apply_patch", "unknown-allocation-session", False, "allocated"),
])
def test_unknown_committed_allocation_requires_current_session_proof(
    scenario, claim_evidence, claiming_session, carry_prior_scope, expected_status,
):
    repo = scenario["repo"]
    probe.commit_file(repo, "own.txt", "独立可观测的已提交内容\n")
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    registration = tracker.register_claims(
        repo=repo, session_id=claiming_session, run_id="before-unknown-allocation", worktree=repo, branch=None,
        owned_paths=["own.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(tracker.OwnedUnit(path="own.txt", unit="path"), claim_evidence)],
        committed_paths={"own.txt"}, committed_baseline=scenario["initial"],
    )
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id="unknown-allocation-session", run_id="unknown-session-proof", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown,
        union_with_prior_scope_unit_ids=registration.claim_ids if carry_prior_scope else None,
    )
    assert allocation["status"] == expected_status
    if expected_status == "allocated":
        assert allocation["scope"]["paths"] == ["own.txt"]


@pytest.mark.parametrize("proof_evidence", ["apply_patch", "agent_explicit_declaration", "agent_explicit_claim"])
def test_unknown_committed_scope_retains_precise_current_session_proof_across_refreshes(scenario, tmp_path, proof_evidence):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo = scenario["repo"]
    probe.commit_file(repo, "own.txt", "本 session 已证明的独立提交\n")
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    tracker.register_claims(
        repo=repo, session_id="proven-current-session", run_id="proven-write", worktree=repo, branch=None,
        owned_paths=["own.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(tracker.OwnedUnit(path="own.txt", unit="path"), proof_evidence)],
        committed_paths={"own.txt"}, committed_baseline=scenario["initial"],
    )
    transcript = tmp_path / "current-session-with-persisted-proof.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "proven-current-session", "cwd": str(repo)}}) + "\n")
    for _ in range(2):
        manifest = build_manifest(repo, transcript, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown)
        assert manifest["owned_committed_round_paths"] == ["own.txt"]
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id="proven-current-session", run_id="proven-unknown-allocation", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown,
    )
    assert allocation["status"] == "allocated"
    assert allocation["scope"]["paths"] == ["own.txt"]


def test_unknown_old_commits_do_not_block_current_session_dirty_tool_proof(scenario, tmp_path):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo = scenario["repo"]
    probe.commit_file(repo, "own.txt", "来自其他 session 的旧提交\n")
    (repo / "independent.txt").write_text("当前 session 的独立 dirty 修改\n")
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    transcript = tmp_path / "new-session-explicit-dirty-write.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in [
        {"type": "session_meta", "payload": {"id": "current-dirty-session", "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "current-dirty-write",
          "input": "*** Begin Patch\n*** Add File: independent.txt\n+当前 session 的独立 dirty 修改\n*** End Patch\n"}},
    ]) + "\n")
    manifest = build_manifest(repo, transcript, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown)
    assert manifest["owned_committed_round_paths"] == []
    assert manifest["owned_dirty_paths"] == ["independent.txt"]
    allocation = tracker.allocate_review_scope(
        repo=repo, session_id="current-dirty-session", run_id="dirty-only-unknown-allocation", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown,
    )
    assert allocation["status"] == "allocated"
    assert allocation["scope"]["paths"] == ["independent.txt"]


def test_unknown_same_path_strong_proof_does_not_absorb_unproven_new_hunks(scenario, tmp_path):
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo = scenario["repo"]
    content = [f"原始共享行 {index}\n" for index in range(1, 71)]
    initial = probe.commit_file(repo, "shared.txt", "".join(content))
    scenario["initial"] = initial
    probe.run_git(repo, "branch", "-f", "main", initial)
    content[2] = "本 session 有强证据的旧 hunk\n"
    probe.commit_file(repo, "shared.txt", "".join(content))
    original_proof = tracker.register_claims(
        repo=repo, session_id="precise-hunk-session", run_id="original-strong-proof", worktree=repo, branch=None,
        owned_paths=["shared.txt"], apply_patch_paths={"shared.txt"}, exec_only_paths=set(),
        committed_paths={"shared.txt"}, committed_baseline=initial,
    )
    content[50] = "同路径另一 session 新增且未经证明的 hunk\n"
    unproven_commit = probe.commit_file(repo, "shared.txt", "".join(content))
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    transcript = tmp_path / "metadata-after-unproven-shared-path-hunk.jsonl"
    session_metadata = {"type": "session_meta", "payload": {"id": "precise-hunk-session", "cwd": str(repo)}}
    transcript.write_text(json.dumps(session_metadata) + "\n")
    manifest = build_manifest(repo, transcript, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=unknown)
    assert manifest["owned_committed_round_paths"] == []
    allocation_arguments = dict(repo=repo, session_id="precise-hunk-session", run_id="unproven-shared-path", reviewer_id="test-reviewer",
        auto_claim_observed=False, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=unknown)
    assert tracker.allocate_review_scope(**allocation_arguments)["status"] == "evidence_pending"
    assert set(tracker.strong_session_owned_unit_paths(repo=repo, session_id="precise-hunk-session")) == set(original_proof.claim_ids)
    (repo / "independent.txt").write_text("混合路径暂缓时的独立 dirty 工作\n")
    transcript.write_text("\n".join(json.dumps(record) for record in [session_metadata,
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "independent-while-pending",
          "input": "*** Begin Patch\n*** Add File: independent.txt\n+混合路径暂缓时的独立 dirty 工作\n*** End Patch\n"}},
    ]) + "\n")
    build_manifest(repo, transcript, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=unknown)
    independent_allocation = tracker.allocate_review_scope(**allocation_arguments)
    assert independent_allocation["scope"]["paths"] == ["independent.txt"]
    assert set(original_proof.claim_ids) <= set(tracker.strong_session_owned_unit_paths(repo=repo, session_id="precise-hunk-session"))
    probe.run_git(repo, "revert", "--no-edit", unproven_commit)
    recovered_unknown = evidence(scenario)
    assert recovered_unknown["status"] == "unknown"
    manifest = build_manifest(repo, transcript, committed_baseline=initial, committed_base_ref="main", committed_scope_evidence=recovered_unknown)
    assert manifest["owned_committed_round_paths"] == ["shared.txt"]
    recovered = tracker.allocate_review_scope(**{**allocation_arguments, "committed_scope_evidence": recovered_unknown})
    assert recovered["scope"]["paths"] == ["shared.txt"]


def test_existing_session_proof_survives_manifest_refresh_and_process_restart_recovery(scenario, tmp_path):
    import subprocess
    import sqlite3
    from core.session_scope_allocation.session_change_manifest import build_manifest
    repo, event = scenario["repo"], scenario["event"]
    own = probe.commit_file(repo, "own.txt", "重启后仍应能用 tracker 证明的原始写入\n")
    probe.register_committed_work(repo, scenario["initial"])
    unknown = evidence(scenario)
    assert unknown["status"] == "unknown"
    transcript = tmp_path / "metadata-refresh-preserving-original-evidence.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": event["session_id"], "cwd": str(repo)}}) + "\n")
    for _ in range(2):
        manifest = build_manifest(repo, transcript, committed_baseline=scenario["initial"], committed_base_ref="main", committed_scope_evidence=unknown)
        assert manifest["owned_committed_round_paths"] == ["own.txt"]
    database = tracker.tracker_dir(tracker.log_root(), tracker.repo_key(tracker.git_common_dir(repo))) / tracker.SQLITE_FILENAME
    with sqlite3.connect(database) as connection:
        labels = {row[0] for row in connection.execute("SELECT evidence FROM session_units WHERE session_id=?", (event["session_id"],))}
    assert labels == {"apply_patch"}
    assert provenance._tracker_proven_commit_shas(repo, scenario["initial"], event["session_id"]) == [own]
    checkpoint = probe.run_git(repo, "commit-tree", scenario["initial"] + "^{tree}", "-p", scenario["initial"], "-m", "test: 补齐创建 checkpoint")
    encoded = base64.urlsafe_b64encode(event["kanban_task_id"].encode()).decode().rstrip("=")
    probe.run_git(repo, "update-ref", f"refs/kanban/checkpoints/{encoded}/turn/1", checkpoint)
    fresh_process = subprocess.run([
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from adapters.cline_kanban.committed_candidate_provenance import resolve_kanban_committed_candidate_scope_evidence; "
        "print(json.dumps(resolve_kanban_committed_candidate_scope_evidence(Path(sys.argv[2]),sys.argv[3],sys.argv[4])))",
        str(ROOT), str(repo), event["kanban_task_id"], event["session_id"],
    ], cwd=ROOT, capture_output=True, text=True, check=True)
    recovered = json.loads(fresh_process.stdout)
    assert recovered["status"] == "verified"
    assert recovered["task_owned_commit_shas"] == [own]


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
