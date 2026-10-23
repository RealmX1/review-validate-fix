"""开审分界门禁：不得把 token 状态或 hook 合成消息冒充独立用户 turn。"""

from __future__ import annotations

import importlib
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))


@pytest.fixture(autouse=True)
def isolate_runtime_state(tmp_path, monkeypatch):
    from core.host_adapter.current_agent_session_identity_from_environment import CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES
    for name in CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local-data"))


def test_start_without_independent_boundary_does_not_acknowledge_or_prepare(tmp_path):
    cli = importlib.import_module("rvf_agent_decision_cli")
    tokens = importlib.import_module("rvf_decision_token_store")
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    roots = cli.AgentDecisionRoots(token_root=tmp_path / "tokens", cooldown_root=tmp_path / "cooldowns")
    minted = tokens.mint_decision_token(
        worktree=str(repo), session_id="synthetic-session", candidate_fingerprint="synthetic-fingerprint",
        root=roots.token_root, cooldown_root=roots.cooldown_root,
        circuit_breaker_root_dir=tmp_path / "breaker",
    )
    prepare_calls = []

    def prepare(**kwargs):
        prepare_calls.append(kwargs)
        return {"status": "failed", "error": "不应进入准备阶段"}

    result = cli.run_start(token=minted.token, reason="条件已满足", roots=roots, prepare_pipeline=prepare)
    assert result.status == "review_start_boundary_pending"
    assert tokens.read_decision_token(minted.token, root=roots.token_root).state == "minted"
    assert prepare_calls == []


def git(repo, *arguments, env=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo, env=env,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def valid_boundary(tmp_path, monkeypatch):
    cli = importlib.import_module("rvf_agent_decision_cli")
    tokens = importlib.import_module("rvf_decision_token_store")
    gate = importlib.import_module("review_start_boundary_gate")
    summaries = importlib.import_module("stop_decision_candidate_summary")
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    # 基线变动反例需要 main；不能依赖机器或调用方的 init.defaultBranch。
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "隔离分界测试")
    git(repo, "config", "user.email", "boundary@example.invalid")
    (repo / "a.py").write_text("value = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "test: 合成基线")
    (repo / "a.py").write_text("value = 2\n")
    transcript = tmp_path / "synthetic-native-transcript.jsonl"
    event = {"cwd": str(repo), "session_id": "synthetic-session", "transcript_path": str(transcript)}
    summary = summaries.build_candidate_summary(event=event, cwd=str(repo), repo=str(repo), ledger=None)
    token = tokens.mint_decision_token(worktree=str(repo), session_id=event["session_id"],
                                      candidate_summary=summary, candidate_fingerprint=summary["candidate_fingerprint"]).token
    snapshot_environment = dict(os.environ, GIT_INDEX_FILE=str(tmp_path / "isolated-checkpoint-index"))
    git(repo, "read-tree", "HEAD", env=snapshot_environment)
    git(repo, "add", "a.py", env=snapshot_environment)
    tree = git(repo, "write-tree", env=snapshot_environment)
    text = tokens.render_decision_point_out_of_band_boundary_marker(token=token)
    records = [
        {"type": "session_meta", "payload": {"id": event["session_id"], "cli_version": "0.163.0", "originator":"codex-tui", "source":"cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "independent-turn", "turn_attribution": {"turn_trigger": "user"}}},
        {"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(), "payload": {"type": "item_completed", "turn_id": "independent-turn", "item": {"type": "UserMessage", "id": "native-message", "content": [{"type": "text", "text": text}]}}},
    ]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    receipt = {
        "schema": "rvf-independent-user-turn-boundary/v1", "decision_token": token,
        "session_id": event["session_id"], "worktree": str(repo), "transcript_path": str(transcript),
        "native_user_message_id": "native-message", "native_turn_id": "independent-turn",
        "boundary_message_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source": "rvf_automatic_decision_message", "candidate_fingerprint": summary["candidate_fingerprint"],
        "worktree_head_commit_sha": git(repo, "rev-parse", "HEAD"),
        "comparison_base_tip": summary.get("committed_round_baseline"),
        "comparison_base_ref": summary.get("committed_round_base_ref"),
        "comparison_base_ref_tip": git(repo, "rev-parse", summary["committed_round_base_ref"]) if summary.get("committed_round_base_ref") else None,
        "checkpoint_tree": tree,
    }
    from core.review_start_boundary.review_start_boundary_verification import CHECKPOINT_BINDING_PREFIX, CHECKPOINT_BINDING_FIELDS, candidate_filesystem_state_sha256
    receipt["candidate_filesystem_state_sha256"] = candidate_filesystem_state_sha256(str(repo), summary["candidates"])
    binding = {key: receipt.get(key) for key in CHECKPOINT_BINDING_FIELDS}
    receipt["checkpoint_commit"] = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", CHECKPOINT_BINDING_PREFIX + json.dumps(binding, ensure_ascii=False))
    monkeypatch.setenv("CODEX_THREAD_ID", event["session_id"])
    path = gate.review_start_boundary_receipt_path(token)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt))
    from independent_review_start_boundary_fixture import configure_synthetic_live_boundary_host_observation
    host_environment = configure_synthetic_live_boundary_host_observation(receipt_path=path, repo=repo, environment=os.environ.copy())
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", host_environment["CODEX_RVF_CLINE_KANBAN_TASK_CMD"])
    return {"repo": repo, "token": token, "receipt": receipt, "transcript": transcript, "records": records,
            "payload": tokens.read_decision_token(token).payload, "gate": gate, "cli": cli, "tokens": tokens}


def test_real_git_snapshot_and_native_independent_message_allow_start(valid_boundary):
    fixture = valid_boundary
    calls = []
    def prepare(**kwargs):
        calls.append(kwargs)
        return {"status": "failed", "error": "只验证到准备边界"}
    result = fixture["cli"].run_start(token=fixture["token"], reason="自主开审", prepare_pipeline=prepare)
    assert result.status == "start_failed"
    assert len(calls) == 1
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "acknowledged"


def test_base_advancement_keeps_registered_boundary(valid_boundary):
    fixture = valid_boundary
    repo = fixture["repo"]
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    for index in range(2):
        tip = git(repo, "commit-tree", tree, "-p", git(repo, "rev-parse", "main"),
                  "-m", f"test: 其它任务推进 base {index}")
        git(repo, "update-ref", "refs/heads/main", tip)
    git(repo, "update-ref", "--no-deref", "HEAD", head)
    calls = []
    def prepare(**kwargs):
        calls.append(kwargs)
        return {"status": "failed", "error": "只验证开审条件"}
    result = fixture["cli"].run_start(token=fixture["token"], reason="固定候选开审", prepare_pipeline=prepare)
    assert result.status == "start_failed"
    assert len(calls) == 1


def test_concurrent_start_prepares_same_token_once(valid_boundary):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    fixture = valid_boundary
    entered, release = Event(), Event()
    calls = []
    def prepare(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        return {"status": "failed", "error": "合成准备结果"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fixture["cli"].run_start, token=fixture["token"], reason="首次启动", prepare_pipeline=prepare)
        try:
            assert entered.wait(10)
            second = fixture["cli"].run_start(token=fixture["token"], reason="重复启动", prepare_pipeline=prepare)
            assert second.status == "start_in_flight"
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result().status == "start_failed"


@pytest.mark.parametrize("token", ["not-a-token", "0123456789abcdef"])
def test_start_cli_invalid_or_missing_token_returns_structured_error(token):
    cli = importlib.import_module("rvf_agent_decision_cli")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIRECTORY / "rvf_agent_decision_cli.py"),
         "start", "--token", token, "--reason", "检查非法或缺失 token 的回执"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert completed.returncode == cli.EXIT_TOKEN_STATE
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    result_lines = [line for line in completed.stderr.splitlines()
                    if line.startswith("RVF_AGENT_RESULT_JSON: ")]
    assert len(result_lines) == 1
    result = json.loads(result_lines[0].removeprefix("RVF_AGENT_RESULT_JSON: "))
    assert result["command"] == "start"
    assert result["status"] == "error"
    assert result["exit_code"] == cli.EXIT_TOKEN_STATE
    assert result["token"] == token


@pytest.mark.parametrize("comparison_base_ref", [None, "main"])
@pytest.mark.parametrize("advance_before", ["manifest", "packet"])
def test_prepare_keeps_fixed_base_when_base_advances_during_packet(valid_boundary, tmp_path, monkeypatch, comparison_base_ref, advance_before):
    fixture = valid_boundary
    preparation = importlib.import_module("prepare_review_run")
    fixture["tokens"].acknowledge_decision_token(fixture["token"])
    repo = fixture["repo"]
    git(repo, "checkout", "--detach", "-q")
    fixed_base = fixture["receipt"]["comparison_base_ref_tip"]
    with fixture["transcript"].open("a") as transcript:
        transcript.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "synthetic-a-py-write",
                "input": "*** Begin Patch\n*** Update File: a.py\n@@\n-value = 1\n+value = 2\n*** End Patch\n"}}) + "\n")
    original_run = preparation.run
    advanced = []
    def run(command, **kwargs):
        advancement_trigger_script = preparation.SESSION_MANIFEST if advance_before == "manifest" else preparation.BUILD_PACKET
        if len(command) > 1 and command[1] == str(advancement_trigger_script):
            tip = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-p", fixed_base,
                      "-m", "test: prepare 期间推进 base")
            git(repo, "update-ref", "refs/heads/main", tip)
            advanced.append(tip)
        return original_run(command, **kwargs)
    monkeypatch.setattr(preparation, "run", run)
    context = tmp_path / "scope-of-work.md"
    context.write_text("审查 a.py 中 value 的任务改动。\n")
    prepared = preparation.prepare_run(repo=repo, session_context=context, session_manifest=None,
        transcript=fixture["transcript"], base_dir=tmp_path / "runs", max_file_bytes=200_000, max_packet_bytes=0,
        base_ref=comparison_base_ref, primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
        scope_contract_extensions={"review_start_boundary_receipt": fixture["receipt"]})
    assert len(advanced) == 1
    contract = json.loads(Path(prepared["scope_contract"]).read_text())
    assert contract["reviewed_base_tip"] == fixed_base
    assert contract["comparison_base_tip"] == fixed_base
    metadata = json.loads(Path(prepared["review_packet_metadata"]).read_text())
    manifest = json.loads(Path(prepared["session_manifest_file"]).read_text())
    assert metadata["owned_diff_base_ref"] == fixed_base
    assert manifest["committed_round_baseline"] == fixed_base
    assert git(repo, "rev-parse", "main") == advanced[0]
    assert "value = 2" in Path(prepared["review_packet"]).read_text()


def test_default_prepare_allocates_scope_using_boundary_base_tip(valid_boundary, monkeypatch):
    fixture = valid_boundary
    git(fixture["repo"], "checkout", "--detach", "-q")
    cli = fixture["cli"]
    captured = []
    def allocate(**kwargs):
        captured.append(kwargs)
        return {"status": cli.AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE,
                "degradation_reason": "合成空范围", "tracker_scope_path": None,
                "tracker_lease_id": None, "tracker_scope_hash": None, "tracker_unit_count": 0,
                "allocator_status": "empty", "error": None, "session_manifest_has_owned_paths": True}
    monkeypatch.setattr(cli, "allocate_agentic_initiation_tracker_review_scope", allocate)
    result = cli.default_prepare_pipeline(token_record=fixture["tokens"].read_decision_token(fixture["token"]),
        repo=str(fixture["repo"]), session_id=fixture["payload"]["session_id"],
        transcript_path=str(fixture["transcript"]), now=datetime.now(timezone.utc))
    assert result["status"] == cli.PREPARE_STATUS_NO_UNREVIEWED_SCOPE
    assert captured[0]["committed_base_ref"] == fixture["receipt"]["comparison_base_ref_tip"]


@pytest.mark.parametrize("changed", ["candidate", "head", "session", "message_hash", "checkpoint", "hook", "later_turn", "rewind", "comparison_base", "merged_prompt", "file_mode"])
def test_stale_or_synthetic_boundary_is_rejected_before_prepare(valid_boundary, changed):
    fixture = valid_boundary
    receipt = copy.deepcopy(fixture["receipt"])
    records = copy.deepcopy(fixture["records"])
    if changed == "file_mode":
        (fixture["repo"] / "a.py").chmod(0o755)
    elif changed == "candidate":
        (fixture["repo"] / "a.py").write_text("value = 3\n")
    elif changed == "head":
        git(fixture["repo"], "commit", "--allow-empty", "-qm", "test: 分界后复工")
    elif changed == "session":
        receipt["session_id"] = "another-session"
    elif changed == "message_hash":
        receipt["boundary_message_sha256"] = "0" * 64
    elif changed == "checkpoint":
        receipt["checkpoint_commit"] = receipt["worktree_head_commit_sha"]
        receipt["checkpoint_tree"] = git(fixture["repo"], "rev-parse", "HEAD^{tree}")
    elif changed == "hook":
        records[-1] = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<hook_prompt>" + fixture["token"] + "</hook_prompt>"}]}}
    elif changed == "later_turn":
        records.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "implementation-resumed", "turn_attribution": {"turn_trigger": "user"}}})
    elif changed == "rewind":
        records.append({"type": "event_msg", "payload": {"type": "thread_reverted"}})
    elif changed == "comparison_base":
        receipt["comparison_base_tip"] = "different-baseline"
    elif changed == "merged_prompt":
        text = records[-1]["payload"]["item"]["content"][0]["text"] + "\n用户的其它输入"
        records[-1]["payload"]["item"]["content"][0]["text"] = text
        receipt["boundary_message_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    fixture["transcript"].write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).write_text(json.dumps(receipt))
    calls = []
    result = fixture["cli"].run_start(token=fixture["token"], reason="条件满足", prepare_pipeline=lambda **kwargs: calls.append(kwargs))
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"
    assert calls == []


def test_direct_prepare_cannot_create_run_without_boundary(tmp_path):
    prepare = importlib.import_module("prepare_review_run")
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    git(repo, "init", "-q")
    with pytest.raises(ReviewStartBoundaryNotVerified):
        prepare.prepare_run(repo=repo, session_context=None, session_manifest=None, transcript=None,
                            base_dir=tmp_path / "forbidden-runs", max_file_bytes=100, max_packet_bytes=0,
                            primary_files=[], background_files=[], exclude_path_prefixes=[])
    assert not (tmp_path / "forbidden-runs").exists()


def test_deferred_recovery_requests_new_message_without_explicit_user_provenance(valid_boundary):
    fixture = valid_boundary
    request = importlib.import_module("rvf_review_start_boundary_request")
    fixture["cli"].run_decline(token=fixture["token"], decline_kind="deferred_until_condition", reason="等待运行时验收")
    sent = []
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    kwargs = dict(deferred_decision_token=fixture["token"], reason="前置条件已满足",
                  now=now, candidate_summary_builder=lambda event, repo: dict(fixture["payload"]["candidate_summary"]),
                  boundary_sender=lambda **fields: sent.append(fields) or {"terminal_delivery_status": "accepted_pending_submit_confirmation"})
    first = request.run_request_review_start_boundary(**kwargs)
    assert first.status == "review_start_boundary_pending", first
    assert first.token != fixture["token"]
    payload = fixture["tokens"].read_decision_token(first.token).payload
    assert payload["state"] == "minted"
    assert payload["initiated_by_explicit_user_request"] is False
    assert not payload.get("explicit_user_request_provenance")
    assert payload["boundary_request_source_token"] == fixture["token"]
    second = request.run_request_review_start_boundary(**kwargs)
    assert second.token == first.token, second
    assert len(sent) == 2  # 同一 token/key；进程重启也只重试投递，不另铸。
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "declined"
    blocked = fixture["cli"].run_start(token=first.token, reason="回执已 accepted")
    assert blocked.status == "review_start_boundary_pending"
    assert fixture["tokens"].read_decision_token(first.token).state == "minted"


def test_structural_decline_cannot_be_reopened_by_agent_reason(valid_boundary):
    fixture = valid_boundary
    request = importlib.import_module("rvf_review_start_boundary_request")
    fixture["cli"].run_decline(token=fixture["token"], decline_kind="structural_not_needed", reason="不需要审查")
    calls = []
    result = request.run_request_review_start_boundary(deferred_decision_token=fixture["token"], reason="开发者想审",
                                                      boundary_sender=lambda **kw: calls.append(kw))
    assert result.status == "error"
    assert calls == []
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "declined"


def test_removed_reopen_flag_is_rejected():
    cli = importlib.import_module("rvf_agent_decision_cli")
    with pytest.raises(SystemExit) as exc:
        cli.build_argument_parser().parse_args(["start", "--reopen-own-declined-decision", "--reason", "Agent 自造理由"])
    assert exc.value.code == 2


@pytest.mark.parametrize("dispatch_entry", ["execute", "detached"])
def test_direct_dispatch_has_no_reviewer_side_effect_without_boundary(tmp_path, dispatch_entry):
    dispatch = importlib.import_module("dispatch_reviewers")
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    artifacts = tmp_path / "forbidden-reviewer-artifacts"
    with pytest.raises(ReviewStartBoundaryNotVerified):
        if dispatch_entry == "execute":
            dispatch.execute_plan({}, repo=str(tmp_path), review_packet=None, session_context=None, scope_contract=None,
                                  run_id="synthetic-run", run_dir=str(tmp_path / "forbidden-run"), artifacts_dir=artifacts)
        else:
            dispatch.launch_detached_dispatch(None, ledger=None, reviewers_dir=artifacts,
                                              repo=str(tmp_path), review_packet=None, session_context=None, scope_contract=None)
    assert not artifacts.exists()


def test_delayed_delivery_outside_token_ttl_cannot_start(valid_boundary):
    fixture = valid_boundary
    fixture["records"][-1]["timestamp"] = (datetime.fromisoformat(fixture["payload"]["expires_at"].replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat()
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    result = fixture["cli"].run_start(token=fixture["token"], reason="迟到投递")
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_delivery_status_without_native_receipt_cannot_start(valid_boundary, monkeypatch):
    fixture = valid_boundary
    client = importlib.import_module("cline_kanban_client")
    fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).unlink()
    fixture["tokens"].record_out_of_band_delivery_status(fixture["token"], status="delivered_and_submit_confirmed",
        detail={"kanban_task_id": "synthetic-task", "kanban_project_path": str(fixture["repo"]), "idempotency_key": "synthetic-key"})
    monkeypatch.setattr(client, "read_task_message_terminal_delivery_status", lambda **kw: {"terminal_delivery_status": "delivered_and_submit_confirmed"})
    result = fixture["cli"].run_start(token=fixture["token"], reason="宿主声称 submitted")
    assert result.status == "review_start_boundary_pending"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_one_boundary_cannot_authorize_two_runs_even_after_restart(valid_boundary, tmp_path):
    fixture = valid_boundary
    verified = fixture["gate"].require_review_start_boundary(token_payload=fixture["payload"], repo=str(fixture["repo"]))
    first = fixture["gate"].bind_review_start_boundary_to_one_run(receipt=verified, run_id="first-run", run_dir=tmp_path / "first-run")
    reloaded_gate = importlib.reload(fixture["gate"])
    assert reloaded_gate.bind_review_start_boundary_to_one_run(receipt=verified, run_id="first-run", run_dir=tmp_path / "first-run") == first
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        reloaded_gate.bind_review_start_boundary_to_one_run(receipt=verified, run_id="second-run", run_dir=tmp_path / "second-run")
    assert not (tmp_path / "first-run").exists()  # 唯一绑定仍是轻量意图，不创建正式 run。
    assert not (tmp_path / "second-run").exists()


def test_verified_boundary_reaches_real_prepare_and_gates_dispatch_again(valid_boundary, tmp_path):
    fixture = valid_boundary
    prepare = importlib.import_module("prepare_review_run")
    fixture["tokens"].acknowledge_decision_token(fixture["token"])
    context = tmp_path / "synthetic-scope-of-work.md"
    context.write_text("## 本轮范围\n审查 a.py 的 value 更新。\n")
    result = prepare.prepare_run(repo=fixture["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "isolated-prepared-runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
        rvf_run_id="synthetic-real-prepare", rvf_run_dir=tmp_path / "isolated-real-run",
        scope_contract_extensions={"review_start_boundary_receipt": fixture["receipt"]})
    contract = json.loads(Path(result["scope_contract"]).read_text())
    assert contract["review_start_boundary_receipt"] == fixture["receipt"]
    assert fixture["gate"].require_prepared_run_review_start_boundary(repo=str(fixture["repo"]), scope_contract=result["scope_contract"],
        run_id="synthetic-real-prepare", run_dir=tmp_path / "isolated-real-run") == fixture["receipt"]
    (fixture["repo"] / "a.py").write_text("value = 99\n")
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        fixture["gate"].require_prepared_run_review_start_boundary(repo=str(fixture["repo"]), scope_contract=result["scope_contract"],
            run_id="synthetic-real-prepare", run_dir=tmp_path / "isolated-real-run")


def explicit_manual_command_boundary(fixture, monkeypatch, command_text):
    tokens = fixture["tokens"]
    tokens.mark_withdrawn(fixture["token"], reason="改用明确手动命令")
    manual = tokens.mint_decision_token(worktree=str(fixture["repo"]), session_id="synthetic-session",
        candidate_summary=fixture["payload"]["candidate_summary"], candidate_fingerprint=fixture["payload"]["candidate_fingerprint"],
        initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit")
    receipt = dict(fixture["receipt"], decision_token=manual.token, source="explicit_user_command")
    receipt["boundary_message_sha256"] = hashlib.sha256(command_text.encode()).hexdigest()
    fixture["records"][-1]["payload"]["item"]["content"][0]["text"] = command_text
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    from core.review_start_boundary.review_start_boundary_verification import CHECKPOINT_BINDING_PREFIX, CHECKPOINT_BINDING_FIELDS
    receipt["checkpoint_commit"] = git(fixture["repo"], "commit-tree", receipt["checkpoint_tree"], "-p", "HEAD", "-m",
        CHECKPOINT_BINDING_PREFIX + json.dumps({key:receipt.get(key) for key in CHECKPOINT_BINDING_FIELDS}))
    manual_receipt_path = fixture["gate"].review_start_boundary_receipt_path(manual.token)
    manual_receipt_path.write_text(json.dumps(receipt))
    from independent_review_start_boundary_fixture import configure_synthetic_live_boundary_host_observation
    host_environment = configure_synthetic_live_boundary_host_observation(receipt_path=manual_receipt_path, repo=fixture["repo"], environment=os.environ.copy())
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", host_environment["CODEX_RVF_CLINE_KANBAN_TASK_CMD"])
    return manual, receipt, host_environment


@pytest.mark.parametrize("command_text", [
    "/review-validate-fix",
    "$review-validate-fix",
    ":review-validate-fix",
    "now $review-validate-fix:review-validate-fix your own work.",
    "请现在 /review-validate-fix 审查这轮改动",
    "待办已完成\n/review-validate-fix:review-validate-fix 审查改动",
    "请运行 $rvf:review-validate-fix 审查改动",
])
def test_explicit_manual_command_needs_its_real_native_message_and_bound_checkpoint(valid_boundary, monkeypatch, command_text):
    fixture = valid_boundary
    tokens = fixture["tokens"]
    manual, receipt, host_environment = explicit_manual_command_boundary(fixture, monkeypatch, command_text)
    manual_payload = tokens.read_decision_token(manual.token).payload
    assert fixture["gate"].require_review_start_boundary(token_payload=manual_payload, repo=str(fixture["repo"]), receipt=receipt) == receipt
    from core.review_start_boundary.review_start_boundary_verification import CHECKPOINT_BINDING_PREFIX, CHECKPOINT_BINDING_FIELDS
    # 即使宿主误把同一条手动消息配给第二枚 token，也不能把旧分界充当新一轮起点。
    first_host_command = host_environment["CODEX_RVF_CLINE_KANBAN_TASK_CMD"]
    tokens.mark_withdrawn(manual.token, reason="合成测试：旧 token 作废")
    second_manual = tokens.mint_decision_token(worktree=str(fixture["repo"]), session_id="synthetic-session",
        candidate_summary=fixture["payload"]["candidate_summary"], candidate_fingerprint=fixture["payload"]["candidate_fingerprint"],
        initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit")
    second_receipt = dict(receipt, decision_token=second_manual.token)
    second_receipt["checkpoint_commit"] = git(fixture["repo"], "commit-tree", receipt["checkpoint_tree"], "-p", "HEAD", "-m",
        CHECKPOINT_BINDING_PREFIX + json.dumps({key:second_receipt.get(key) for key in CHECKPOINT_BINDING_FIELDS}))
    second_path = fixture["gate"].review_start_boundary_receipt_path(second_manual.token)
    second_path.write_text(json.dumps(second_receipt))
    from independent_review_start_boundary_fixture import configure_synthetic_live_boundary_host_observation
    second_environment = configure_synthetic_live_boundary_host_observation(receipt_path=second_path, repo=fixture["repo"], environment=os.environ.copy())
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", second_environment["CODEX_RVF_CLINE_KANBAN_TASK_CMD"])
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified, match="此原生消息已绑定另一 token"):
        fixture["gate"].require_review_start_boundary(token_payload=tokens.read_decision_token(second_manual.token).payload, repo=str(fixture["repo"]), receipt=second_receipt)
    assert tokens.read_decision_token(second_manual.token).state == "minted"
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", first_host_command)
    fixture["records"][-1]["payload"]["item"]["content"][0]["text"] = "运行时成果交接，已验收"
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        fixture["gate"].require_review_start_boundary(token_payload=manual_payload, repo=str(fixture["repo"]), receipt=receipt)


@pytest.mark.parametrize("command_text,expected_classification", [
    ("/review-validate-fix", "manual"),
    ("now $review-validate-fix:review-validate-fix your own work.", "manual"),
    ("请运行 $rvf:review-validate-fix 审查改动", "none"),
    ("/review-validate-fix:rvf-land", "none"),
    ("请运行 $review-validate-fix:rvf-reopen", "none"),
    ("请运行 /review-validate-fix:rvf-local-deploy", "none"),
    ("/rvf-land\n/review-validate-fix", "suppressed"),
    ("/review-validate-fix\n## 处理结果\nrvf-20261020T080229Z-test", "suppressed"),
    ("请参考 `$review-validate-fix` 的说明", "none"),
    ("运行时成果交接，已验收", "none"),
])
def test_explicit_manual_source_text_uses_user_prompt_submit_classification(command_text, expected_classification):
    submit = importlib.import_module("rvf_user_prompt_submit")
    assert submit._classify_manual_trigger({}, command_text) == expected_classification


@pytest.mark.parametrize("invalid_proof", ["checkpoint_binding", "merged_user_turn", "active_branch"])
def test_namespaced_manual_command_still_requires_native_turn_checkpoint_and_active_branch(valid_boundary, monkeypatch, invalid_proof):
    fixture = valid_boundary
    manual, receipt, _ = explicit_manual_command_boundary(fixture, monkeypatch,
        "now $review-validate-fix:review-validate-fix your own work.")
    if invalid_proof == "checkpoint_binding":
        receipt["checkpoint_commit"] = fixture["receipt"]["checkpoint_commit"]
    elif invalid_proof == "merged_user_turn":
        fixture["records"].append({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "independent-turn",
            "item": {"type": "UserMessage", "id": "steered-message", "content": [{"type": "text", "text": "回合中途补充输入"}]}}})
        fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    else:
        receipt_path = fixture["gate"].review_start_boundary_receipt_path(manual.token)
        receipt_path.with_suffix(".native-active-branch.json").write_text(json.dumps({
            "native_active_branch_user_message_ids": [], "native_active_branch_user_turn_id": "independent-turn"}))
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        fixture["gate"].require_review_start_boundary(token_payload=fixture["tokens"].read_decision_token(manual.token).payload,
            repo=str(fixture["repo"]), receipt=receipt)
    assert fixture["tokens"].read_decision_token(manual.token).state == "minted"


@pytest.mark.parametrize("command_text,initiated,provenance", [
    ("/review-validate-fix:rvf-land", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("请运行 $review-validate-fix:rvf-reopen", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("请运行 /review-validate-fix:rvf-local-deploy", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("/rvf-land\n/review-validate-fix", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("/review-validate-fix\n## 处理结果\nrvf-20261020T080229Z-test", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("请参考 `$review-validate-fix` 的说明", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("运行时成果交接，已验收", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("Agent reason 表示条件满足", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("<hook_prompt>\n/review-validate-fix\n</hook_prompt>", True, "developer_manual_trigger_via_user_prompt_submit"),
    ("$rvf:review-validate-fix", False, None),
    ("$rvf:review-validate-fix", True, "agent_reason_claim"),
    ("/review-validate-fix", False, None),
    ("/review-validate-fix", True, "agent_reason_claim"),
])
def test_explicit_manual_source_rejects_siblings_handoff_hook_and_unproven_commands(valid_boundary, monkeypatch, command_text, initiated, provenance):
    fixture = valid_boundary
    manual, receipt, _ = explicit_manual_command_boundary(fixture, monkeypatch, command_text)
    payload = dict(fixture["tokens"].read_decision_token(manual.token).payload,
        initiated_by_explicit_user_request=initiated, explicit_user_request_provenance=provenance)
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        fixture["gate"].require_review_start_boundary(token_payload=payload,
            repo=str(fixture["repo"]), receipt=receipt)
    assert fixture["tokens"].read_decision_token(manual.token).state == "minted"


def test_failed_boundary_delivery_keeps_fresh_token_unaccepted(valid_boundary):
    fixture = valid_boundary
    request = importlib.import_module("rvf_review_start_boundary_request")
    def failed_sender(**kwargs):
        raise OSError("合成投递失败")
    result = request.run_request_review_start_boundary(decision_token=fixture["token"], reason="重新请求独立消息",
        candidate_summary_builder=lambda event, repo: dict(fixture["payload"]["candidate_summary"]), boundary_sender=failed_sender)
    assert result.status == "review_start_boundary_pending"
    assert result.token != fixture["token"]
    assert fixture["tokens"].read_decision_token(result.token).state == "minted"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "withdrawn"


def test_local_base_change_without_candidate_content_change_keeps_boundary(valid_boundary):
    fixture = valid_boundary
    base = fixture["receipt"]["comparison_base_ref"]
    assert base
    commit = git(fixture["repo"], "commit-tree", fixture["receipt"]["checkpoint_tree"], "-p", "HEAD", "-m", "test: base 前进")
    # 改动另一个本地 ref，保持当前 worktree HEAD 和候选内容不变。
    git(fixture["repo"], "checkout", "--detach", "-q")
    git(fixture["repo"], "update-ref", "refs/heads/" + base, commit)
    result = fixture["cli"].run_start(token=fixture["token"], reason="候选内容看似不变")
    assert result.status == "ok"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "acknowledged"


def test_legacy_stop_route_cannot_allocate_or_consume_reopen_without_boundary(tmp_path, monkeypatch):
    stop = importlib.import_module("stop_review_validate_fix")
    from core.run_ledger.run_ledger import start_run
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    git(repo, "init", "-q")
    calls = []
    monkeypatch.setattr(stop, "precheck_session_scope_for_dirty_route", lambda *a, **kw: calls.append("precheck"))
    monkeypatch.setattr(stop, "consume_review_reopen_marker", lambda *a, **kw: calls.append("consume-reopen"))
    monkeypatch.setattr(stop, "allocate_auto_review_scope", lambda *a, **kw: calls.append("allocate"))
    ledger = start_run("stop-diagnostic", repo=str(repo), run_dir=tmp_path / "isolated-stop-diagnostic")
    decision = stop.route_reviewable_scope({"cwd":str(repo)}, str(repo), ["a.py"], ledger, str(repo), source="dirty_working_tree", gate_status="DIRTY")
    assert decision.reason_code == "review_start_boundary_pending"
    assert decision.audience == "developer_only"
    assert calls == []


def test_legacy_child_fork_cannot_write_formal_artifacts_without_boundary(tmp_path, monkeypatch):
    stop = importlib.import_module("stop_review_validate_fix")
    from core.run_ledger.run_ledger import start_run
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    git(repo, "init", "-q")
    ledger = start_run("stop-diagnostic", repo=str(repo), run_dir=tmp_path / "isolated-stop-diagnostic")
    calls = []
    monkeypatch.setattr(stop, "parent_thread_name_from_app_server", lambda *a, **kw: calls.append("host-query"))
    monkeypatch.setattr(stop, "write_dispatch_prep_file", lambda **kw: calls.append("formal-prep"))
    result = stop.run_review_validate_fix_child_fork(parent_session_id="synthetic-session", cwd=str(repo),
        prompt="旧 fork 入口", log_prefix="synthetic-test", ledger=ledger)
    assert result["continue"] is True
    assert "review_start_boundary_pending" in result["systemMessage"]
    assert calls == []
    assert not (ledger.artifacts_dir / "origin.json").exists()


@pytest.mark.parametrize("version", [None, "0.164.0"])
def test_unverified_native_harness_version_stays_pending(valid_boundary, version):
    fixture = valid_boundary
    fixture["records"][0]["payload"]["cli_version"] = version
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    result = fixture["cli"].run_start(token=fixture["token"], reason="不能从旧宿主版本外推")
    assert result.status == "review_start_boundary_pending"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_duplicate_native_message_id_is_not_a_unique_boundary(valid_boundary):
    fixture = valid_boundary
    fixture["records"].append(copy.deepcopy(fixture["records"][-1]))
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    result = fixture["cli"].run_start(token=fixture["token"], reason="重复原生 ID")
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


@pytest.mark.parametrize("comparison_argument", ["base_ref", "committed_baseline"])
def test_prepare_rejects_comparison_ref_advanced_after_boundary_before_run(valid_boundary, tmp_path, monkeypatch, comparison_argument):
    fixture = valid_boundary
    preparation = importlib.import_module("prepare_review_run")
    fixture["tokens"].acknowledge_decision_token(fixture["token"])
    repo = fixture["repo"]
    git(repo, "checkout", "--detach", "-q")
    fixed_base = fixture["receipt"]["comparison_base_ref_tip"]
    advanced_base = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-p", fixed_base,
                        "-m", "test: 分界之后推进显式比较 ref")
    git(repo, "update-ref", "refs/heads/main", advanced_base)
    def reject_run_creation(*args, **kwargs):
        pytest.fail("冲突比较坐标不得创建 run")
    monkeypatch.setattr(preparation, "start_run", reject_run_creation)
    with pytest.raises(ValueError, match="比较基线.*开审分界不一致"):
        preparation.prepare_run(repo=repo, session_context=None, session_manifest=None,
            transcript=None, base_dir=tmp_path / "runs", max_file_bytes=200_000, max_packet_bytes=0,
            primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
            scope_contract_extensions={"review_start_boundary_receipt": fixture["receipt"]},
            **{comparison_argument: "main"})
    assert git(repo, "rev-parse", "HEAD") == fixed_base
    assert git(repo, "rev-parse", "main") == advanced_base


def test_dry_run_diagnostic_never_creates_dispatch_prep_without_boundary(tmp_path, monkeypatch):
    stop = importlib.import_module("stop_review_validate_fix")
    from core.run_ledger.run_ledger import start_run
    ledger = start_run("isolated-diagnostic", run_dir=tmp_path / "isolated-diagnostic")
    calls = []
    monkeypatch.setattr(stop, "parent_thread_name_from_app_server", lambda *a, **kw: {"name":None,"source":"synthetic","thread_found":False})
    monkeypatch.setattr(stop, "write_dispatch_prep_file", lambda **kw: calls.append("formal-prep"))
    result = stop.run_review_validate_fix_child_fork(parent_session_id="synthetic-session", cwd=str(tmp_path),
        prompt="只渲染诊断请求", log_prefix="synthetic-diagnostic", ledger=ledger, launch_mode="dry-run")
    assert "reason=dry_run" in result["systemMessage"]
    assert calls == []


def test_process_restart_revalidates_native_branch_instead_of_cached_verified_status(valid_boundary):
    fixture = valid_boundary
    source = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import review_start_boundary_gate as gate, rvf_decision_token_store as tokens; "
        "gate.require_review_start_boundary(token_payload=tokens.read_decision_token(sys.argv[2]).payload, repo=sys.argv[3]); print('verified')"
    )
    command = [sys.executable, "-c", source, str(SCRIPT_DIRECTORY), fixture["token"], str(fixture["repo"])]
    first = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert first.returncode == 0, first.stderr
    fixture["records"].append({"type":"event_msg", "payload":{"type":"thread_rolled_back"}})
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    second = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert second.returncode != 0
    assert "review_start_boundary_invalidated" in second.stderr
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


@pytest.mark.parametrize("entry", ["start", "force-start", "prepare", "dispatch", "removed-reopen"])
def test_cli_processes_cannot_bypass_missing_boundary(valid_boundary, entry, tmp_path):
    fixture = valid_boundary
    fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).unlink()
    if entry in ("start", "force-start", "removed-reopen"):
        command = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_agent_decision_cli.py"), "start", "--token", fixture["token"], "--reason", "合成 CLI 请求"]
        expected_code = 12
        if entry == "force-start":
            command.append("--force-full-rescope")
        if entry == "removed-reopen":
            command.append("--reopen-own-declined-decision")
            expected_code = 2
    elif entry == "prepare":
        command = [sys.executable, str(SCRIPT_DIRECTORY / "prepare_review_run.py"), "--repo", str(fixture["repo"]), "--allow-missing-session-context", "--base-dir", str(tmp_path / "forbidden-prepared-runs")]
        expected_code = 2
    else:
        command = [sys.executable, str(SCRIPT_DIRECTORY / "dispatch_reviewers.py"), "--repo", str(fixture["repo"]), "--execute"]
        expected_code = 4
    result = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == expected_code, result.stderr
    if entry != "removed-reopen":
        assert "review_start_boundary_pending" in result.stderr
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"
    assert not (tmp_path / "forbidden-prepared-runs").exists()
    assert not (tmp_path / "isolated-user-local-data" / "run-logs" / "runs").exists()


def test_default_boundary_sender_uses_idle_delivery_and_persists_intent_before_send(valid_boundary, monkeypatch, tmp_path):
    fixture = valid_boundary
    request = importlib.import_module("rvf_review_start_boundary_request")
    client = importlib.import_module("cline_kanban_client")
    locks = importlib.import_module("kanban_followup_lock")
    monkeypatch.setenv("KANBAN_TASK_ID", "synthetic-task")
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(tmp_path / "isolated-lock-and-intent-root"))
    sent = []
    def send(**fields):
        intent = locks.read_decision_point_delivery_intent(task_id="synthetic-task")
        assert intent and intent["idempotency_key"] == fields["idempotency_key"]
        sent.append(fields)
        return {"terminal_delivery_status":"accepted_pending_submit_confirmation"}
    monkeypatch.setattr(client, "send_task_message", send)
    result = request.run_request_review_start_boundary(decision_token=fixture["token"], reason="请求下一独立回合",
        candidate_summary_builder=lambda event, repo: dict(fixture["payload"]["candidate_summary"]))
    assert result.status == "review_start_boundary_pending"
    assert len(sent) == 1, result
    assert sent[0]["deliver_when_agent_idle"] is True
    assert sent[0]["required_native_independent_user_turn_session_id"] == fixture["payload"]["session_id"]
    assert 0 < sent[0]["max_wait_for_agent_idle_ms"] < 1200000
    assert sent[0]["prompt"] == fixture["tokens"].render_decision_point_out_of_band_boundary_marker(token=result.token)
    assert fixture["tokens"].read_decision_token(result.token).state == "minted"


def test_same_turn_implementation_then_content_restore_does_not_reuse_old_boundary(valid_boundary):
    fixture = valid_boundary
    path = fixture["repo"] / "a.py"
    original = path.read_text()
    path.write_text("value = 99\n")
    path.write_text(original)
    result = fixture["cli"].run_start(token=fixture["token"], reason="内容已恢复也不能省略新的分界")
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


@pytest.mark.parametrize("source,originator", [("exec","codex_exec"),("vscode","codex_vscode")])
def test_other_codex_host_shapes_do_not_borrow_tui_rewind_proof(valid_boundary, source, originator):
    fixture = valid_boundary
    fixture["records"][0]["payload"].update(source=source, originator=originator)
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    result = fixture["cli"].run_start(token=fixture["token"], reason="其它宿主不能沿用 TUI 样本")
    assert result.status == "review_start_boundary_pending"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_native_ui_rewind_with_unchanged_rollout_is_rejected_by_fresh_host_branch(valid_boundary):
    fixture = valid_boundary
    from adapters.codex.review_start_independent_user_turn_evidence import read_independent_user_turn_boundary_evidence
    assert read_independent_user_turn_boundary_evidence(receipt=fixture["receipt"])["independent_user_turn_verified"] is True
    # 复现真实 TUI：原始 rollout 尚保留旧消息，宿主当前分支已经移除了它。
    active = fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).with_suffix(".native-active-branch.json")
    active.write_text(json.dumps({"native_active_branch_user_message_ids":[], "native_active_branch_user_turn_id":"rewound-active-turn"}))
    result = fixture["cli"].run_start(token=fixture["token"], reason="旧文件仍在不等于分界仍有效")
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


@pytest.mark.parametrize("host_reply", ["unavailable", "stale-nonce", "missing-observation"])
def test_static_receipt_never_replaces_a_fresh_host_observation(valid_boundary, monkeypatch, host_reply):
    fixture = valid_boundary
    client = importlib.import_module("cline_kanban_client")
    def reply(**fields):
        if host_reply == "unavailable":
            raise OSError("宿主观测接口不可用")
        result = {"independent_user_turn_boundary_receipt":fixture["receipt"]}
        if host_reply == "stale-nonce":
            result["review_start_boundary_active_branch_observation"] = {
                "request_nonce":"stale-request", "kanban_task_id":fields["task_id"], "idempotency_key":fields["idempotency_key"],
                "session_id":fixture["receipt"]["session_id"], "native_active_branch_user_message_ids":[fixture["receipt"]["native_user_message_id"]],
                "native_active_branch_user_turn_id":fixture["receipt"]["native_turn_id"]}
        return result
    monkeypatch.setattr(client, "read_task_message_terminal_delivery_status", reply)
    result = fixture["cli"].run_start(token=fixture["token"], reason="即使 canonical receipt 存在也须实时观测")
    assert result.status == "review_start_boundary_pending"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_each_gate_queries_a_new_host_observation_and_rejects_forged_supplied_receipt(valid_boundary, monkeypatch):
    fixture = valid_boundary
    client = importlib.import_module("cline_kanban_client")
    original = client.read_task_message_terminal_delivery_status
    requests = []
    def observe(**fields):
        requests.append(fields["review_start_boundary_observation_nonce"])
        return original(**fields)
    monkeypatch.setattr(client, "read_task_message_terminal_delivery_status", observe)
    for _ in range(2):
        fixture["gate"].require_review_start_boundary(token_payload=fixture["payload"], repo=str(fixture["repo"]))
    assert len(requests) == len(set(requests)) == 2
    forged = dict(fixture["receipt"], checkpoint_commit="0"*40)
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        fixture["gate"].require_review_start_boundary(token_payload=fixture["payload"], repo=str(fixture["repo"]), receipt=forged)


def test_separate_message_steered_into_existing_user_turn_is_not_an_independent_boundary(valid_boundary):
    fixture = valid_boundary
    fixture["records"].insert(-1, {"type":"event_msg", "payload":{"type":"item_completed", "turn_id":"independent-turn",
        "item":{"type":"UserMessage", "id":"another-user-message-in-the-same-turn", "content":[{"type":"text","text":"运行时成果交接"}]}}})
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    result = fixture["cli"].run_start(token=fixture["token"], reason="单独一条消息仍须是新 turn")
    assert result.status == "review_start_boundary_invalidated"
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


def test_model_maintenance_without_new_user_message_keeps_the_same_review_boundary(valid_boundary):
    fixture = valid_boundary
    fixture["records"].append({"type":"event_msg", "payload":{"type":"task_started", "turn_id":"model-maintenance-turn", "turn_attribution":{"turn_trigger":"automatic_context_compaction"}}})
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in fixture["records"]))
    active = fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).with_suffix(".native-active-branch.json")
    active.write_text(json.dumps({"native_active_branch_user_message_ids":[fixture["receipt"]["native_user_message_id"]], "native_active_branch_user_turn_id":fixture["receipt"]["native_turn_id"]}))
    calls = []
    result = fixture["cli"].run_start(token=fixture["token"], reason="模型维护没有产生新的用户消息或实现",
        prepare_pipeline=lambda **kwargs: calls.append(kwargs) or {"status":"failed","error":"止于验证边界"})
    assert result.status == "start_failed"
    assert len(calls) == 1
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "acknowledged"


def test_rewind_during_first_receipt_reconciliation_is_rechecked_before_acknowledge(valid_boundary, tmp_path, monkeypatch):
    fixture = valid_boundary
    client = importlib.import_module("cline_kanban_client")
    from independent_review_start_boundary_fixture import configure_synthetic_live_boundary_host_observation
    host_private_receipt = tmp_path / "host-owned-boundary-receipt.json"
    host_private_receipt.write_text(json.dumps(fixture["receipt"]))
    environment = configure_synthetic_live_boundary_host_observation(receipt_path=host_private_receipt, repo=fixture["repo"], environment=os.environ.copy())
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", environment["CODEX_RVF_CLINE_KANBAN_TASK_CMD"])
    fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).unlink()
    original = client.read_task_message_terminal_delivery_status
    observations = []
    def observe(**fields):
        response = original(**fields)
        observations.append(fields["review_start_boundary_observation_nonce"])
        if len(observations) == 2:
            response["review_start_boundary_active_branch_observation"]["native_active_branch_user_message_ids"] = []
        return response
    monkeypatch.setattr(client, "read_task_message_terminal_delivery_status", observe)
    calls = []
    result = fixture["cli"].run_start(token=fixture["token"], reason="首次取证期间发生真实分支撤回",
        prepare_pipeline=lambda **kw: calls.append(kw))
    assert result.status == "review_start_boundary_invalidated"
    assert len(observations) == 2
    assert observations[0] != observations[1]
    assert calls == []
    assert fixture["tokens"].read_decision_token(fixture["token"]).state == "minted"


@pytest.mark.parametrize("reused_identity", ["run_id", "run_dir"])
def test_run_identity_cannot_be_reassigned_to_a_second_token(valid_boundary, tmp_path, reused_identity):
    fixture = valid_boundary
    gate = fixture["gate"]
    original_run_directory = tmp_path / "original-directory"
    gate.bind_review_start_boundary_to_one_run(receipt=fixture["receipt"], run_id="logical-run", run_dir=original_run_directory)
    competing_receipt = dict(fixture["receipt"], decision_token="f" * 16)
    competing_id = "logical-run" if reused_identity == "run_id" else "another-logical-run"
    competing_directory = original_run_directory if reused_identity == "run_dir" else tmp_path / "another-directory"
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        gate.bind_review_start_boundary_to_one_run(receipt=competing_receipt, run_id=competing_id, run_dir=competing_directory)
    assert not gate.review_start_boundary_receipt_path(competing_receipt["decision_token"]).with_suffix(".run-binding.json").exists()
    assert not original_run_directory.exists()
    assert not competing_directory.exists()


@pytest.mark.parametrize("cached_completed", [False, True])
@pytest.mark.parametrize("changed_identity", ["run_id", "run_dir"])
def test_prep_replay_checks_binding_before_cache_or_startup_artifact(valid_boundary, tmp_path, cached_completed, changed_identity):
    fixture = valid_boundary
    prepare = importlib.import_module("prepare_review_run")
    prep_files = importlib.import_module("rvf_prep_file")
    original_run = tmp_path / "original-directory"
    fixture["gate"].bind_review_start_boundary_to_one_run(receipt=fixture["receipt"], run_id="logical-run", run_dir=original_run)
    run_directory = tmp_path / "forbidden-replayed-directory" if changed_identity == "run_dir" else original_run
    run_id = "another-logical-run" if changed_identity == "run_id" else "logical-run"
    payload = {
        "origin_session_id": fixture["receipt"]["session_id"], "origin_repo": str(fixture["repo"]),
        "origin_cwd": str(fixture["repo"]), "target_worktree": str(fixture["repo"]),
        "target_flow": "flow-agentic-initiation",
        "rvf_run": {"run_id": run_id, "run_dir": str(run_directory), "review_start_boundary_receipt": fixture["receipt"]},
    }
    if cached_completed:
        payload["rvf_run"]["shared_workflow_state"] = {"status": "completed", "run_id": run_id, "run_dir": str(run_directory), "artifacts": {}}
    prep = prep_files.write_prep_file(payload, root=tmp_path / "dispatch-prep-files")
    original_prep_bytes = prep.path.read_bytes()
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        prepare.prepare_run_from_prep_file(prep, base_dir=tmp_path / "forbidden-prepared-runs")
    assert prep.path.read_bytes() == original_prep_bytes
    assert not run_directory.exists()
    assert not (tmp_path / "forbidden-prepared-runs").exists()


def test_default_start_prepare_dispatch_preserves_real_run_identity(valid_boundary, tmp_path, monkeypatch):
    fixture = valid_boundary
    cli = fixture["cli"]
    bridge = importlib.import_module("rvf_automatic_closeout_bridge")
    logical_run_id = "logical-review-run"
    run_directory = tmp_path / "different-physical-directory-name"
    monkeypatch.setenv("RVF_RUN_ID", logical_run_id)
    monkeypatch.setenv("RVF_RUN_DIR", str(run_directory))
    monkeypatch.setenv("CODEX_RVF_PREP_ROOT", str(tmp_path / "dispatch-prep-files"))
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(tmp_path / "isolated-followup-locks"))
    # 只替代宿主捕获/激活接口和外部 provider 探测；默认 prepare 与所有真实分界绑定均执行。
    monkeypatch.setattr(bridge, "capture_before_start", lambda *args, **kwargs: fixture["receipt"])
    activations = []
    monkeypatch.setattr(bridge, "activate_bound_run", lambda receipt: activations.append(receipt))
    started = cli.run_start(token=fixture["token"], reason="验证默认完整绑定链", transcript_path=str(fixture["transcript"]),
        launch_preflight=lambda **kwargs: (True, None), provider_health_requirements_resolver=lambda backend: [])
    assert started.status == "ok", started
    assert activations == [fixture["receipt"]]
    overlay = cli.read_scope_adjustment(fixture["token"])
    assert overlay["run_id"] == logical_run_id
    assert overlay["run_dir"] == str(run_directory)
    contract_path = Path(overlay["prepare_artifacts"]["scope_contract"])
    assert json.loads(contract_path.read_text())["run_id"] == logical_run_id
    binding_path = fixture["gate"].review_start_boundary_receipt_path(fixture["token"]).with_suffix(".run-binding.json")
    frozen_binding = binding_path.read_bytes()
    prep_files = importlib.import_module("rvf_prep_file")
    prepare = importlib.import_module("prepare_review_run")
    prep_path = next((tmp_path / "dispatch-prep-files").glob("*.json"))
    prep_payload = json.loads(prep_path.read_text())
    prepared_contract_snapshot = (contract_path.read_bytes(), contract_path.stat().st_mtime_ns)
    repeated = cli.run_start(token=fixture["token"], reason="重复读取已准备的 run", retry=True,
        transcript_path=str(fixture["transcript"]),
        prepare_pipeline=lambda **kwargs: pytest.fail("同一 token 已准备完成，不得重复 prepare"))
    assert repeated.status == "ok", repeated
    assert repeated.detail["run_id"] == logical_run_id
    assert activations == [fixture["receipt"]]
    assert (contract_path.read_bytes(), contract_path.stat().st_mtime_ns) == prepared_contract_snapshot
    completed_cache = prepare.prepare_run_from_prep_file(prep_files.PrepFileRecord(token=prep_payload["token"], path=prep_path, payload=prep_payload))
    assert completed_cache["status"] == "completed"
    assert completed_cache["run_id"] == logical_run_id
    assert (contract_path.read_bytes(), contract_path.stat().st_mtime_ns) == prepared_contract_snapshot
    scope = Path(overlay["prepare_artifacts"]["scope_of_work"])
    scope.write_text("## 本轮范围\n审查 a.py 中 value 从 1 更新为 2 的实现与行为。\n")
    rerendered = cli.run_scope_rerender_scope_of_work(token=fixture["token"])
    assert rerendered.status == "ok", rerendered
    dispatches = []
    dispatched = cli.run_dispatch(token=fixture["token"], strictness="light",
        reviewer_dispatcher=lambda **kwargs: dispatches.append(kwargs) or {"status": "ok"})
    assert dispatched.status == "ok", dispatched
    assert dispatches[0]["run_id"] == logical_run_id
    assert dispatches[0]["run_dir"] == str(run_directory)
    assert binding_path.read_bytes() == frozen_binding


def prepare_start_with_retryable_post_prepare_failure(fixture, tmp_path, monkeypatch, failure_phase):
    cli = fixture["cli"]
    bridge = importlib.import_module("rvf_automatic_closeout_bridge")
    tracker = importlib.import_module("core.session_scope_allocation.reviewable_unit_diff_tracker")
    records = copy.deepcopy(fixture["records"])
    records.insert(1, {
        "type": "event_msg", "timestamp": records[-1]["timestamp"],
        "payload": {"type": "patch_apply_end", "call_id": "owned-a-py-edit", "success": True,
                    "changes": {str(fixture["repo"] / "a.py"): {
                        "type": "update", "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2\n"}},
                    "status": "completed"},
    })
    fixture["transcript"].write_text("\n".join(json.dumps(record) for record in records) + "\n")
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(tmp_path / "isolated-run-logs"))
    monkeypatch.setenv("CODEX_RVF_PREP_ROOT", str(tmp_path / "dispatch-prep-files"))
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(tmp_path / "isolated-followup-locks"))
    monkeypatch.setattr(bridge, "capture_before_start", lambda *args, **kwargs: fixture["receipt"])
    allocations, preparations, bindings, activations = [], [], [], []
    original_allocate = tracker.allocate_review_scope
    original_bind = bridge.bind_run

    def allocate(**kwargs):
        result = original_allocate(**kwargs)
        allocations.append(result)
        return result

    def prepare(**kwargs):
        result = cli.default_prepare_pipeline(**kwargs)
        preparations.append(result)
        return result

    def bind(*args, **kwargs):
        bindings.append(kwargs)
        if failure_phase == "bind" and len(bindings) == 1:
            raise RuntimeError("合成的首次绑定失败")
        return original_bind(*args, **kwargs)

    def activate(receipt):
        activations.append(receipt)
        if failure_phase == "activate" and len(activations) == 1:
            raise RuntimeError("合成的首次激活失败")

    monkeypatch.setattr(tracker, "allocate_review_scope", allocate)
    monkeypatch.setattr(bridge, "bind_run", bind)
    monkeypatch.setattr(bridge, "activate_bound_run", activate)
    arguments = dict(token=fixture["token"], reason="验证准备成功后的失败重试",
                     transcript_path=str(fixture["transcript"]), prepare_pipeline=prepare,
                     launch_preflight=lambda **kwargs: (True, None),
                     provider_health_requirements_resolver=lambda backend: [])
    failed = cli.run_start(**arguments)
    assert failed.status == "start_failed", failed
    assert failed.detail["phase"] == (
        "bind_automatic_closeout_run" if failure_phase == "bind"
        else "activate_automatic_closeout_after_run_binding")
    assert len(preparations) == len(allocations) == 1
    assert preparations[0]["status"] == "completed"
    assert allocations[0]["status"] == "allocated"
    return arguments, preparations, allocations, bindings, activations


@pytest.mark.parametrize("failure_phase", ["bind", "activate"])
def test_start_retry_reuses_completed_prepare_and_lease(valid_boundary, tmp_path, monkeypatch, failure_phase):
    fixture = valid_boundary
    cli = fixture["cli"]
    arguments, preparations, allocations, bindings, activations = prepare_start_with_retryable_post_prepare_failure(
        fixture, tmp_path, monkeypatch, failure_phase)
    prepared = copy.deepcopy(preparations[0])
    artifact_snapshots = {path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
                          for path in prepared["artifacts"].values()
                          if isinstance(path, str) and Path(path).is_file()}
    overlay = cli.read_scope_adjustment(fixture["token"])
    assert overlay is not None
    overlay["annotations"] = {"a.py": "保留已有范围说明"}
    cli.write_scope_adjustment(overlay, token=fixture["token"])
    repeated = cli.run_start(**arguments, retry=True)
    assert repeated.status == "ok", repeated
    assert len(preparations) == len(allocations) == 1
    assert repeated.detail["run_id"] == prepared["run_id"]
    assert repeated.detail["run_dir"] == prepared["run_dir"]
    assert repeated.detail["prepare_artifacts"] == prepared["artifacts"]
    assert repeated.detail["tracker_scope_allocation"]["tracker_lease_id"] == allocations[0]["lease_id"]
    assert len(bindings) == (2 if failure_phase == "bind" else 1)
    assert len(activations) == (2 if failure_phase == "activate" else 1)
    assert cli.read_scope_adjustment(fixture["token"])["annotations"] == overlay["annotations"]
    assert {path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
            for path in artifact_snapshots} == artifact_snapshots
    successful_retry = cli.run_start(**arguments, retry=True)
    assert successful_retry.to_dict() == repeated.to_dict()
    assert len(preparations) == len(allocations) == 1
    assert len(activations) == (2 if failure_phase == "activate" else 1)


@pytest.mark.parametrize("changed", ["candidate", "prepared_run"])
def test_start_retry_revalidates_boundary_and_prepared_run(valid_boundary, tmp_path, monkeypatch, changed):
    fixture = valid_boundary
    arguments, preparations, allocations, bindings, activations = prepare_start_with_retryable_post_prepare_failure(
        fixture, tmp_path, monkeypatch, "activate")
    if changed == "candidate":
        (fixture["repo"] / "a.py").write_text("value = 3\n")
    else:
        contract_path = Path(preparations[0]["artifacts"]["scope_contract"])
        contract = json.loads(contract_path.read_text())
        contract["run_id"] = "another-run"
        contract_path.write_text(json.dumps(contract))
    rejected = fixture["cli"].run_start(**arguments, retry=True)
    assert rejected.status == "review_start_boundary_invalidated", rejected
    assert len(preparations) == len(allocations) == len(bindings) == len(activations) == 1


def test_real_prepare_rejects_second_token_before_overwriting_scope_contract(valid_boundary, tmp_path, monkeypatch):
    fixture = valid_boundary
    prepare = importlib.import_module("prepare_review_run")
    context = tmp_path / "written-scope-of-work.md"
    context.write_text("## 本轮范围\n审查 a.py 的 value 更新。\n")
    run_directory = tmp_path / "physical-run-directory"
    arguments = dict(repo=fixture["repo"], session_context=context, session_manifest=None, transcript=None,
        base_dir=tmp_path / "prepared-runs", max_file_bytes=10000, max_packet_bytes=0,
        primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
        rvf_run_id="logical-run", rvf_run_dir=run_directory)
    first = prepare.prepare_run(**arguments, scope_contract_extensions={"review_start_boundary_receipt": fixture["receipt"]})
    contract = Path(first["scope_contract"])
    original_contract = (contract.read_bytes(), contract.stat().st_mtime_ns)
    from independent_review_start_boundary_fixture import environment_with_synthetic_independent_review_start_boundary
    competing_environment = environment_with_synthetic_independent_review_start_boundary(
        repo=fixture["repo"], environment=os.environ.copy(), run_id="logical-run", run_dir=run_directory,
        session_id="second-independent-session", preserve_runtime_state_root=True)
    for key, value in competing_environment.items():
        monkeypatch.setenv(key, value)
    competing_receipt = json.loads(Path(competing_environment["RVF_REVIEW_START_BOUNDARY_RECEIPT"]).read_text())
    assert competing_receipt["decision_token"] != fixture["token"]
    from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
    with pytest.raises(ReviewStartBoundaryNotVerified):
        prepare.prepare_run(**arguments, scope_contract_extensions={"review_start_boundary_receipt": competing_receipt})
    assert (contract.read_bytes(), contract.stat().st_mtime_ns) == original_contract
    assert not fixture["gate"].review_start_boundary_receipt_path(competing_receipt["decision_token"]).with_suffix(".run-binding.json").exists()


@pytest.mark.parametrize("conflicting_direction", ["same_run", "same_run_id", "same_run_directory"])
def test_concurrent_processes_and_restart_preserve_exclusive_run_binding(valid_boundary, tmp_path, conflicting_direction):
    fixture = valid_boundary
    run_directory = tmp_path / "claimed-physical-directory"
    code = '''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import review_start_boundary_gate as gate
from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
receipt, run_id, directory = json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    gate.bind_review_start_boundary_to_one_run(receipt=receipt, run_id=run_id, run_dir=directory)
    print(json.dumps({"status":"bound", "token":receipt["decision_token"]}))
except ReviewStartBoundaryNotVerified as error:
    print(json.dumps({"status":error.status, "token":receipt["decision_token"]}))
'''
    contenders = []
    for number in range(6):
        receipt = dict(fixture["receipt"], decision_token=f"{number + 1:016x}")
        run_id = f"logical-run-{number}" if conflicting_direction == "same_run_directory" else "logical-run"
        directory = tmp_path / f"physical-directory-{number}" if conflicting_direction == "same_run_id" else run_directory
        contenders.append((receipt, run_id, directory))
    def command(contender):
        receipt, run_id, directory = contender
        return [sys.executable, "-c", code, str(SCRIPT_DIRECTORY), json.dumps(receipt), run_id, str(directory)]
    processes = [subprocess.Popen(command(contender), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy()) for contender in contenders]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert [result["status"] for result in results].count("bound") == 1
    winner = next(contender for contender, result in zip(contenders, results) if result["status"] == "bound")
    binding_path = fixture["gate"].review_start_boundary_receipt_path(winner[0]["decision_token"]).with_suffix(".run-binding.json")
    frozen_binding = (binding_path.read_bytes(), binding_path.stat().st_mtime_ns)
    # 重启后的每个进程仍只接受原获胜者，且同一静态 receipt 重入不改冻结记录。
    for contender in contenders:
        restarted = subprocess.run(command(contender), check=True, capture_output=True, text=True, env=os.environ.copy())
        result = json.loads(restarted.stdout)
        assert result["status"] == ("bound" if contender == winner else "review_start_boundary_invalidated")
    assert (binding_path.read_bytes(), binding_path.stat().st_mtime_ns) == frozen_binding
    assert all(not contender[2].exists() for contender in contenders)
