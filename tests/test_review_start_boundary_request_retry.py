"""普通自主开审分界请求的失败恢复、进程重启与旧决策隔离。"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))


@pytest.fixture
def isolated_boundary_request(tmp_path, monkeypatch):
    import _rvf_pyroot  # noqa: F401
    from core.host_adapter.current_agent_session_identity_from_environment import CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES

    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")) or name in CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local-data"))
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-boundary-request-session")
    repo = tmp_path / "synthetic-repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tokens = importlib.import_module("rvf_decision_token_store")
    request = importlib.import_module("rvf_review_start_boundary_request")
    cli = importlib.import_module("rvf_agent_decision_cli")
    summary = {"candidate_path_count": 1, "candidate_fingerprint": "synthetic-candidate", "candidates": []}
    now = datetime.now(timezone.utc)
    source = tokens.mint_decision_token(
        worktree=str(repo), session_id=os.environ["CODEX_THREAD_ID"],
        candidate_summary=summary, candidate_fingerprint=summary["candidate_fingerprint"], now=now,
    ).token
    deliveries = []

    def failed_sender(**fields):
        deliveries.append(fields)
        raise OSError("合成终端发送异常")

    monkeypatch.setattr(importlib.import_module("cline_kanban_client"), "send_task_message", failed_sender)
    monkeypatch.setattr(importlib.import_module("rvf_analyze_advisory"), "current_kanban_task_id", lambda event: "synthetic-task")
    monkeypatch.setattr(importlib.import_module("stop_review_validate_fix"), "_render_decision_point_injection_prompt", lambda **fields: "合成独立开审正文")
    monkeypatch.setattr(importlib.import_module("kanban_followup_lock"), "write_decision_point_delivery_intent", lambda **fields: None)
    return {"request": request, "tokens": tokens, "cli": cli, "source": source, "summary": summary,
            "now": now, "repo": repo, "deliveries": deliveries, "monkeypatch": monkeypatch}


def request_boundary(fixture, **overrides):
    arguments = dict(decision_token=fixture["source"], reason="请求下一独立开审消息", now=fixture["now"] + timedelta(seconds=1),
                     candidate_summary_builder=lambda event, repo: dict(fixture["summary"]))
    arguments.update(overrides)
    return fixture["request"].run_request_review_start_boundary(**arguments)


@pytest.mark.parametrize("derived_state", ["minted", "injected"])
@pytest.mark.parametrize("restart_process", [False, True])
def test_ordinary_request_retries_same_token_and_delivery_key_after_send_failure(isolated_boundary_request, derived_state, restart_process):
    fixture = isolated_boundary_request
    first = request_boundary(fixture)
    assert first.status == "review_start_boundary_pending"
    assert first.token != fixture["source"]
    assert fixture["tokens"].read_decision_token(fixture["source"]).state == "withdrawn"
    if derived_state == "injected":
        fixture["tokens"].mark_injected(first.token, now=fixture["now"] + timedelta(seconds=1))
    if restart_process:
        # 新解释器只读取隔离磁盘状态，不继承第一次调用的模块对象或内存缓存。
        program = '''
import json, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import rvf_review_start_boundary_request as request
import cline_kanban_client as client
import rvf_analyze_advisory as advisory
import stop_review_validate_fix as stop
import kanban_followup_lock as delivery_lock
sent = []
client.send_task_message = lambda **fields: sent.append(fields) or {"terminal_delivery_status": "accepted_pending_submit_confirmation"}
advisory.current_kanban_task_id = lambda event: "synthetic-task"
stop._render_decision_point_injection_prompt = lambda **fields: "合成独立开审正文"
delivery_lock.write_decision_point_delivery_intent = lambda **fields: None
result = request.run_request_review_start_boundary(decision_token=sys.argv[2], reason="请求下一独立开审消息",
    now=datetime.fromisoformat(sys.argv[3]), candidate_summary_builder=lambda event, repo: json.loads(sys.argv[4]))
print(json.dumps({"status": result.status, "token": result.token,
                  "deliveries": [{"idempotency_key": item["idempotency_key"], "prompt": item["prompt"]} for item in sent]}))
'''
        completed = subprocess.run([sys.executable, "-c", program, str(SCRIPT_DIRECTORY), fixture["source"],
                                    (fixture["now"] + timedelta(seconds=2)).isoformat(), json.dumps(fixture["summary"])],
                                   cwd=ROOT, check=True, capture_output=True, text=True)
        second = json.loads(completed.stdout)
        assert second["status"] == "review_start_boundary_pending", second
        assert second["token"] == first.token, second
        retried_deliveries = second["deliveries"]
    else:
        second = request_boundary(fixture)
        assert second.status == "review_start_boundary_pending", second
        assert second.token == first.token, second
        retried_deliveries = fixture["deliveries"][1:]
    assert len(retried_deliveries) == 1
    assert retried_deliveries[0]["idempotency_key"] == fixture["deliveries"][0]["idempotency_key"]
    assert retried_deliveries[0]["prompt"] == fixture["deliveries"][0]["prompt"]
    records = fixture["tokens"].iter_decision_tokens_for_session(worktree=str(fixture["repo"]), session_id="synthetic-boundary-request-session")
    assert len(records) == 2
    derived = fixture["tokens"].read_decision_token(first.token)
    assert derived.state == derived_state
    assert derived.payload["initiated_by_explicit_user_request"] is False
    assert not derived.payload.get("explicit_user_request_provenance")


@pytest.mark.parametrize("invalidated_by", ["candidate", "expired_source", "expired_derived", "newer_decision", "other_session", "unrelated_withdrawal", "derived_structural_decline"])
def test_withdrawn_source_does_not_revive_invalidated_boundary_request(isolated_boundary_request, invalidated_by):
    fixture = isolated_boundary_request
    first = request_boundary(fixture)
    tokens = fixture["tokens"]
    overrides = {}
    if invalidated_by == "candidate":
        fixture["summary"]["candidate_fingerprint"] = "changed-candidate"
    elif invalidated_by in ("expired_source", "expired_derived"):
        expired_token = fixture["source"] if invalidated_by == "expired_source" else first.token
        # 只改变隔离 fixture 的时效，使来源与派生的过期约束可分别验证。
        record = tokens.read_decision_token(expired_token)
        payload = dict(record.payload, expires_at=fixture["now"].isoformat())
        record.path.write_text(json.dumps(payload), encoding="utf-8")
    elif invalidated_by == "newer_decision":
        tokens.mint_decision_token(worktree=str(fixture["repo"]), session_id="synthetic-boundary-request-session",
            candidate_fingerprint="newer-candidate", candidate_summary=fixture["summary"], now=fixture["now"] + timedelta(seconds=2),
            circuit_breaker_max_requests=10)
    elif invalidated_by == "other_session":
        fixture["monkeypatch"].setenv("CODEX_THREAD_ID", "another-session")
    elif invalidated_by == "unrelated_withdrawal":
        record = tokens.read_decision_token(fixture["source"])
        record.path.write_text(json.dumps(dict(record.payload, withdrawal_cause="rewound_by_user")), encoding="utf-8")
    elif invalidated_by == "derived_structural_decline":
        tokens.mark_declined(first.token, reason="本轮不需要审查", decline_kind="structural_not_needed",
                             now=fixture["now"] + timedelta(seconds=2))
    previous_files = {path: path.read_bytes() for path in tokens.decision_token_root().rglob("*") if path.is_file()}
    second = request_boundary(fixture, **overrides)
    assert second.status in ("error", "review_start_boundary_invalidated"), second
    assert len(fixture["deliveries"]) == 1
    assert previous_files == {path: path.read_bytes() for path in tokens.decision_token_root().rglob("*") if path.is_file()}


@pytest.mark.parametrize("source_argument", ["decision_token", "deferred_decision_token"])
def test_structurally_declined_source_cannot_request_new_boundary(isolated_boundary_request, source_argument):
    fixture = isolated_boundary_request
    fixture["tokens"].mark_declined(fixture["source"], reason="无需审查", decline_kind="structural_not_needed", now=fixture["now"])
    result = request_boundary(fixture, decision_token=None, **{source_argument: fixture["source"]}) if source_argument == "deferred_decision_token" else request_boundary(fixture)
    assert result.status == "error"
    assert fixture["deliveries"] == []
