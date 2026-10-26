"""备用交付与 Stop 在途／pending 对账的真实 Git 回归；不启动 reviewer 或真实投递。"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from test_independent_backup_route import SCRIPT, record_candidate_coverage_beyond_first_recovery_page, record_complete_delivery_fixture, route


@pytest.fixture
def stop_followup(route, tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name)
    state = tmp_path / "isolated-stop-state"
    monkeypatch.setenv("RVF_HOME", str(state / "rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(state / "user-local-data"))
    monkeypatch.setenv("CODEX_RVF_STATE_DIR", str(state))
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(state / "followup-markers"))
    monkeypatch.setenv("KANBAN_TASK_ID", "backup-stop-task")
    monkeypatch.setenv("KANBAN_PROJECT_PATH", str(route[1]))
    monkeypatch.setenv("KANBAN_WORKSPACE_PATH", str(route[1]))
    monkeypatch.setenv("RVF_DECISION_POINT_DELIVERY_TRANSPORT", "stop_hook_self_injection")
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("rvf_stop_backup_followup_tests", SCRIPT.with_name("stop_review_validate_fix.py"))
    hook = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, hook)
    spec.loader.exec_module(hook)
    import kanban_followup_lock as locks
    event = {"cwd": str(route[1]), "session_id": "backup-stop-session", "last_user_message": "继续完成已授权工作。"}
    ledger = hook.start_stop_hook_ledger(event)
    return hook, locks, event, ledger


def arm_in_progress(stop_followup, repo, run_dir, *, task_id="backup-stop-task", stale=False):
    hook, locks, _, _ = stop_followup
    run_dir.mkdir(parents=True, exist_ok=True)
    path = locks.write_marker(task_id=task_id, session_id="backup-stop-session", run_id=run_dir.name,
                              run_dir=str(run_dir), repo=str(repo), cwd=str(repo), state=locks.STATE_IN_PROGRESS)
    if stale:
        value = json.loads(path.read_text())
        value["expires_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(value))
    return path


def arm_pending(stop_followup, repo, run_dir, *, task_id="backup-stop-task"):
    _, locks, _, _ = stop_followup
    return locks.write_pending_marker(task_id=task_id, session_id="backup-stop-session", run_id=run_dir.name,
                                      run_dir=str(run_dir), token="original-dispatch-token", repo=str(repo), cwd=str(repo),
                                      kanban_project_path=str(repo), idempotency_key="original-dispatch-key", delivery_channel="terminal")


def ledger_events(ledger):
    return [json.loads(line) for line in (ledger.run_dir / "events.jsonl").read_text().splitlines()]


def test_stop_waits_for_candidate_pagination_without_claiming_completed_coverage(route, stop_followup):
    module, repo, *_ = route
    hook, _, event, ledger = stop_followup
    record_candidate_coverage_beyond_first_recovery_page(route)
    decisions = [hook.evaluate_stop_event(event, ledger) for _ in range(4)]
    assert decisions[0].reason_code == "independent_backup_route_candidate_verification_pending"
    assert all(decision.reason_code == "independent_backup_route_owns_or_completed_candidate" for decision in decisions[1:])
    assert all(decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY for decision in decisions)
    assert not any(item["event"] == "dirty_gate_completed" for item in ledger_events(ledger))
    (repo / "source.txt").write_text("新候选仍须进入原有发起判断\n")
    assert hook.evaluate_stop_event(event, ledger).reason_code == "independent_backup_route_candidate_verification_pending"
    hook.evaluate_stop_event(event, ledger)
    assert any(item["event"] == "dirty_gate_completed" for item in ledger_events(ledger))
    assert json.loads((module.control_directory(repo) / "stop-content-verification-observation.json").read_text())["status"] == "not_covered"


def test_candidate_pagination_wait_preserves_another_original_runs_continuation_budget(route, stop_followup, tmp_path):
    module, repo, *_ = route
    hook, _, event, ledger = stop_followup
    record_candidate_coverage_beyond_first_recovery_page(route)
    original_run = tmp_path / "another-original-run"
    first_consumption = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert first_consumption["status"] == "unknown" and first_consumption["recovery_after_operation"]
    marker = arm_in_progress(stop_followup, repo, original_run)
    decision = hook.evaluate_stop_event(event, ledger)
    assert decision.reason_code == "kanban_followup_in_progress_reengage"
    assert decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW
    assert marker.exists()
    assert json.loads(marker.read_text())["reengage_nudge_count"] == 1
    assert any(item["event"] == "kanban_followup_in_progress_nudged" for item in ledger_events(ledger))


@pytest.mark.parametrize("change", ["unchanged", "commit", "new_content"])
@pytest.mark.parametrize("entry", ["root", "subdirectory", "symlink"])
def test_stop_consumes_original_run_before_nudge_and_checks_current_candidate(route, stop_followup, tmp_path, change, entry):
    module, repo, _, _, git = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    marker = arm_in_progress(stop_followup, repo, original_run)
    pending = arm_pending(stop_followup, repo, original_run)
    nested = repo / "nested-source"
    nested.mkdir()
    alias = tmp_path / "repository-entry-alias"
    alias.symlink_to(nested, target_is_directory=True)
    event["cwd"] = str({"root": repo, "subdirectory": nested, "symlink": alias}[entry])
    if change == "commit":
        git("commit", "--allow-empty", "-qm", "fix: 备用交付后的新提交")
    elif change == "new_content":
        (repo / "source.txt").write_text("备用交付后的新内容\n")
    decision = hook.evaluate_stop_event(event, ledger)
    assert decision.reason_code != "kanban_followup_in_progress_reengage"
    assert not marker.exists()
    assert not pending.exists()
    events = ledger_events(ledger)
    assert any(item["event"] == "kanban_followup_lock_reconciled_for_backup_consumed_run" for item in events)
    assert not any(item["event"] == "kanban_followup_in_progress_nudged" for item in events)
    if change == "unchanged":
        assert decision.reason_code == "independent_backup_route_owns_or_completed_candidate"
        assert decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    else:
        assert any(item["event"] == "dirty_gate_completed" for item in events)


def test_active_backup_owner_suppresses_existing_run_without_claiming_completion(route, stop_followup, tmp_path):
    module, repo, authorization, artifacts, _ = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    marker = arm_in_progress(stop_followup, repo, original_run)
    module.claim_backup_route(repo, "operation-a", artifacts, authorization, "接管进行中", original_run, "land")
    decision = hook.evaluate_stop_event(event, ledger)
    assert decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    assert marker.exists()
    assert not module.consumed_run_record(repo, original_run).exists()


@pytest.mark.parametrize("stale", [False, True])
def test_backup_consumed_run_is_neither_nudged_nor_revived_even_if_marker_cleanup_fails(
    route, stop_followup, tmp_path, monkeypatch, stale,
):
    module, repo, *_ = route
    hook, locks, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    marker = arm_in_progress(stop_followup, repo, original_run, stale=stale)
    pending = arm_pending(stop_followup, repo, original_run)
    before = marker.read_bytes()
    (repo / "source.txt").write_text("新内容仍必须进入候选判断\n")
    unlink = Path.unlink
    def reject_marker_unlink(path, *args, **kwargs):
        if path in {marker, pending}:
            raise PermissionError("模拟凭证清理失败")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", reject_marker_unlink)
    decision = hook.evaluate_stop_event(event, ledger)
    assert decision.reason_code not in {"kanban_followup_in_progress_reengage", "kanban_followup_in_progress_revived_on_run_activity",
                                        "review_initiation_accepted_run_in_flight"}
    assert marker.read_bytes() == before
    assert pending.exists()
    assert any(item["event"] == "dirty_gate_completed" for item in ledger_events(ledger))
    monkeypatch.setattr(Path, "unlink", unlink)
    assert hook.kanban_followup_in_progress_decision(event, ledger, cwd=event["cwd"]) is None
    assert not marker.exists()
    assert not pending.exists()


@pytest.mark.parametrize("pending_for_other_run", [False, True])
def test_backup_consumed_run_keeps_another_run_in_progress_and_its_pending(
    route, stop_followup, tmp_path, pending_for_other_run,
):
    module, repo, *_ = route
    hook, _, event, ledger = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    consumed_marker = arm_in_progress(stop_followup, repo, original_run)
    other_marker = arm_in_progress(stop_followup, repo, other_run, task_id=None)
    pending = arm_pending(stop_followup, repo, other_run if pending_for_other_run else original_run)
    decision = hook.evaluate_stop_event(event, ledger)
    assert decision.reason_code == "kanban_followup_in_progress_reengage"
    assert decision.summary_fields["active_rvf_run_id"] == other_run.name
    assert not consumed_marker.exists()
    assert other_marker.exists()
    assert pending.exists() is pending_for_other_run


def test_backup_pending_is_reconciled_before_receipt_query_or_redispatch(route, stop_followup, tmp_path, monkeypatch):
    module, repo, *_ = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    pending = arm_pending(stop_followup, repo, original_run)
    marker = json.loads(pending.read_text())
    monkeypatch.setattr(hook, "_query_kanban_terminal_delivery_receipt", lambda *_: pytest.fail("已消费 run 不得再查询投递并重投"))
    assert hook._kanban_followup_pending_decision(event, ledger, task_id="backup-stop-task",
                                                 session_id=event["session_id"], cwd=event["cwd"]) is None
    assert not pending.exists()
    result = hook._maybe_redispatch_stranded_kanban_followup(marker, ledger, token=marker["token"])
    assert result["redispatched"] is False
    assert result["reason"] == "independent-backup-consumption-consumed"


def test_stranded_sweep_does_not_escalate_or_redispatch_backup_consumed_run(route, stop_followup, tmp_path, monkeypatch):
    module, repo, *_ = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    pending = arm_pending(stop_followup, repo, original_run, task_id="other-task")
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: pytest.fail("备用消费应先于搁浅升级核验"))
    hook.sweep_stranded_kanban_followup_pending(event, ledger)
    assert not pending.exists()


def test_unknown_backup_completion_stays_developer_only_and_recovers_on_next_stop(route, stop_followup, tmp_path):
    module, repo, _, artifacts, _ = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    marker = arm_in_progress(stop_followup, repo, original_run)
    pending = arm_pending(stop_followup, repo, original_run)
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    result_bytes = delivery.read_bytes()
    delivery.write_text("broken")
    (module.control_directory(repo) / "completed-operations/operation-a.json").write_text("broken")
    decision = hook.kanban_followup_in_progress_decision(event, ledger, cwd=event["cwd"])
    assert decision.reason_code == "kanban_followup_backup_consumption_unknown"
    assert decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    assert marker.exists() and pending.exists()
    delivery.write_bytes(result_bytes)
    assert hook.kanban_followup_in_progress_decision(event, ledger, cwd=event["cwd"]) is None
    assert not marker.exists() and not pending.exists()


@pytest.mark.parametrize("completion", ["verified", "unknown"])
@pytest.mark.parametrize("channel", ["blocking_decision", "non_error_additional_context", "hook_specific_output_permission_decision_deny"])
def test_backup_reconciliation_output_never_drives_any_host(route, stop_followup, tmp_path, completion, channel):
    module, repo, _, artifacts, _ = route
    hook, _, event, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    arm_in_progress(stop_followup, repo, original_run)
    if completion == "unknown":
        (module.control_directory(repo) / "completed-operations/operation-a.json").write_text("broken")
        (artifacts / "operation-a/backup-route-delivery-result.json").write_text("broken")
    decision = hook.evaluate_stop_event(event, ledger)
    payload = hook.render_stop_hook_payload(audience=decision.audience, agent_directive=decision.agent_directive,
                                           developer_only_payload=decision.payload, agent_directive_channel=channel)
    assert payload.get("continue") is True
    assert "decision" not in payload and "hookSpecificOutput" not in payload


@pytest.mark.parametrize("task_id", ["backup-stop-task", "other-task"])
def test_pending_cleanup_serializes_final_identity_guard_with_new_run_publication(
    route, stop_followup, tmp_path, monkeypatch, task_id,
):
    module, repo, *_ = route
    hook, locks, _, ledger = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    arm_pending(stop_followup, repo, original_run, task_id=task_id)
    publication_reached_serial_boundary = threading.Event()
    publication_errors = []
    read_pending = locks.read_pending_marker
    atomic_write = locks._atomic_write
    flock = locks.fcntl.flock

    def publish_next_run():
        try:
            with locks.dispatch_single_flight_for_task(task_id) as acquired:
                assert acquired
                arm_pending(stop_followup, repo, other_run, task_id=task_id)
        except BaseException as exc:
            publication_errors.append(exc)
            publication_reached_serial_boundary.set()

    publisher = threading.Thread(target=publish_next_run)

    def observe_writer_lock(file_descriptor, operation):
        # 派发闸使用 LOCK_NB；这里只在实际 pending 写锁将阻塞时放行旧身份检查。
        if threading.current_thread() is publisher and operation == locks.fcntl.LOCK_EX:
            publication_reached_serial_boundary.set()
        return flock(file_descriptor, operation)

    def observe_unserialized_publication(path, payload):
        result = atomic_write(path, payload)
        if threading.current_thread() is publisher:
            # 修复前没有写锁：确定性地先发布新 run，再继续旧 run 的 unlink。
            publication_reached_serial_boundary.set()
        return result

    def publish_after_last_identity_read(**kwargs):
        pending = read_pending(**kwargs)
        publisher.start()
        assert publication_reached_serial_boundary.wait(5), "新 run 未进入发布串行边界"
        return pending

    monkeypatch.setattr(locks.fcntl, "flock", observe_writer_lock)
    monkeypatch.setattr(locks, "_atomic_write", observe_unserialized_publication)
    monkeypatch.setattr(locks, "read_pending_marker", publish_after_last_identity_read)
    evidence = module.read_original_run_backup_consumption_evidence(repo, original_run)
    try:
        hook.clear_backup_consumed_followup_pending(ledger, task_id=task_id, evidence=evidence)
    finally:
        publisher.join(5)
    assert not publisher.is_alive(), "pending 清理不得与发布互相等待"
    assert publication_errors == []
    pending = read_pending(task_id=task_id)
    assert pending is not None and pending["run_id"] == other_run.name


def test_pending_cleanup_and_metadata_updates_work_while_dispatch_gate_is_already_held(
    route, stop_followup, tmp_path,
):
    module, repo, *_ = route
    hook, locks, _, ledger = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    evidence = module.read_original_run_backup_consumption_evidence(repo, original_run)
    with locks.dispatch_single_flight_for_task("backup-stop-task") as acquired:
        assert acquired
        pending = arm_pending(stop_followup, repo, original_run)
        assert locks.stamp_pending_notified(task_id="backup-stop-task", token="original-dispatch-token")
        assert hook.clear_backup_consumed_followup_pending(ledger, task_id="backup-stop-task", evidence=evidence)
    assert not pending.exists()


def test_pending_cleanup_rechecks_run_identity_before_removing_replaced_marker(route, stop_followup, tmp_path, monkeypatch):
    module, repo, *_ = route
    hook, locks, _, ledger = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    arm_pending(stop_followup, repo, original_run)
    clear_pending = hook.clear_kanban_followup_pending
    def replace_before_cleanup(**kwargs):
        # 即使另一轮错误复用了 token，run 归属检查也必须保留它。
        arm_pending(stop_followup, repo, other_run)
        return clear_pending(**kwargs)
    monkeypatch.setattr(hook, "clear_kanban_followup_pending", replace_before_cleanup)
    evidence = module.read_original_run_backup_consumption_evidence(repo, original_run)
    assert hook.clear_backup_consumed_followup_pending(ledger, task_id="backup-stop-task", evidence=evidence) == []
    assert locks.read_pending_marker(task_id="backup-stop-task")["run_id"] == other_run.name


def prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry):
    module, repo, *_ = route
    hook, locks, event, ledger = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    (repo / "source.txt").write_text("原 run 占用的未审内容\n")
    original_scope_path = original_run / "artifacts/tracker-scope.json"
    original_scope_path.parent.mkdir(parents=True)
    original_scope = hook.allocate_review_scope(
        repo=repo, session_id=event["session_id"], run_id=original_run.name, reviewer_id="original-reviewer",
        output_scope_path=original_scope_path, lease_ttl_seconds=7200,
    )
    assert original_scope["status"] == "allocated"
    (repo / "other-source.txt").write_text("另一 run 仍在评审的内容\n")
    other_scope = hook.allocate_review_scope(
        repo=repo, session_id="unrelated-session", run_id=other_run.name, reviewer_id="other-reviewer",
        lease_ttl_seconds=7200,
    )
    assert other_scope["status"] == "allocated"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    task_id = "stranded-backup-task" if entry == "stranded_sweep" else "backup-stop-task"
    pending = arm_pending(stop_followup, repo, original_run, task_id=task_id)
    in_progress = arm_in_progress(stop_followup, repo, original_run) if entry == "in_progress" else None
    other_pending = arm_pending(stop_followup, repo, other_run, task_id="unrelated-task")
    other_marker = locks.write_marker(
        task_id="unrelated-task", session_id="unrelated-session", run_id=other_run.name,
        run_dir=str(other_run), repo=str(repo), cwd=str(repo), state=locks.STATE_IN_PROGRESS,
    )
    # 备用完成后再产生新内容，并通过真实归属／候选查询确认旧 lease 不再遮住原 scope。
    (repo / "new-source.txt").write_text("备用完成后新增的未审内容\n")
    from core.session_scope_allocation.reviewable_unit_diff_tracker import register_claims
    register_claims(
        repo=repo, session_id=event["session_id"], run_id=None, worktree=repo, branch="main",
        owned_paths=["source.txt", "new-source.txt"], apply_patch_paths={"source.txt", "new-source.txt"}, exec_only_paths=set(),
    )
    return {"original_run": original_run, "other_run": other_run, "original_scope": original_scope,
            "other_scope": other_scope, "scope_path": original_scope_path,
            "db_path": Path(original_scope["tracker_dir"]) / "tracker.sqlite3",
            "pending": pending, "in_progress": in_progress, "other_pending": other_pending,
            "other_marker": other_marker, "task_id": task_id}


def read_tracker_lease_state(fixture, scope_name):
    with sqlite3.connect(fixture["db_path"]) as connection:
        return connection.execute("SELECT state FROM leases WHERE lease_id=?",
                                  (fixture[scope_name]["lease_id"],)).fetchone()[0]


def current_tracker_candidate(stop_followup, repo):
    hook, _, event, _ = stop_followup
    return hook.allocate_review_scope(repo=repo, session_id=event["session_id"], run_id="candidate-after-backup",
                                      dry_run=True, auto_claim_observed=False)


def reconcile_consumed_entry(stop_followup, entry):
    hook, _, event, ledger = stop_followup
    if entry == "stranded_sweep":
        return hook.sweep_stranded_kanban_followup_pending(event, ledger)
    if entry == "pending_only":
        return hook._kanban_followup_pending_decision(event, ledger, task_id="backup-stop-task",
                                                      session_id=event["session_id"], cwd=event["cwd"])
    return hook.kanban_followup_in_progress_decision(event, ledger, cwd=event["cwd"])


@pytest.mark.parametrize("entry", ["in_progress", "pending_only", "stranded_sweep"])
def test_backup_consumed_cleanup_releases_exact_real_lease_and_restores_current_candidate(
    route, stop_followup, tmp_path, monkeypatch, entry,
):
    _, repo, *_ = route
    hook, _, _, _ = stop_followup
    fixture = prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry)
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: "alive")
    original_units = set(fixture["original_scope"]["scope"]["unit_ids"])
    assert read_tracker_lease_state(fixture, "original_scope") == "active"
    assert original_units.isdisjoint(current_tracker_candidate(stop_followup, repo)["unit_ids"])
    assert reconcile_consumed_entry(stop_followup, entry) is None
    assert read_tracker_lease_state(fixture, "original_scope") == "failed-released"
    candidate = current_tracker_candidate(stop_followup, repo)
    assert original_units.issubset(candidate["unit_ids"])
    assert "new-source.txt" in candidate["paths"]
    assert read_tracker_lease_state(fixture, "other_scope") == "active"
    assert fixture["other_pending"].exists() and fixture["other_marker"].exists()
    assert not fixture["pending"].exists()
    assert fixture["in_progress"] is None or not fixture["in_progress"].exists()


@pytest.mark.parametrize("entry", ["in_progress", "pending_only", "stranded_sweep"])
def test_backup_consumed_lease_release_failure_keeps_retry_credentials_without_nudge(
    route, stop_followup, tmp_path, monkeypatch, entry,
):
    _, repo, *_ = route
    hook, locks, event, ledger = stop_followup
    fixture = prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry)
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: "alive")
    release = hook.lease_release
    def fail_lease_release(**kwargs):
        raise OSError("模拟 tracker 暂不可写")
    monkeypatch.setattr(hook, "lease_release", fail_lease_release)
    decision = reconcile_consumed_entry(stop_followup, entry)
    assert decision is None or decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    assert fixture["pending"].exists()
    assert fixture["in_progress"] is None or fixture["in_progress"].exists()
    assert read_tracker_lease_state(fixture, "original_scope") == "active"
    assert not any(item["event"] == "kanban_followup_in_progress_nudged" for item in ledger_events(ledger))
    assert any(item["event"] == "kanban_followup_backup_consumed_tracker_lease_reconciliation"
               and item["status"] == "warning" and item["recovery_condition"] for item in ledger_events(ledger))
    monkeypatch.setattr(hook, "lease_release", release)
    # 重建 hook 与 ledger，证明恢复只依赖持久凭证，不依赖上一次 Stop 的进程内状态。
    spec = importlib.util.spec_from_file_location("rvf_stop_backup_lease_retry_tests", SCRIPT.with_name("stop_review_validate_fix.py"))
    restarted_hook = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, restarted_hook)
    spec.loader.exec_module(restarted_hook)
    monkeypatch.setattr(restarted_hook, "_kanban_task_session_liveness", lambda *_: "alive")
    restarted = restarted_hook, locks, event, restarted_hook.start_stop_hook_ledger(event)
    assert reconcile_consumed_entry(restarted, entry) is None
    assert not fixture["pending"].exists()
    assert read_tracker_lease_state(fixture, "original_scope") == "failed-released"
    assert read_tracker_lease_state(fixture, "other_scope") == "active"
    assert set(fixture["original_scope"]["scope"]["unit_ids"]).issubset(current_tracker_candidate(stop_followup, repo)["unit_ids"])


@pytest.mark.parametrize("entry", ["in_progress", "pending_only", "stranded_sweep"])
def test_backup_consumed_cleanup_rejects_another_runs_lease_identity_and_retries(
    route, stop_followup, tmp_path, monkeypatch, entry,
):
    hook, _, _, _ = stop_followup
    fixture = prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry)
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: "alive")
    scope_bytes = fixture["scope_path"].read_bytes()
    scope = json.loads(scope_bytes)
    scope["lease_id"] = fixture["other_scope"]["lease_id"]
    fixture["scope_path"].write_text(json.dumps(scope))
    decision = reconcile_consumed_entry(stop_followup, entry)
    assert decision is None or decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    assert fixture["pending"].exists()
    assert read_tracker_lease_state(fixture, "original_scope") == "active"
    assert read_tracker_lease_state(fixture, "other_scope") == "active"
    fixture["scope_path"].write_bytes(scope_bytes)
    assert reconcile_consumed_entry(stop_followup, entry) is None
    assert read_tracker_lease_state(fixture, "original_scope") == "failed-released"
    assert read_tracker_lease_state(fixture, "other_scope") == "active"


@pytest.mark.parametrize("entry", ["in_progress", "pending_only", "stranded_sweep"])
@pytest.mark.parametrize("recovery", ["missing_scope", "already_released"])
def test_backup_consumed_cleanup_recovers_native_lease_evidence_without_requiring_artifact_or_active_state(
    route, stop_followup, tmp_path, monkeypatch, entry, recovery,
):
    _, repo, *_ = route
    hook, _, _, _ = stop_followup
    fixture = prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry)
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: "alive")
    if recovery == "missing_scope":
        fixture["scope_path"].unlink()
    else:
        hook.lease_release(repo=repo, lease_id=fixture["original_scope"]["lease_id"], reason="test_cleanup_before_retry")
    assert reconcile_consumed_entry(stop_followup, entry) is None
    assert not fixture["pending"].exists()
    assert read_tracker_lease_state(fixture, "original_scope") == "failed-released"
    assert read_tracker_lease_state(fixture, "other_scope") == "active"
    assert set(fixture["original_scope"]["scope"]["unit_ids"]).issubset(current_tracker_candidate(stop_followup, repo)["unit_ids"])


@pytest.mark.parametrize("entry", ["in_progress", "pending_only", "stranded_sweep"])
def test_backup_consumed_cleanup_keeps_credentials_when_native_lease_query_is_unavailable(
    route, stop_followup, tmp_path, monkeypatch, entry,
):
    hook, _, _, ledger = stop_followup
    fixture = prepare_backup_consumption_with_real_tracker_leases(route, stop_followup, tmp_path, entry)
    monkeypatch.setattr(hook, "_kanban_task_session_liveness", lambda *_: "alive")
    unavailable_database = fixture["db_path"].with_name("tracker-unavailable-for-retry.sqlite3")
    fixture["db_path"].rename(unavailable_database)
    try:
        decision = reconcile_consumed_entry(stop_followup, entry)
        assert fixture["pending"].exists()
    finally:
        unavailable_database.rename(fixture["db_path"])
    assert decision is None or decision.audience == hook.STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY
    assert read_tracker_lease_state(fixture, "original_scope") == "active"
    assert read_tracker_lease_state(fixture, "other_scope") == "active"
    assert not any(item["event"] == "kanban_followup_in_progress_nudged" for item in ledger_events(ledger))
    assert reconcile_consumed_entry(stop_followup, entry) is None
    assert not fixture["pending"].exists()
    assert read_tracker_lease_state(fixture, "original_scope") == "failed-released"


def test_unconsumed_run_still_uses_existing_nudge_budget(route, stop_followup, tmp_path, monkeypatch):
    _, repo, *_ = route
    hook, _, event, ledger = stop_followup
    marker = arm_in_progress(stop_followup, repo, tmp_path / "rvf-unfinished-run")
    monkeypatch.setenv("CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET", "2")
    decisions = [hook.kanban_followup_in_progress_decision(event, ledger, cwd=event["cwd"]) for _ in range(3)]
    assert [item.reason_code for item in decisions] == ["kanban_followup_in_progress_reengage",
                                                      "kanban_followup_in_progress_reengage", "kanban_followup_in_progress"]
    assert json.loads(marker.read_text())["reengage_nudge_count"] == 2


@pytest.mark.parametrize("commit_after_finish", [False, True])
def test_dispatcher_executes_real_stop_without_reengaging_backup_consumed_run(route, stop_followup, tmp_path, commit_after_finish):
    module, repo, _, _, git = route
    _, _, event, _ = stop_followup
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    marker = arm_in_progress(stop_followup, repo, original_run)
    pending = arm_pending(stop_followup, repo, original_run)
    if commit_after_finish:
        git("commit", "--allow-empty", "-qm", "fix: dispatcher 复现提交后旧续跑")
    environment = {**os.environ, "CODEX_RVF_DEV_SYNC": "0",
                   "CODEX_RVF_INSTALLED_STOP_HOOK": str(SCRIPT.with_name("stop_review_validate_fix.py"))}
    completed = subprocess.run([sys.executable, str(SCRIPT.with_name("stop_hook_dispatcher.py"))],
                               input=json.dumps(event), env=environment, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert "仍在进行且未 handoff" not in completed.stdout
    assert not marker.exists() and not pending.exists()
    if not commit_after_finish:
        assert "decision" not in payload and "hookSpecificOutput" not in payload


def test_dispatcher_continues_another_run_even_when_current_candidate_has_backup_coverage(route, stop_followup, tmp_path):
    module, repo, *_ = route
    _, _, event, _ = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    consumed_marker = arm_in_progress(stop_followup, repo, original_run)
    other_marker = arm_in_progress(stop_followup, repo, other_run, task_id=None)
    environment = {**os.environ, "CODEX_RVF_DEV_SYNC": "0",
                   "CODEX_RVF_INSTALLED_STOP_HOOK": str(SCRIPT.with_name("stop_review_validate_fix.py"))}
    completed = subprocess.run([sys.executable, str(SCRIPT.with_name("stop_hook_dispatcher.py"))],
                               input=json.dumps(event), env=environment, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload.get("decision") == "block"
    assert other_run.name in payload["reason"] and original_run.name not in payload["reason"]
    assert not consumed_marker.exists() and other_marker.exists()


@pytest.mark.parametrize("guard,expected_reason", [
    ("plan_operation_metadata", "plan_operation"),
    ("plan_operation_assistant_text", "plan_operation"),
    ("suppress_environment", "suppressed"),
    ("suppress_stop_hook_environment", "suppressed"),
    ("codex_goal_mode", "codex_goal_mode"),
    ("handoff", "handoff_file_ready"),
    ("analyze_followup", "rvf_analyze_followup_trigger_turn"),
    ("ordinary_execution", None),
])
def test_dispatcher_backup_coverage_preserves_host_guards_before_reconciling_another_run(
    route, stop_followup, tmp_path, guard, expected_reason,
):
    module, repo, *_ = route
    _, _, event, _ = stop_followup
    original_run, other_run = tmp_path / "rvf-original-run", tmp_path / "rvf-other-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    consumed_marker = arm_in_progress(stop_followup, repo, original_run)
    consumed_pending = arm_pending(stop_followup, repo, original_run)
    other_marker = arm_in_progress(stop_followup, repo, other_run, task_id=None)
    other_marker_before = other_marker.read_bytes()
    dispatcher_run_directory = tmp_path / "isolated-dispatcher-guard-run"
    isolated_external_command = tmp_path / "isolated-external-command.py"
    isolated_external_command.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    isolated_external_command.chmod(0o755)
    environment = {
        **os.environ,
        "CODEX_RVF_DEV_SYNC": "0",
        "CODEX_RVF_INSTALLED_STOP_HOOK": str(SCRIPT.with_name("stop_review_validate_fix.py")),
        "RVF_RUN_ID": dispatcher_run_directory.name,
        "RVF_RUN_DIR": str(dispatcher_run_directory),
        "RVF_TERMINAL_NOTIFIER_BIN": str(isolated_external_command),
        "RVF_TMUX_BIN": str(isolated_external_command),
        "CODEX_RVF_CODEX_BIN": str(isolated_external_command),
        "CODEX_RVF_CLAUDE_BIN": str(isolated_external_command),
    }
    if guard == "plan_operation_metadata":
        event["operation"] = "plan"
    elif guard == "plan_operation_assistant_text":
        event["last_assistant_message"] = "<proposed_plan>\n先核对已授权范围。\n</proposed_plan>"
    elif guard == "suppress_environment":
        environment["RVF_SUPPRESS"] = "1"
    elif guard == "suppress_stop_hook_environment":
        environment["RVF_SUPPRESS_STOP_HOOK"] = "1"
    elif guard == "codex_goal_mode":
        transcript = tmp_path / "isolated-codex-goal-transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
            {"type": "session_meta", "payload": {
                "id": event["session_id"], "cwd": str(repo),
                "originator": "codex-tui", "cli_version": "0.130.0",
            }},
            {"type": "event_msg", "payload": {
                "type": "thread_goal_updated", "thread_id": event["session_id"],
                "goal": {"threadId": event["session_id"], "objective": "隔离验证", "status": "active"},
            }},
        ]) + "\n")
        event["transcript_path"] = str(transcript)
    elif guard == "handoff":
        handoff = tmp_path / "isolated-handoff.md"
        handoff.write_text("# 隔离交接夹具\n")
        event["last_assistant_message"] = f"RVF_HANDOFF_FILE: {handoff}"
    elif guard == "analyze_followup":
        event["last_user_message"] = "分析已结束的 run。\nRVF_KANBAN_ANALYZE_TRIGGER"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT.with_name("stop_hook_dispatcher.py"))],
        input=json.dumps(event), env=environment, capture_output=True, text=True, timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    events = [json.loads(line) for line in (dispatcher_run_directory / "events.jsonl").read_text().splitlines()]
    installed_hook_started = any(item["event"] == "installed_hook_started" for item in events)
    if expected_reason is None:
        assert installed_hook_started
        assert payload.get("decision") == "block"
        assert other_run.name in payload["reason"] and original_run.name not in payload["reason"]
        assert not consumed_marker.exists() and not consumed_pending.exists()
        assert other_marker.exists()
    else:
        assert payload.get("continue") is True
        assert "decision" not in payload and "hookSpecificOutput" not in payload
        assert f"reason={expected_reason}" in payload["systemMessage"]
        assert not installed_hook_started
        assert other_marker.read_bytes() == other_marker_before
