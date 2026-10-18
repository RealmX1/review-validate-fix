"""无 Kanban task 的 Codex GUI 只允许手动 RVF 与静默收尾。"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def isolated_hook_environment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def desktop_event(tmp_path, *, originator="Codex Desktop", source="vscode"):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "desktop-session", "originator": originator, "source": source},
    }) + "\n", encoding="utf-8")
    return {"session_id": "desktop-session", "cwd": str(tmp_path),
            "transcript_path": str(transcript)}


def policy_module():
    return importlib.import_module("codex_gui_without_kanban_task_stop_hook_policy")


@pytest.mark.parametrize("path_key", [
    "transcript_path", "session_path", "conversation_path", "log_path", "session_file",
])
def test_gui_without_task_is_disabled_for_every_transcript_path(tmp_path, path_key):
    event = desktop_event(tmp_path)
    event[path_key] = event.pop("transcript_path")
    assert policy_module().codex_gui_without_kanban_task_stop_hook_is_disabled(event)


@pytest.mark.parametrize("task_key", [
    "kanban_task_id", "kanbanTaskId", "task_id", "taskId",
])
def test_event_task_binding_preserves_gui_automation(tmp_path, task_key):
    event = desktop_event(tmp_path)
    event[task_key] = "bound-task"
    assert not policy_module().codex_gui_without_kanban_task_stop_hook_is_disabled(event)


@pytest.mark.parametrize("task_key", [
    "KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID",
])
def test_environment_task_binding_preserves_gui_automation(tmp_path, monkeypatch, task_key):
    event = desktop_event(tmp_path)
    monkeypatch.setenv(task_key, "bound-task")
    assert not policy_module().codex_gui_without_kanban_task_stop_hook_is_disabled(event)


@pytest.mark.parametrize("originator,source", [
    ("codex-tui", "cli"), ("codex_exec", "exec"), ("codex_vscode", "vscode"),
    (None, "vscode"), ("Claude Code", "claude-code"),
])
def test_other_or_unknown_clients_keep_existing_behavior(tmp_path, originator, source):
    event = desktop_event(tmp_path, originator=originator, source=source)
    assert not policy_module().codex_gui_without_kanban_task_stop_hook_is_disabled(event)


@pytest.mark.parametrize("script,channel", [
    ("stop_hook_router.py", "stable"), ("stop_hook_router.py", "dev"),
    ("stop_hook_dispatcher.py", "stable"), ("stop_review_validate_fix.py", "stable"),
])
def test_real_stop_entries_do_not_dispatch_or_reengage(tmp_path, monkeypatch, script, channel):
    event = desktop_event(tmp_path)
    event.update({"last_user_message": "RVF_STOP_HOOK: on", "stop_hook_active": True})
    marker = tmp_path / "forbidden-target-ran"
    forbidden_target = tmp_path / "forbidden-target.py"
    forbidden_target.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(99)\n",
        encoding="utf-8",
    )
    for key in ("CODEX_RVF_STABLE_STOP_HOOK", "CODEX_RVF_DEV_STOP_HOOK", "CODEX_RVF_STOP_HOOK"):
        monkeypatch.setenv(key, str(forbidden_target))
    monkeypatch.setenv("CODEX_RVF_DEV_REPO", str(tmp_path))
    monkeypatch.setenv("CODEX_RVF_DEV_SYNC", "1")
    from core.rvf_user_local_data_paths import resolved_rvf_session_hook_state_dir
    state_path = resolved_rvf_session_hook_state_dir() / "desktop-session.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"channel": channel, "enabled": True}), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script)], input=json.dumps(event),
        text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "codex_gui_without_kanban_task_stop_hook_disabled" in payload["systemMessage"]
    assert "decision" not in payload and "hookSpecificOutput" not in payload
    assert not marker.exists()
    assert not list((tmp_path / "user-local-data").rglob("*decision-token*.json"))


@pytest.mark.parametrize("session_context", [
    "desktop_without_task", "desktop_with_task", "codex_tui_without_task",
])
def test_main_guardian_alarm_policy_preserves_translation_and_emitted_payload_ledger(
    tmp_path, monkeypatch, capsys, session_context,
):
    event = desktop_event(
        tmp_path, originator="codex-tui" if session_context == "codex_tui_without_task" else "Codex Desktop",
    )
    if session_context == "desktop_with_task":
        event["kanban_task_id"] = "bound-task"
    automation_disabled = session_context == "desktop_without_task"
    engine = importlib.import_module("stop_review_validate_fix")
    guardian = importlib.import_module("agentic_initiation_guardian_dispatch")
    from core.run_ledger.run_ledger import start_run
    ledger = start_run("test-gui-policy")
    lifecycle_calls = []
    translation_calls = []
    original_render = engine.render_stop_hook_payload
    def render_payload(**kwargs):
        translation_calls.append(kwargs)
        return original_render(**kwargs)
    def guardian_alarm():
        lifecycle_calls.append("alarm")
        return {"message": "应当只向正常自动分支显示的告警"}
    def normal_stop_decision(*args):
        assert not automation_disabled, "关闭策略之后不得评估自动 Stop 流程"
        return engine.StopDecision(
            action="skip", reason_code="test_normal_stop",
            payload={"continue": True, "systemMessage": "正常停止"},
        )
    monkeypatch.setattr(engine, "read_event", lambda: event)
    monkeypatch.setattr(engine, "start_stop_hook_ledger", lambda _event: ledger)
    monkeypatch.setattr(engine, "render_stop_hook_payload", render_payload)
    monkeypatch.setattr(engine, "evaluate_stop_event", normal_stop_decision)
    monkeypatch.setattr(engine, "sweep_stranded_kanban_followup_pending", lambda *_: lifecycle_calls.append("sweep"))
    monkeypatch.setattr(engine, "_record_guardian_stop_event_heartbeat", lambda: lifecycle_calls.append("heartbeat"))
    monkeypatch.setattr(engine, "unpark_self_parked_card", lambda *_, **__: None)
    monkeypatch.setattr(guardian, "evaluate_initiation_layer_liveness", guardian_alarm)

    assert engine.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["continue"] is True
    assert "decision" not in payload and "hookSpecificOutput" not in payload
    assert len(translation_calls) == 1
    assert translation_calls[0]["audience"] == "developer_only"
    if automation_disabled:
        assert lifecycle_calls == []
        assert "RVF GUARDIAN" not in payload["systemMessage"]
    else:
        assert lifecycle_calls == ["sweep", "heartbeat", "alarm"]
        assert "RVF GUARDIAN" in payload["systemMessage"]
    emitted_records = [
        json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "stop_hook_payload_emitted"
    ]
    assert len(emitted_records) == 1
    assert emitted_records[0]["stop_hook_output_audience"] == "developer_only"
    assert emitted_records[0]["emitted_payload"] == payload


def test_core_decision_guard_precedes_in_progress_and_dirty_gates(tmp_path, monkeypatch):
    event = desktop_event(tmp_path)
    engine = importlib.import_module("stop_review_validate_fix")
    from core.run_ledger.run_ledger import start_run
    def forbidden(*args, **kwargs):
        pytest.fail("关闭策略之后不得进入自动 gate")
    for name in ("awaiting_dispatched_agent_decision", "kanban_followup_in_progress_decision", "run_gate"):
        monkeypatch.setattr(engine, name, forbidden)
    decision = engine.evaluate_stop_event(event, start_run("test-gui-policy"))
    assert decision.reason_code == "codex_gui_without_kanban_task_stop_hook_disabled"
    assert decision.audience == "developer_only"


@pytest.mark.parametrize("event_has_cwd", [True, False])
@pytest.mark.parametrize("event_session_field", [
    "session_id", "thread_id", "sessionId", "parent_thread_id",
])
def test_handoff_finalizes_idempotently_and_releases_only_this_session_marker(
    tmp_path, event_has_cwd, event_session_field,
):
    event = desktop_event(tmp_path)
    event.pop("session_id")
    event[event_session_field] = "event-session"
    canonical_session_id = "event-session" if event_session_field == "session_id" else "desktop-session"
    if not event_has_cwd:
        event.pop("cwd")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    module = policy_module()
    from core.run_ledger.run_ledger import start_run
    from kanban_followup_lock import marker_paths
    run_dir = tmp_path / "manual-run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    handoff = artifacts / "handoff.md"
    handoff.write_text("# 手动 RVF 交接\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": "manual-run", "repo": str(tmp_path), "status": "completed",
    }), encoding="utf-8")
    (artifacts / "origin.json").write_text(json.dumps({
        "source_kind": "cline-kanban-task", "kanban_task_id": "parent-task",
    }), encoding="utf-8")
    event["last_assistant_message"] = f"RVF_HANDOFF_FILE: {handoff}"
    own_marker = marker_paths(task_id=None, session_id=canonical_session_id, repo=tmp_path)[0]
    other_session_id = "desktop-session" if canonical_session_id == "event-session" else "event-session"
    other_marker = marker_paths(task_id=None, session_id=other_session_id, repo=tmp_path)[0]
    parent_task_marker = marker_paths(task_id="parent-task", session_id=None, repo=tmp_path)[0]
    own_marker.parent.mkdir(parents=True, exist_ok=True)
    own_marker.write_text("{}", encoding="utf-8")
    other_marker.write_text("{}", encoding="utf-8")
    parent_task_marker.write_text("{}", encoding="utf-8")
    payload = module.disabled_codex_gui_stop_hook_payload(event, start_run("test-gui-policy"))
    assert payload["continue"] is True
    assert not own_marker.exists()
    assert other_marker.exists()
    assert parent_task_marker.exists()
    finalized = (artifacts / ".finalize.lock").read_text(encoding="utf-8")
    assert json.loads(finalized)["decision_kind"] == "codex-gui-without-kanban-task-silent-handoff"
    module.disabled_codex_gui_stop_hook_payload(event, start_run("test-gui-policy"))
    assert (artifacts / ".finalize.lock").read_text(encoding="utf-8") == finalized


@pytest.mark.parametrize("suppression", ["RVF_SUPPRESS", "RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"])
def test_explicit_suppression_does_not_finalize(tmp_path, monkeypatch, suppression):
    event = desktop_event(tmp_path)
    event["last_assistant_message"] = f"RVF_HANDOFF_FILE: {tmp_path / 'handoff.md'}"
    (tmp_path / "handoff.md").write_text("# 交接\n", encoding="utf-8")
    monkeypatch.setenv(suppression, "1")
    def forbidden(**kwargs):
        pytest.fail("显式 suppress 的子线程不应收尾")
    module = policy_module()
    monkeypatch.setattr(module, "finalize_for_handoff", forbidden)
    from core.run_ledger.run_ledger import start_run
    assert module.disabled_codex_gui_stop_hook_payload(event, start_run("test-gui-policy"))["continue"]


@pytest.mark.parametrize("suppression_source", [
    "last_user_message", "suppress_review_validate_fix", "review_validate_fix_suppressed",
    "transcript_event", "transcript_response", "earlier_transcript_user",
])
def test_event_or_transcript_explicit_suppression_preserves_handoff_and_marker(
    tmp_path, monkeypatch, suppression_source,
):
    event = desktop_event(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# 交接\n", encoding="utf-8")
    event["last_assistant_message"] = f"RVF_HANDOFF_FILE: {handoff}"
    suppression_text = "RVF_SUPPRESS_STOP_HOOK=1"
    if suppression_source == "last_user_message":
        event["last_user_message"] = suppression_text
    elif suppression_source in {"suppress_review_validate_fix", "review_validate_fix_suppressed"}:
        event[suppression_source] = True
    else:
        if suppression_source == "transcript_response":
            suppression_record = {
                "type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": suppression_text}],
                },
            }
        else:
            suppression_record = {
                "type": "event_msg", "payload": {"type": "user_message", "message": suppression_text},
            }
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(suppression_record) + "\n")
            if suppression_source == "earlier_transcript_user":
                handle.write(json.dumps({
                    "type": "event_msg", "payload": {"type": "user_message", "message": "继续手动工作"},
                }) + "\n")
        event["last_user_message"] = "当前消息没有 suppress"

    from kanban_followup_lock import marker_paths
    own_marker = marker_paths(task_id=None, session_id="desktop-session", repo=tmp_path)[0]
    own_marker.parent.mkdir(parents=True, exist_ok=True)
    own_marker.write_text("{}", encoding="utf-8")
    def forbidden(**kwargs):
        pytest.fail("显式 suppress 的会话不得 finalize 或清锁")
    module = policy_module()
    monkeypatch.setattr(module, "finalize_for_handoff", forbidden)
    monkeypatch.setattr(module, "clear_marker", forbidden)
    from core.run_ledger.run_ledger import start_run
    payload = module.disabled_codex_gui_stop_hook_payload(event, start_run("test-gui-policy"))
    assert payload["continue"] is True
    assert own_marker.exists()
    assert not (tmp_path / ".finalize.lock").exists()


def test_finalize_failure_is_developer_only_and_preserves_marker(tmp_path, monkeypatch):
    event = desktop_event(tmp_path)
    module = policy_module()
    handoff = tmp_path / "artifacts" / "handoff.md"
    handoff.parent.mkdir()
    handoff.write_text("# 手动 RVF 交接\n", encoding="utf-8")
    event["last_assistant_message"] = f"RVF_HANDOFF_FILE: {handoff}"
    def failed_finalize(**kwargs):
        raise OSError("收尾失败")
    monkeypatch.setattr(module, "finalize_for_handoff", failed_finalize)
    from core.run_ledger.run_ledger import start_run
    payload = module.disabled_codex_gui_stop_hook_payload(event, start_run("test-gui-policy"))
    assert payload["continue"] is True
    assert "decision" not in payload


def test_manual_user_prompt_entry_is_not_suppressed(tmp_path, monkeypatch):
    event = desktop_event(tmp_path)
    event["prompt"] = "/review-validate-fix"
    ups = importlib.import_module("rvf_user_prompt_submit")
    calls = []
    monkeypatch.setattr(ups, "_handle_manual_decision_point", lambda *args, **kwargs: calls.append((args, kwargs)) or {"kind": "manual_no_candidates"})
    result = ups.inspect_user_prompt_submit(event)
    assert calls, result


def test_real_manual_user_prompt_mints_a_decision_token(tmp_path):
    event = desktop_event(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    changed = tmp_path / "changed.py"
    changed.write_text("print('手动审查')\n", encoding="utf-8")
    event["prompt"] = "/review-validate-fix scope: changed.py"
    ups = importlib.import_module("rvf_user_prompt_submit")
    result = ups.inspect_user_prompt_submit(event)
    assert result["status"] == "manual_decision_injected", result
    wire = ups._render_hook_payload(result)
    assert "RVF_DECISION_TOKEN=" in wire["hookSpecificOutput"]["additionalContext"]
