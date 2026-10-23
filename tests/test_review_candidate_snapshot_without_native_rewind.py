"""普通 RVF 冻结候选，不把可选原生回退能力变成审查准入。"""

import json
import subprocess
import sys
from pathlib import Path
import pytest

from test_review_start_independent_user_turn_boundary import valid_boundary, isolate_runtime_state, git


def test_missing_native_receipt_still_reaches_prepare_with_real_candidate_snapshot(valid_boundary, monkeypatch):
    f = valid_boundary
    f["gate"].review_start_boundary_receipt_path(f["token"]).unlink()
    monkeypatch.setattr(f["gate"], "_read_or_reconcile_host_boundary_receipt", lambda *a, **k: missing_native())
    calls = []
    result = f["cli"].run_start(token=f["token"], reason="已授权审查，缺回退能力仍继续", prepare_pipeline=lambda **k: calls.append(k) or {"status":"failed", "error":"合成准备结果"})
    assert len(calls) == 1
    assert result.status == "start_failed"
    snapshot = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    assert snapshot["schema"] == "rvf-review-candidate-snapshot/v1"
    assert snapshot["automatic_closeout_eligibility"]["status"] == "unavailable"
    assert "native_user_message_id" not in snapshot
    assert git(f["repo"], "show", snapshot["checkpoint_commit"]+":a.py") == "value = 2"
    assert git(f["repo"], "rev-parse", "HEAD") == f["receipt"]["worktree_head_commit_sha"]


def missing_native():
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified, PENDING
    raise ReviewStartBoundaryNotVerified(PENDING, "合成宿主没有原生回退能力", reason_code="native_rewind_capability_unverified", recovery_action="repair_host_capability")


def test_candidate_snapshot_rejects_content_changes_even_without_native_rewind(valid_boundary, monkeypatch):
    f = valid_boundary
    monkeypatch.setattr(f["gate"], "_read_or_reconcile_host_boundary_receipt", lambda *a, **k: missing_native())
    f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    (f["repo"] / "a.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="候选|快照"):
        f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))


def test_candidate_snapshot_does_not_invalidate_identical_content_rewrite(valid_boundary, monkeypatch):
    f = valid_boundary
    monkeypatch.setattr(f["gate"], "_read_or_reconcile_host_boundary_receipt", lambda *a, **k: missing_native())
    first = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    (f["repo"] / "a.py").write_text("value = 2\n")
    second = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    assert first["checkpoint_commit"] == second["checkpoint_commit"]


def test_candidate_snapshot_never_bypasses_withdrawn_token(valid_boundary):
    f = valid_boundary
    payload = {**f["payload"], "state":"withdrawn"}
    with pytest.raises(ValueError, match="token|撤销|失效"):
        f["gate"].require_review_start_boundary(token_payload=payload, repo=str(f["repo"]))


def test_direct_prepare_and_dispatch_gate_use_candidate_snapshot_without_native_receipt(valid_boundary, tmp_path, monkeypatch):
    import prepare_review_run
    f = valid_boundary
    f["gate"].review_start_boundary_receipt_path(f["token"]).unlink()
    monkeypatch.setattr(f["gate"], "_read_or_reconcile_host_boundary_receipt", lambda *a, **k: missing_native())
    context = tmp_path / "approved-review-scope.md"
    context.write_text("审查 a.py 中已授权的 value 修改。\n")
    prepared = prepare_review_run.prepare_run(
        repo=f["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[], decision_token=f["token"],
    )
    contract = json.loads(Path(prepared["scope_contract"]).read_text())
    assert contract["review_start_boundary_receipt"] is None
    assert contract["review_candidate_snapshot"]["schema"] == "rvf-review-candidate-snapshot/v1"
    verified = f["gate"].require_prepared_run_review_start_boundary(
        repo=str(f["repo"]), scope_contract=prepared["scope_contract"], run_id=prepared["run_id"], run_dir=prepared["run_dir"],
    )
    assert verified["decision_token"] == f["token"]


@pytest.mark.parametrize("phase", ["bind", "activate"])
def test_optional_registration_failure_is_persisted_and_does_not_fail_review_start(tmp_path, monkeypatch, phase):
    import rvf_automatic_closeout_bridge as bridge
    run = tmp_path / "run"
    def fail(*args, **kwargs):
        raise TimeoutError("合成注册响应未知")
    monkeypatch.setattr(bridge, "bind_run", fail if phase == "bind" else lambda *a, **k: None)
    monkeypatch.setattr(bridge, "activate_bound_run", fail if phase == "activate" else lambda *a, **k: None)
    result = bridge.register_optional_automatic_closeout({"decision_token":"0123456789abcdef"}, run, run_id="run", decision_token="0123456789abcdef")
    assert result["status"] == "disabled"
    assert result["failed_phase"] == phase
    persisted = json.loads((run / "artifacts" / "automatic-closeout-registration.json").read_text())
    assert persisted == result
    assert bridge._completion_notification_identity(run, {"completed_at":"now"}) is None


def test_manual_land_uses_real_review_candidate_without_native_receipt(valid_boundary, tmp_path, monkeypatch):
    """没有原生能力仍能消费规范 reviewer 结果、验证并经实际 land CLI 提交。"""
    import prepare_review_run
    f = valid_boundary
    f["gate"].review_start_boundary_receipt_path(f["token"]).unlink()
    monkeypatch.setattr(f["gate"], "_read_or_reconcile_host_boundary_receipt", lambda *a, **k: missing_native())
    context = tmp_path / "approved-manual-review-scope.md"
    context.write_text("审查 a.py 中 value 从 1 改为 2 的实现。\n")
    prepared = prepare_review_run.prepare_run(
        repo=f["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[], decision_token=f["token"],
    )
    run = Path(prepared["run_dir"])
    artifacts = run / "artifacts"
    scripts = Path(prepare_review_run.__file__).parent

    def execute(script, *arguments):
        result = subprocess.run([sys.executable, str(scripts / script), *arguments], cwd=f["repo"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    reviewers = []
    for reviewer_id in ("controlled-reviewer-a", "controlled-reviewer-b"):
        execute("write_review_result.py", "no-issues", "--out", str(artifacts / "reviewers" / reviewer_id / "review-result.json"), "--audit-summary", "受控 reviewer 核对 value 修改的候选与范围")
        reviewers.append({"reviewer_id": reviewer_id, "returncode": 0})
    (artifacts / "reviewers" / "reviewer-plan.json").write_text(json.dumps({"schema_version": 1, "status": "completed", "reviewers": reviewers}))
    handoff = artifacts / "handoff.md"
    handoff.write_text("# 人工收尾交接\n\n- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n## 本次 review\n\n共 0 条，0 真实修复 / 0 误报 / 0 升级\n")
    execute("validation_result_cache.py", "run", "--repo", str(f["repo"]), "--name", "manual-candidate-behavior", "--rvf-run-dir", str(run), "--", sys.executable, "-B", "-c", "from a import value; assert value == 2")
    execute("rvf_land.py", "prepare", "--handoff", str(handoff), "--repo", str(f["repo"]), "--run-dir", str(run), "--format", "json")
    message = artifacts / "manual-land-commit-message.txt"
    message.write_text("test(rvf): 提交无原生回退能力的已审候选\n")
    committed = json.loads(execute("rvf_land.py", "commit", "--repo", str(f["repo"]), "--run-dir", str(run), "--message-file", str(message)).stdout)
    assert committed["status"] == "committed"
    assert committed["candidate_evidence"]["status"] == "written"
    assert git(f["repo"], "status", "--porcelain") == ""


def test_missing_completed_native_prefix_records_manual_closeout_instead_of_registered(tmp_path, monkeypatch):
    import rvf_automatic_closeout_bridge as bridge
    token = "0123456789abcdef"
    monkeypatch.setattr(bridge, "bind_run", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "activate_bound_run", lambda *a, **k: {"automaticCloseoutActivationStatus": "skipped_no_completed_pre_review_conversation_prefix"})
    registration = bridge.register_optional_automatic_closeout({"decision_token": token}, tmp_path / "run", run_id="logical-run", decision_token=token)
    assert registration["status"] == "disabled"
    assert registration["reason_code"] == "no_completed_pre_review_conversation_prefix"


@pytest.mark.parametrize("native_failure", [
    "non_independent_turn", "malformed_receipt", "unreadable_receipt",
    "receipt_session_mismatch", "nonce_mismatch", "native_reader_exception",
])
def test_invalid_optional_native_evidence_does_not_block_candidate_snapshot(valid_boundary, monkeypatch, native_failure):
    f = valid_boundary
    receipt_path = f["gate"].review_start_boundary_receipt_path(f["token"])
    if native_failure == "non_independent_turn":
        f["records"].append({"type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "independent-turn",
            "item": {"type": "UserMessage", "id": "second-native-message", "content": []},
        }})
        f["transcript"].write_text("\n".join(json.dumps(record) for record in f["records"]) + "\n")
    elif native_failure == "malformed_receipt":
        receipt_path.write_text("{")
    elif native_failure == "unreadable_receipt":
        receipt_path.unlink()
        receipt_path.mkdir()
    elif native_failure == "receipt_session_mismatch":
        receipt_path.write_text(json.dumps({**f["receipt"], "session_id": "another-native-session"}))
    elif native_failure == "nonce_mismatch":
        import cline_kanban_client
        monkeypatch.setattr(cline_kanban_client, "read_task_message_terminal_delivery_status", lambda **k: {
            "independent_user_turn_boundary_receipt": f["receipt"],
            "review_start_boundary_active_branch_observation": {"request_nonce": "stale-observation"},
        })
    else:
        def unavailable_native_reader(**kwargs):
            raise RuntimeError("合成原生 reader 不可用")
        monkeypatch.setattr(f["gate"], "require_native_review_start_boundary", unavailable_native_reader)

    snapshot = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    assert snapshot["automatic_closeout_eligibility"]["status"] == "unavailable"
    assert snapshot["automatic_closeout_eligibility"]["reason"]
    assert "native_review_start_boundary_receipt" not in snapshot
    assert git(f["repo"], "show", snapshot["checkpoint_commit"] + ":a.py") == "value = 2"


@pytest.mark.parametrize("capture_failure", ["pending", "invalidated", "exception"])
def test_native_capture_failure_keeps_review_start_and_records_manual_fallback(valid_boundary, monkeypatch, capture_failure):
    from core.review_start_boundary.review_start_boundary_verification import (
        INVALIDATED, PENDING, ReviewStartBoundaryNotVerified,
    )
    import rvf_automatic_closeout_bridge
    f = valid_boundary
    def fail_native_capture(*args, **kwargs):
        if capture_failure == "exception":
            raise TimeoutError("合成捕获请求未确认")
        raise ReviewStartBoundaryNotVerified(
            INVALIDATED if capture_failure == "invalidated" else PENDING,
            "合成宿主未提供可用原生分界", reason_code="synthetic_native_capture_unavailable",
        )
    monkeypatch.setattr(rvf_automatic_closeout_bridge, "capture_before_start", fail_native_capture)
    prepare_calls = []
    result = f["cli"].run_start(
        token=f["token"], reason="普通候选已授权，原生捕获只影响自动恢复",
        launch_preflight=lambda **k: (True, None), provider_health_requirements_resolver=lambda backend: [],
        prepare_pipeline=lambda **k: prepare_calls.append(k) or {"status": "failed", "error": "仅验证到 prepare"},
    )
    assert result.status == "start_failed"
    assert len(prepare_calls) == 1
    snapshot = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    assert snapshot["automatic_closeout_eligibility"]["status"] == "unavailable"
    assert "native_review_start_boundary_receipt" not in snapshot


@pytest.mark.parametrize("candidate_change", ["content", "head", "mode"])
def test_optional_native_failure_cannot_hide_concurrent_candidate_changes(valid_boundary, monkeypatch, candidate_change):
    from core.review_start_boundary.review_start_boundary_verification import PENDING, ReviewStartBoundaryNotVerified
    f = valid_boundary
    def mutate_candidate_during_native_verification(**kwargs):
        if candidate_change == "content":
            (f["repo"] / "a.py").write_text("value = 99\n")
        elif candidate_change == "head":
            git(f["repo"], "commit", "--allow-empty", "-qm", "test: 原生查询期间推进 HEAD")
        else:
            (f["repo"] / "a.py").chmod(0o755)
        raise ReviewStartBoundaryNotVerified(PENDING, "合成原生观测失败")
    monkeypatch.setattr(f["gate"], "require_native_review_start_boundary", mutate_candidate_during_native_verification)
    prepare_calls = []
    result = f["cli"].run_start(
        token=f["token"], reason="原生降级不能覆盖真实候选变化",
        launch_preflight=lambda **k: (True, None), provider_health_requirements_resolver=lambda backend: [],
        prepare_pipeline=lambda **k: prepare_calls.append(k) or {"status": "failed"},
    )
    assert result.status == "review_start_boundary_invalidated"
    assert f["tokens"].read_decision_token(f["token"]).state == "minted"
    assert not prepare_calls


@pytest.mark.parametrize("native_receipt_environment", ["malformed", "foreign_token"])
def test_direct_prepare_uses_explicit_decision_token_when_optional_receipt_is_unusable(valid_boundary, tmp_path, monkeypatch, native_receipt_environment):
    import prepare_review_run
    f = valid_boundary
    optional_receipt = tmp_path / "optional-native-receipt.json"
    optional_receipt.write_text("{" if native_receipt_environment == "malformed" else json.dumps({
        **f["receipt"], "decision_token": "fedcba9876543210",
    }))
    monkeypatch.setenv("RVF_REVIEW_START_BOUNDARY_RECEIPT", str(optional_receipt))
    context = tmp_path / "approved-explicit-token-review-scope.md"
    context.write_text("审查 a.py 的已授权改动。\n")
    prepared = prepare_review_run.prepare_run(
        repo=f["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[], decision_token=f["token"],
    )
    contract = json.loads(Path(prepared["scope_contract"]).read_text())
    assert contract["review_candidate_snapshot"]["decision_token"] == f["token"]
    assert contract["automatic_closeout_eligibility"]["status"] == "unavailable"
    f["gate"].require_prepared_run_review_start_boundary(
        repo=str(f["repo"]), scope_contract=prepared["scope_contract"],
        run_id=prepared["run_id"], run_dir=prepared["run_dir"],
    )


@pytest.mark.parametrize("identity_failure", ["session_argument", "current_session", "worktree_binding"])
def test_native_fallback_never_bypasses_ordinary_invocation_identity(valid_boundary, monkeypatch, tmp_path, identity_failure):
    f = valid_boundary
    payload = dict(f["payload"])
    arguments = {}
    if identity_failure == "session_argument":
        arguments["session_id"] = "another-calling-session"
    elif identity_failure == "current_session":
        monkeypatch.setenv("CODEX_THREAD_ID", "another-current-session")
    else:
        payload["worktree"] = str(tmp_path / "another-bound-worktree")
    with pytest.raises(ValueError, match="session/worktree"):
        f["gate"].require_review_start_boundary(token_payload=payload, repo=str(f["repo"]), **arguments)
    assert f["tokens"].read_decision_token(f["token"]).state == "minted"


def test_failed_native_recapture_cannot_register_automation_using_cached_eligible_snapshot(valid_boundary, tmp_path, monkeypatch):
    from core.review_start_boundary.review_start_boundary_verification import INVALIDATED, ReviewStartBoundaryNotVerified
    import rvf_automatic_closeout_bridge
    f = valid_boundary
    snapshot = f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"]))
    assert snapshot["automatic_closeout_eligibility"]["status"] == "eligible"
    def fail_native_recapture(*args, **kwargs):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "合成原生重捕获失败")
    monkeypatch.setattr(rvf_automatic_closeout_bridge, "capture_before_start", fail_native_recapture)
    run = tmp_path / "runs" / "prepared-review-run"
    result = f["cli"].run_start(
        token=f["token"], reason="缓存候选可继续，失败的原生重捕获不得激活自动恢复",
        launch_preflight=lambda **k: (True, None), provider_health_requirements_resolver=lambda backend: [],
        prepare_pipeline=lambda **k: {"status": "completed", "run_id": "logical-review-run", "run_dir": str(run), "artifacts": {}},
    )
    assert result.status == "ok"
    assert result.detail["automatic_closeout_registration"]["status"] == "disabled"
    assert f["gate"].require_review_start_boundary(token_payload=f["payload"], repo=str(f["repo"])) == snapshot
    assert not (run / "inputs" / "automatic-closeout-review-start-receipt.json").exists()


def _freeze_isolated_snapshot_for_selected_candidate_path(repo, selected_candidate_path):
    import stop_decision_candidate_summary
    from core.review_start_boundary.review_candidate_snapshot import freeze_review_candidate_snapshot

    def build_selected_candidate_summary(_fixed_snapshot=None):
        summary = stop_decision_candidate_summary.build_candidate_summary(
            event={"cwd": str(repo), "session_id": "isolated-candidate-session"},
            cwd=str(repo), repo=str(repo), ledger=None,
            workspace_guard=lambda **kwargs: None,
            committed_baseline_resolver=lambda *args: None,
            committed_base_ref_resolver=lambda *args: None,
        )
        # 未入选路径可属于其它会话；冻结层仍须证明生效的输入上下文。
        for candidate in summary["candidates"]:
            candidate["included_in_candidate_set"] = candidate["path"] == selected_candidate_path
        summary["candidate_fingerprint"] = stop_decision_candidate_summary.compute_candidate_fingerprint([
            (candidate["path"], candidate["content_sha256"])
            for candidate in summary["candidates"] if candidate["included_in_candidate_set"]
        ])
        return summary

    summary = build_selected_candidate_summary()
    assert [candidate["path"] for candidate in summary["candidates"] if candidate["included_in_candidate_set"]] == [selected_candidate_path]
    token_payload = {
        "token": "0123456789abcdef", "session_id": "isolated-candidate-session", "worktree": str(repo),
        "candidate_summary": summary, "candidate_fingerprint": summary["candidate_fingerprint"],
    }
    return freeze_review_candidate_snapshot(
        token_payload=token_payload, repo=str(repo), candidate_summary_builder=build_selected_candidate_summary,
    )


@pytest.fixture
def isolated_symlink_candidate_repository(tmp_path):
    repo = tmp_path / "isolated-symlink-candidate-repository"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "隔离符号链接测试")
    git(repo, "config", "user.email", "isolated-symlink@example.invalid")
    (repo / "baseline.py").write_text("baseline = True\n")
    git(repo, "add", "baseline.py")
    git(repo, "commit", "-qm", "test: 隔离符号链接基线")
    return repo


@pytest.mark.parametrize("target_state", [
    "never_tracked_and_absent", "tracked_then_deleted", "tracked_then_deleted_and_staged",
    "present_and_untracked", "present_and_tracked",
])
def test_symlink_candidate_snapshot_preserves_capturable_target_and_deleted_target_absence(isolated_symlink_candidate_repository, target_state):
    repo = isolated_symlink_candidate_repository
    target = repo / "candidate-target.py"
    if target_state != "never_tracked_and_absent":
        target.write_text("target_value = 2\n")
    if target_state in {"tracked_then_deleted", "tracked_then_deleted_and_staged", "present_and_tracked"}:
        git(repo, "add", "candidate-target.py")
        git(repo, "commit", "-qm", "test: 记录可删除的链接目标")
    if target_state.startswith("tracked_then_deleted"):
        target.unlink()
        if target_state.endswith("and_staged"):
            git(repo, "add", "--all", "--", "candidate-target.py")
    (repo / "candidate-link.py").symlink_to("candidate-target.py")
    prior_index_tree = git(repo, "write-tree")
    prior_status = git(repo, "status", "--porcelain")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "candidate-link.py")

    checkpoint = snapshot["checkpoint_commit"]
    assert git(repo, "ls-tree", checkpoint, "--", "candidate-link.py").startswith("120000 ")
    assert git(repo, "show", checkpoint + ":candidate-link.py") == "candidate-target.py"
    if target.exists():
        assert git(repo, "show", checkpoint + ":candidate-target.py") == "target_value = 2"
    else:
        assert git(repo, "ls-tree", checkpoint, "--", "candidate-target.py") == ""
    assert git(repo, "write-tree") == prior_index_tree
    assert git(repo, "status", "--porcelain") == prior_status


@pytest.mark.parametrize("outside_target_exists", [False, True])
def test_symlink_candidate_snapshot_rejects_target_outside_repository(isolated_symlink_candidate_repository, outside_target_exists):
    repo = isolated_symlink_candidate_repository
    outside_target = repo.parent / "outside-candidate-target.py"
    if outside_target_exists:
        outside_target.write_text("outside_value = 99\n")
    (repo / "candidate-link.py").symlink_to(outside_target)

    with pytest.raises(ValueError, match="仓库外"):
        _freeze_isolated_snapshot_for_selected_candidate_path(repo, "candidate-link.py")


@pytest.mark.parametrize("policy_exclusion_reason", ["self_excluded", "ownership_excluded", "git_ignored"])
def test_snapshot_captures_effective_review_scope_policy_outside_review_candidates(isolated_symlink_candidate_repository, policy_exclusion_reason):
    repo = isolated_symlink_candidate_repository
    policy = repo / ".review-validate-fix-ignore"
    if policy_exclusion_reason == "git_ignored":
        (repo / ".gitignore").write_text(".review-validate-fix-ignore\n")
        git(repo, "add", ".gitignore")
    else:
        policy.write_text(".review-validate-fix-ignore\n" if policy_exclusion_reason == "self_excluded" else "old-policy-\n")
        git(repo, "add", ".review-validate-fix-ignore")
    (repo / "excluded-source.py").write_text("excluded_value = 2\n")
    git(repo, "add", "excluded-source.py")
    git(repo, "commit", "-qm", "test: 记录排除策略的旧输入")
    effective_policy = (".review-validate-fix-ignore\n" if policy_exclusion_reason == "self_excluded" else "") + "excluded-\n"
    policy.write_text(effective_policy)
    (repo / "baseline.py").write_text("baseline = False\n")
    prior_head = git(repo, "rev-parse", "HEAD")
    prior_index_tree = git(repo, "write-tree")
    prior_status = git(repo, "status", "--porcelain", "--ignored")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    assert git(repo, "show", snapshot["checkpoint_commit"] + ":.review-validate-fix-ignore") == effective_policy.strip()
    assert git(repo, "show", snapshot["checkpoint_commit"] + ":baseline.py") == "baseline = False"
    assert ".review-validate-fix-ignore" in snapshot["packet_context_only_paths"]
    assert "baseline.py" not in snapshot["packet_context_only_paths"]
    binding_text = git(repo, "show", "-s", "--format=%B", snapshot["checkpoint_commit"])
    from core.review_start_boundary.review_candidate_snapshot import SNAPSHOT_BINDING_PREFIX
    binding = json.loads(next(line[len(SNAPSHOT_BINDING_PREFIX):] for line in binding_text.splitlines() if line.startswith(SNAPSHOT_BINDING_PREFIX)))
    assert binding["packet_context_only_paths"] == snapshot["packet_context_only_paths"]
    assert git(repo, "rev-parse", "HEAD") == prior_head
    assert git(repo, "write-tree") == prior_index_tree
    assert git(repo, "status", "--porcelain", "--ignored") == prior_status


@pytest.mark.parametrize("policy_state", ["never_existed", "tracked_then_deleted", "tracked_then_deleted_and_staged"])
def test_snapshot_captures_review_scope_policy_absence_without_a_candidate_path(isolated_symlink_candidate_repository, policy_state):
    repo = isolated_symlink_candidate_repository
    if policy_state != "never_existed":
        policy = repo / ".review-validate-fix-ignore"
        policy.write_text("excluded-\n")
        git(repo, "add", ".review-validate-fix-ignore")
        git(repo, "commit", "-qm", "test: 记录随后删除的排除策略")
        policy.unlink()
        if policy_state.endswith("and_staged"):
            git(repo, "add", "--all", "--", ".review-validate-fix-ignore")
    (repo / "baseline.py").write_text("baseline = False\n")
    prior_index_tree = git(repo, "write-tree")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    assert git(repo, "ls-tree", snapshot["checkpoint_commit"], "--", ".review-validate-fix-ignore") == ""
    assert git(repo, "write-tree") == prior_index_tree


@pytest.mark.parametrize("policy_link_shape", [
    "relative_file", "absolute_file", "relative_link_chain", "absolute_link_chain",
    "intermediate_directory_link", "absolute_intermediate_directory_link",
])
def test_snapshot_captures_review_scope_policy_internal_link_dependencies(isolated_symlink_candidate_repository, policy_link_shape):
    repo = isolated_symlink_candidate_repository
    policy = repo / ".review-validate-fix-ignore"
    policy.write_text("old-policy-\n")
    git(repo, "add", ".review-validate-fix-ignore")
    git(repo, "commit", "-qm", "test: 记录链接替换前的旧策略")
    policy.unlink()
    target_directory = repo / "policy-context-directory"
    target_directory.mkdir()
    target_policy = target_directory / "active-policy"
    unrelated_policy_sibling = target_directory / "unrelated-policy-background.py"
    unrelated_policy_sibling.write_text("unrelated_background = 99\n")
    effective_policy = ".review-validate-fix-ignore\npolicy-\nexcluded-\n"
    target_policy.write_text(effective_policy)
    symlinks = []
    if policy_link_shape.endswith("intermediate_directory_link"):
        directory_link = repo / "policy-directory-link"
        directory_link.symlink_to(target_directory if policy_link_shape.startswith("absolute") else target_directory.name)
        symlinks.append(directory_link)
        policy_target = directory_link / target_policy.name
    elif policy_link_shape.endswith("link_chain"):
        first_link = repo / "policy-first-link"
        second_link = repo / "policy-second-link"
        first_link.symlink_to(second_link if policy_link_shape.startswith("absolute") else second_link.name)
        second_link.symlink_to(target_policy if policy_link_shape.startswith("absolute") else target_policy.relative_to(repo))
        symlinks.extend([first_link, second_link])
        policy_target = first_link
    else:
        policy_target = target_policy
    policy.symlink_to(policy_target if policy_link_shape.startswith("absolute") else policy_target.relative_to(repo))
    symlinks.append(policy)
    # 策略及其依赖均不属于 review candidates，并可被 Git ignore。
    (repo / ".gitignore").write_text("policy-*\n.review-validate-fix-ignore\n")
    (repo / "baseline.py").write_text("baseline = False\n")
    prior_head = git(repo, "rev-parse", "HEAD")
    prior_index_tree = git(repo, "write-tree")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    checkpoint = snapshot["checkpoint_commit"]
    assert git(repo, "show", checkpoint + ":" + target_policy.relative_to(repo).as_posix()) == effective_policy.strip()
    for symlink in symlinks:
        relative_path = symlink.relative_to(repo).as_posix()
        assert git(repo, "ls-tree", checkpoint, "--", relative_path).startswith("120000 ")
        assert git(repo, "show", checkpoint + ":" + relative_path) == str(symlink.readlink())
        assert relative_path in snapshot["packet_context_only_paths"]
    assert target_policy.relative_to(repo).as_posix() in snapshot["packet_context_only_paths"]
    assert git(repo, "ls-tree", checkpoint, "--", unrelated_policy_sibling.relative_to(repo).as_posix()) == ""
    assert git(repo, "rev-parse", "HEAD") == prior_head
    assert git(repo, "write-tree") == prior_index_tree


@pytest.mark.parametrize("unsafe_policy_link", ["outside_file", "outside_intermediate_directory", "self_cycle", "indirect_cycle"])
def test_snapshot_rejects_unsafe_review_scope_policy_links_even_outside_candidates(isolated_symlink_candidate_repository, unsafe_policy_link):
    repo = isolated_symlink_candidate_repository
    policy = repo / ".review-validate-fix-ignore"
    if unsafe_policy_link.startswith("outside"):
        outside_directory = repo.parent / "outside-policy-directory"
        outside_directory.mkdir()
        outside_policy = outside_directory / "policy"
        outside_policy.write_text("outside-\n")
        if unsafe_policy_link == "outside_file":
            policy.symlink_to(outside_policy)
        else:
            (repo / "policy-directory-link").symlink_to(outside_directory)
            policy.symlink_to("policy-directory-link/policy")
    elif unsafe_policy_link == "self_cycle":
        policy.symlink_to(policy.name)
    else:
        policy.symlink_to("policy-first-link")
        (repo / "policy-first-link").symlink_to("policy-second-link")
        (repo / "policy-second-link").symlink_to("policy-first-link")
    (repo / "baseline.py").write_text("baseline = False\n")

    with pytest.raises(ValueError, match="仓库外|循环"):
        _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")


def test_snapshot_captures_raw_effective_review_scope_policy_without_git_clean_filters(isolated_symlink_candidate_repository):
    repo = isolated_symlink_candidate_repository
    (repo / ".gitattributes").write_text(".review-validate-fix-ignore filter=review-scope-policy\n")
    git(repo, "config", "filter.review-scope-policy.clean", "printf 'filtered-out-\\n'")
    effective_policy = ".review-validate-fix-ignore\nreal-excluded-\n"
    (repo / ".review-validate-fix-ignore").write_text(effective_policy)
    (repo / "baseline.py").write_text("baseline = False\n")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    assert git(repo, "show", snapshot["checkpoint_commit"] + ":.review-validate-fix-ignore") == effective_policy.strip()


@pytest.mark.parametrize("policy_target_state", ["never_existed", "tracked_then_deleted", "tracked_then_deleted_and_staged"])
def test_snapshot_captures_dangling_review_scope_policy_target_absence(isolated_symlink_candidate_repository, policy_target_state):
    repo = isolated_symlink_candidate_repository
    policy_target = repo / "policy-context-target"
    if policy_target_state != "never_existed":
        policy_target.write_text("old-excluded-\n")
        git(repo, "add", "policy-context-target")
        git(repo, "commit", "-qm", "test: 记录随后删除的策略目标")
        policy_target.unlink()
        if policy_target_state.endswith("and_staged"):
            git(repo, "add", "--all", "--", "policy-context-target")
    (repo / ".review-validate-fix-ignore").symlink_to(policy_target.name)
    (repo / "baseline.py").write_text("baseline = False\n")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    checkpoint = snapshot["checkpoint_commit"]
    assert git(repo, "show", checkpoint + ":.review-validate-fix-ignore") == policy_target.name
    assert git(repo, "ls-tree", checkpoint, "--", policy_target.name) == ""
    assert {".review-validate-fix-ignore", policy_target.name}.issubset(snapshot["packet_context_only_paths"])


def test_snapshot_does_not_capture_a_transient_excluded_policy_after_candidate_summary(isolated_symlink_candidate_repository):
    from core.review_start_boundary.review_candidate_snapshot import freeze_review_candidate_snapshot
    import stop_decision_candidate_summary

    repo = isolated_symlink_candidate_repository
    policy = repo / ".review-validate-fix-ignore"
    original_policy = ".review-validate-fix-ignore\noriginal-excluded-\n"
    policy.write_text(original_policy)
    (repo / "baseline.py").write_text("baseline = False\n")
    summary = stop_decision_candidate_summary.build_candidate_summary(
        event={"cwd": str(repo), "session_id": "isolated-policy-session"}, cwd=str(repo), repo=str(repo), ledger=None,
        workspace_guard=lambda **kwargs: None,
        committed_baseline_resolver=lambda *args: None, committed_base_ref_resolver=lambda *args: None,
    )
    token_payload = {
        "token": "0123456789abcdef", "session_id": "isolated-policy-session", "worktree": str(repo),
        "candidate_summary": summary, "candidate_fingerprint": summary["candidate_fingerprint"],
    }
    summary_calls = []
    def candidate_summary_with_transient_policy_after_first_read(_fixed_snapshot):
        summary_calls.append(True)
        policy.write_text(".review-validate-fix-ignore\nwrong-transient-excluded-\n" if len(summary_calls) == 1 else original_policy)
        return summary

    snapshot = freeze_review_candidate_snapshot(
        token_payload=token_payload, repo=str(repo), candidate_summary_builder=candidate_summary_with_transient_policy_after_first_read,
    )

    assert git(repo, "show", snapshot["checkpoint_commit"] + ":.review-validate-fix-ignore") == original_policy.strip()


@pytest.mark.parametrize("empty_directory_relative_path", ["real-policy/deep", "real-policy/nested/deep"])
def test_snapshot_preserves_empty_context_directories_before_symlink_parent_traversal(isolated_symlink_candidate_repository, empty_directory_relative_path):
    import build_review_packet

    repo = isolated_symlink_candidate_repository
    excluded_source = repo / "excluded-source.py"
    excluded_source.write_text("excluded_value = 1\n")
    git(repo, "add", "excluded-source.py")
    git(repo, "commit", "-qm", "test: 记录排除内容的比较基线")
    comparison_base = git(repo, "rev-parse", "HEAD")
    empty_directory = repo / empty_directory_relative_path
    empty_directory.mkdir(parents=True)
    directory_link = repo / "directory-link"
    directory_link.symlink_to(empty_directory_relative_path)
    excluded_source.write_text("excluded_value = 2\n")
    git(repo, "add", "directory-link", "excluded-source.py")
    git(repo, "commit", "-qm", "test: 记录非候选目录链接和待排除的已提交内容")
    policy_target = empty_directory.parent / "policy.txt"
    effective_policy = ".review-validate-fix-ignore\nreal-policy/\nexcluded-\n"
    policy_target.write_text(effective_policy)
    (repo / ".review-validate-fix-ignore").symlink_to("directory-link/../policy.txt")
    (repo / "baseline.py").write_text("baseline = False\n")
    unrelated_context_sibling = empty_directory.parent / "unrelated-policy-background.py"
    unrelated_context_sibling.write_text("unrelated_value = 99\n")
    assert (repo / ".review-validate-fix-ignore").read_text() == effective_policy
    assert not any(empty_directory.iterdir())
    prior_head = git(repo, "rev-parse", "HEAD")
    prior_index_tree = git(repo, "write-tree")
    prior_status = git(repo, "status", "--porcelain")

    snapshot = _freeze_isolated_snapshot_for_selected_candidate_path(repo, "baseline.py")

    checkpoint = snapshot["checkpoint_commit"]
    empty_tree_entry = git(repo, "ls-tree", checkpoint, "--", empty_directory_relative_path)
    assert empty_tree_entry.startswith("040000 tree ")
    assert git(repo, "ls-tree", checkpoint + ":" + empty_directory_relative_path) == ""
    assert git(repo, "ls-tree", checkpoint, "--", unrelated_context_sibling.relative_to(repo).as_posix()) == ""
    frozen_source = build_review_packet.FrozenCandidatePacketGitSource(repo, checkpoint)
    frozen_policy, _ = frozen_source.read_frozen_blob_text_and_metadata(
        ".review-validate-fix-ignore", 10000, follow_symlinks=True,
    )
    assert frozen_policy == effective_policy
    packet, metadata = build_review_packet.build_packet(
        repo, None, None, 10000, ["baseline.py"], [], allow_missing_session_context=True,
        base_ref_override=comparison_base, frozen_candidate_commit=checkpoint,
        frozen_packet_context_only_paths=snapshot["packet_context_only_paths"],
    )
    assert "excluded-source.py" not in packet
    assert "+baseline = False" in packet
    assert "excluded-" in metadata["excluded_path_prefixes"]
    assert policy_target.relative_to(repo).as_posix() in snapshot["packet_context_only_paths"]
    assert "baseline.py" not in snapshot["packet_context_only_paths"]
    assert git(repo, "rev-parse", "HEAD") == prior_head
    assert git(repo, "write-tree") == prior_index_tree
    assert git(repo, "status", "--porcelain") == prior_status
