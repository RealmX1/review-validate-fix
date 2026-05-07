#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "codex_stop_review_validate_fix.py"
)
DIAGNOSTIC_SCRIPT = SCRIPT.with_name("diagnose_codex_fork.py")
RVF_HANDOFF = SCRIPT.with_name("rvf_handoff.py")

for _name in tuple(os.environ):
    if _name.startswith("CODEX_RVF_"):
        os.environ.pop(_name, None)


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def init_repo(path: Path, dirty: bool) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], path)
    if dirty:
        (path / "changed.txt").write_text("dirty\n", encoding="utf-8")
    return path


def init_repo_with_head(path: Path) -> Path:
    repo = init_repo(path, dirty=False)
    run(["git", "config", "user.email", "rvf@example.test"], repo)
    run(["git", "config", "user.name", "RVF Test"], repo)
    (repo / "changed.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "changed.txt"], repo)
    run(["git", "commit", "-q", "-m", "base"], repo)
    (repo / "changed.txt").write_text("dirty\n", encoding="utf-8")
    return repo


def write_apply_patch_transcript(
    path: Path,
    repo: Path,
    rel_path: str = "changed.txt",
    session_id: str = "parent-thread",
) -> Path:
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {rel_path}\n"
        "@@\n"
        "-base\n"
        "+dirty\n"
        "*** End Patch\n"
    )
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(repo)}})
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": patch,
                    "call_id": "call_patch",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_assistant_handoff_transcript(path: Path, handoff: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": f"完成。\nRVF_HANDOFF_FILE: {handoff}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_fake_opener(path: Path, marker: Path, *, fail: bool = False) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(sys.argv[-1], encoding='utf-8')\n"
        + ("sys.exit(5)\n" if fail else ""),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def invoke(
    event: dict[str, object],
    config: Path | None = None,
    extra_env: dict[str, str] | None = None,
    state_dir: Path | None = None,
) -> tuple[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_") or name.startswith("KANBAN_") or name.startswith("CLINE_KANBAN_"):
            env.pop(name, None)
    env.pop("CODEX_THREAD_ID", None)
    if config is not None:
        env["CODEX_RVF_CONFIG"] = str(config)
    if state_dir is not None:
        env["CODEX_RVF_STATE_DIR"] = str(state_dir)
    if extra_env is not None:
        env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return completed.stdout, completed.stderr


def write_config(path: Path, projects: list[Path]) -> None:
    body = []
    for project in projects:
        body.extend(
            [
                f'[projects."{project}"]',
                'trust_level = "trusted"',
                "",
            ]
        )
    path.write_text("\n".join(body), encoding="utf-8")


def parse_json(stdout: str) -> dict[str, object]:
    assert stdout.strip(), "expected JSON stdout"
    return json.loads(stdout)


def summary_path_from_message(message: str) -> Path:
    marker = "summary="
    assert marker in message, message
    return Path(message.split(marker, 1)[1].split(";", 1)[0].strip())


def summary_from_payload(payload: dict[str, object]) -> dict[str, object]:
    message = payload["systemMessage"]
    assert isinstance(message, str)
    return json.loads(summary_path_from_message(message).read_text(encoding="utf-8"))


def latest_pointer(state: Path) -> dict[str, object]:
    return json.loads((state / "latest.json").read_text(encoding="utf-8"))


def latest_summary(state: Path) -> dict[str, object]:
    pointer = latest_pointer(state)
    assert set(pointer) >= {
        "run_id",
        "summary_path",
        "events_path",
        "status",
        "reason_code",
        "updated_at",
    }
    return json.loads(Path(str(pointer["summary_path"])).read_text(encoding="utf-8"))


def latest_events(state: Path) -> list[dict[str, object]]:
    pointer = latest_pointer(state)
    return [
        json.loads(line)
        for line in Path(str(pointer["events_path"])).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_json_artifact(summary: dict[str, object], key: str) -> object:
    path = summary.get(key)
    assert isinstance(path, str), f"missing artifact path {key}: {summary}"
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_text_artifact(summary: dict[str, object], key: str) -> str:
    path = summary.get(key)
    assert isinstance(path, str), f"missing artifact path {key}: {summary}"
    return Path(path).read_text(encoding="utf-8")


def prompt_text(summary: dict[str, object]) -> str:
    return read_text_artifact(summary, "prompt_path")


def dispatch_prep_payload(summary: dict[str, object]) -> dict[str, object]:
    path = summary.get("rvf_dispatch_prep_file_path")
    assert isinstance(path, str), f"missing dispatch prep file path: {summary}"
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def app_server_requests(summary: dict[str, object]) -> list[dict[str, object]]:
    requests = read_json_artifact(summary, "app_server_requests_path")
    assert isinstance(requests, list)
    return requests


def assert_skip_reason(stdout: str, expected: str) -> dict[str, object]:
    payload = parse_json(stdout)
    assert "decision" not in payload
    assert str(payload["systemMessage"]).startswith("review-validate-fix: skipped;")
    summary = summary_from_payload(payload)
    assert summary["status"] == "skipped"
    assert expected in str(summary.get("message"))
    return payload


def load_hook_module():
    spec = importlib.util.spec_from_file_location("rvf_stop_hook_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_workspace_snapshot_module():
    script = SCRIPT.with_name("workspace_snapshot.py")
    spec = importlib.util.spec_from_file_location("rvf_workspace_snapshot_for_tests", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def seed_finalize_run_dir(
    *,
    state: Path,
    repo: Path,
    run_id: str = "rvf-child",
) -> tuple[Path, Path]:
    run_dir = state / "runs" / run_id
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    handoff = artifacts / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "started",
                "reason_code": "test",
                "repo": str(repo),
                "events_path": str(run_dir / "events.jsonl"),
                "artifacts_dir": str(artifacts),
                "run_dir": str(run_dir),
            }
        ),
        encoding="utf-8",
    )
    snapshot_module = load_workspace_snapshot_module()
    (artifacts / "before-workspace-snapshot.json").write_text(
        json.dumps(snapshot_module.capture(repo), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, handoff


def write_same_session_transcript_with_marker(path: Path, repo: Path) -> Path:
    records = [
        {"type": "session_meta", "payload": {"id": "child-session", "cwd": str(repo)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "background work"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ack"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "go RVF_FORKED_REVIEW_VALIDATE_FIX",
            },
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "running rvf"}},
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_normalize_backend_from_env(tmp_path: Path) -> None:
    module = load_hook_module()
    original = {
        name: os.environ.get(name)
        for name in (
            "CODEX_RVF_MODE",
            "CODEX_RVF_FORK_MODE",
            "KANBAN_TASK_ID",
            "CLINE_KANBAN_TASK_ID",
            "KANBAN_HOOK_TASK_ID",
        )
    }
    cases = [
        ({}, {}, "kanban"),
        ({"CODEX_RVF_MODE": "off"}, {}, "off"),
        ({"CODEX_RVF_MODE": "continuation"}, {}, "report-only"),
        ({"CODEX_RVF_MODE": "block"}, {}, "report-only"),
        ({"CODEX_RVF_FORK_MODE": "auto"}, {}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "auto"}, {"task_id": "task-1"}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "auto", "KANBAN_TASK_ID": "task-2"}, {}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "auto", "KANBAN_HOOK_TASK_ID": "task-legacy"}, {}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "manual"}, {}, "manual"),
        ({"CODEX_RVF_FORK_MODE": "prepare"}, {}, "manual"),
        ({"CODEX_RVF_FORK_MODE": "dry-run"}, {}, "dry-run"),
        ({"CODEX_RVF_FORK_MODE": "cline-kanban"}, {}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "cline"}, {}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "ck"}, {}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "kanban-followup"}, {}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "kanban-message"}, {}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "kanban-inject"}, {}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": ""}, {}, ""),
        ({"CODEX_RVF_FORK_MODE": "surprise"}, {}, "surprise"),
    ]
    try:
        for env, event, expected in cases:
            for name in original:
                os.environ.pop(name, None)
            os.environ.update(env)
            assert module.normalize_backend_from_env(event) == expected
            if env.get("CODEX_RVF_FORK_MODE") == "":
                assert module.fork_mode_selection_from_env() == "explicit"
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_dispatch_flow_helpers_lock_route_and_fallback_contract(tmp_path: Path) -> None:
    module = load_hook_module()
    flow = module.dispatch_flow

    assert flow.backend_from_values(mode=None, fork_mode="auto", in_kanban_task=False) == "kanban"
    assert flow.backend_from_values(mode=None, fork_mode="auto", in_kanban_task=True) == "kanban-followup"
    assert flow.backend_from_values(mode=None, fork_mode="gui", in_kanban_task=False) == "gui"
    assert flow.backend_from_values(mode=None, fork_mode="cline", in_kanban_task=False) == "kanban"
    assert flow.backend_from_values(mode=None, fork_mode="kanban-message", in_kanban_task=False) == "kanban-followup"
    assert flow.backend_from_values(mode=None, fork_mode="prepare", in_kanban_task=False) == "manual"
    assert flow.backend_from_values(mode=None, fork_mode="", in_kanban_task=False) == ""
    assert flow.backend_from_values(mode=None, fork_mode=None, in_kanban_task=False) == "kanban"
    assert flow.backend_from_values(mode="off", fork_mode="auto", in_kanban_task=True) == "off"
    assert flow.backend_from_values(mode="continuation", fork_mode="auto", in_kanban_task=True) == "report-only"
    assert flow.backend_selection_mode_from_fork_mode("") == "explicit"
    assert flow.backend_selection_mode_from_fork_mode(None) == "auto"
    assert flow.backend_selection_mode_from_fork_mode("detect") == "auto"
    assert flow.backend_selection_mode_from_fork_mode("cline-kanban") == "explicit"
    assert flow.launch_mode_for_backend("kanban") == "cline-kanban"
    assert flow.launch_mode_for_backend("gui") == "gui"

    recoverable = {"status": "cline-kanban-unavailable", "error": "kanban unavailable"}
    management_plane_error = {
        "status": "cline-kanban-unavailable",
        "error": "no listener pane belongs to tmux session `cline-kanban`; Stop the foreign listener",
    }
    assert flow.should_attempt_legacy_gui_fallback(
        primary_result=recoverable,
        backend_selection_mode="auto",
        fallback_enabled=True,
    )
    assert not flow.should_attempt_legacy_gui_fallback(
        primary_result=recoverable,
        backend_selection_mode="explicit",
        fallback_enabled=True,
    )
    assert not flow.should_attempt_legacy_gui_fallback(
        primary_result=recoverable,
        backend_selection_mode="auto",
        fallback_enabled=False,
    )
    assert not flow.should_attempt_legacy_gui_fallback(
        primary_result=management_plane_error,
        backend_selection_mode="auto",
        fallback_enabled=True,
    )


def test_parent_conversation_origin_prefers_app_server_chat_name(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        parent_thread_name="Find RVF_STOP_HOOK behavior",
        name_lookup={"name": "Find RVF_STOP_HOOK behavior", "source": "desktop-control"},
    )

    assert origin["label"] == "Find RVF_STOP_HOOK behavior"
    assert origin["name_source"] == "app_server_name"
    assert origin["task_title"] == "RVF from Find RVF_STOP_HOOK behavior run 562915ad"
    assert origin["codex_url"] == "codex://local/019de191-ba6c-7b13-9874-65eeabb6a6a7"
    assert origin["transcript_file"] == transcript.name


def test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    first_prompt = (
        "for the path in RVF hook fork to cline kanban, we need way to trace "
        "which original conversation the fork comes from"
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": first_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
    )

    assert origin["label"] == '"for the path in RVF hook fork to cline kanban, we need way t"'
    assert origin["name_source"] == "first_user_prompt_fallback"
    assert origin["task_title"] == (
        'RVF from "for the path in RVF hook fork to cline kanban, we need way t" run 562915ad'
    )


def test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    user_prompt = (
        "currently the fallback chat session name in handoff as well as cline-task "
        "is incorrectly using the stitched prompt"
    )
    stitched_prompt = (
        "# AGENTS.md instructions for /Users/bominzhang/Documents/GitHub/review-validate-fix\n\n"
        "<INSTRUCTIONS>\n"
        "你应该默认使用中文作为主要语言进行回复。\n"
        "</INSTRUCTIONS><environment_context>\n"
        "  <cwd>/Users/bominzhang/Documents/GitHub/review-validate-fix</cwd>\n"
        "</environment_context>\n"
        f"{user_prompt}"
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": stitched_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
    )

    expected_excerpt = module.single_line_excerpt(
        user_prompt,
        module.DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS,
    )
    assert origin["label"] == f'"{expected_excerpt}"'
    assert "AGENTS.md instructions" not in origin["task_title"]
    assert origin["name_source"] == "first_user_prompt_fallback"


def test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    context_only = (
        "# AGENTS.md instructions for /Users/bominzhang/Documents/GitHub/review-validate-fix\n\n"
        "<INSTRUCTIONS>\n"
        "project instructions\n"
        "</INSTRUCTIONS><environment_context>\n"
        "  <cwd>/Users/bominzhang/Documents/GitHub/review-validate-fix</cwd>\n"
        "</environment_context>\n"
    )
    user_prompt = "please run review validate fix for the current change"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": context_only},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
    )

    assert origin["label"] == f'"{user_prompt}"'
    assert "AGENTS.md instructions" not in origin["task_title"]


def test_parent_conversation_origin_uses_stable_ref_when_chat_lookup_fails(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "this prompt must not be quoted"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        name_lookup={"name": None, "source": "unavailable", "error": "socket unavailable"},
    )

    assert origin["label"] == "Codex 2026-05-01T11-25-17 019de191"
    assert origin["name_source"] == "session_ref_fallback"
    assert '"' not in origin["task_title"]


def test_rvf_fork_prompt_includes_parent_origin_metadata_for_legacy_gui(tmp_path: Path) -> None:
    module = load_hook_module()
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-parent-thread.jsonl"
    write_user_session(transcript, "parent-thread", "please review the Stop hook change")

    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_lookup = module.parent_thread_name_from_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": "Original GUI Review",
            "thread_found": True,
            "source": "test",
        }
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp_path),
            prompt=module.fork_review_validate_fix_prompt(
                "parent-thread",
                str(tmp_path),
                str(tmp_path / "repo"),
            ),
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=transcript,
            launch_mode="dry-run",
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        if original_state_dir is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = original_state_dir

    assert "reason=dry_run" in payload["systemMessage"]
    latest = latest_summary(state)
    prompt = prompt_text(latest)
    origin_path = latest["parent_origin_path"]
    assert "RVF_PARENT_CONVERSATION_REF: Original GUI Review" in prompt
    assert "RVF_PARENT_CONVERSATION_NAME: Original GUI Review" in prompt
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: app_server_name" in prompt
    assert "RVF_PARENT_CODEX_URL: codex://local/parent-thread" in prompt
    assert f"RVF_PARENT_TRANSCRIPT_PATH: {transcript}" in prompt
    assert f"RVF_ORIGIN_METADATA: {origin_path}" in prompt
    assert "不要把 `RVF_PARENT_SESSION_ID` 当成 conversation name source" in prompt
    requests = app_server_requests(latest)
    turn_input = requests[1]["params"]["input"][0]
    assert isinstance(turn_input, dict)
    assert turn_input["text"] == prompt


def test_parent_thread_name_from_app_server_reads_thread_name(tmp_path: Path) -> None:
    module = load_hook_module()
    socket_path = tmp_path / "app-server.sock"
    calls: list[tuple[str, dict[str, object] | None]] = []
    notifications: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, socket: Path) -> None:
            assert socket == socket_path
            self.notifications: list[dict[str, object]] = []

        def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
            calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "parent-thread",
                        "name": "Find RVF_STOP_HOOK behavior",
                    }
                }
            raise AssertionError(method)

        def send_json(self, payload: dict[str, object]) -> None:
            notifications.append(payload)

        def close(self) -> None:
            pass

    original_client = module.AppServerWebSocket
    original_select = module.select_existing_app_server_socket_for_metadata
    try:
        module.AppServerWebSocket = FakeClient
        module.select_existing_app_server_socket_for_metadata = lambda: (
            socket_path,
            "desktop-control",
            {},
        )
        lookup = module.parent_thread_name_from_app_server("parent-thread", str(tmp_path))
    finally:
        module.AppServerWebSocket = original_client
        module.select_existing_app_server_socket_for_metadata = original_select

    assert lookup["name"] == "Find RVF_STOP_HOOK behavior"
    assert lookup["thread_found"] is True
    assert lookup["source"] == "desktop-control"
    assert lookup["method"] == "thread/read"
    assert calls[0][0] == "initialize"
    assert calls[1] == ("thread/read", {"threadId": "parent-thread", "includeTurns": False})
    assert notifications == [{"method": "initialized"}]


def write_subagent_session(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent",
                                "depth": 1,
                            }
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def write_user_session(path: Path, session_id: str, message: str) -> None:
    write_user_session_messages(path, session_id, [message])


def write_user_session_messages(path: Path, session_id: str, messages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(path.parent),
            },
        }
    ]
    for message in messages:
        lines.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": message,
                },
            }
        )
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )


def test_fork_experiment_marker_no_longer_triggers_stop_hook_fork(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "state"
    write_user_session(
        transcript,
        "00000000-0000-0000-0000-000000000001",
        "RVF_FORK_EXPERIMENT: what is 2+2?",
    )
    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_EXPERIMENT_MODE": "dry-run",
        },
        state_dir=state,
    )
    payload = parse_json(stdout)
    assert "decision" not in payload
    assert "reason=dry_run" not in payload["systemMessage"]
    assert "reason=cwd_not_git_repo" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "cwd_not_git_repo"
    assert "app_server_requests_path" not in latest


def test_diagnose_codex_fork_dry_run_writes_requests(tmp_path: Path) -> None:
    state = tmp_path / "state"
    message = "RVF_FORK_EXPERIMENT: custom diagnostic message"
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_"):
            env.pop(name, None)
    env["CODEX_RVF_STATE_DIR"] = str(state)
    completed = subprocess.run(
        [sys.executable, str(DIAGNOSTIC_SCRIPT), "--mode", "dry-run", "--message", message],
        input=json.dumps({"session_id": "parent-thread", "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )

    payload = parse_json(completed.stdout)
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "dry-run"
    assert read_text_artifact(latest, "latest_user_message_path") == message
    requests = app_server_requests(latest)
    assert requests[0]["method"] == "thread/fork"
    assert requests[1]["method"] == "turn/start"


def test_stop_hook_active_skips(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    stdout, _ = invoke({"cwd": str(dirty), "stop_hook_active": True})
    payload = assert_skip_reason(stdout, "stop_hook_active=true")
    assert "detail=Codex 已在执行 Stop hook，RVF 跳过以避免递归" in payload["systemMessage"]


def test_env_suppression_skips(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    run_dir = tmp_path / "state" / "runs" / "rvf-child"
    stdout, _ = invoke(
        {"cwd": str(dirty), "stop_hook_active": False},
        extra_env={
            "CODEX_RVF_SUPPRESS_STOP_HOOK": "1",
            "CODEX_RVF_RUN_ID": "rvf-child",
            "CODEX_RVF_RUN_DIR": str(run_dir),
        },
    )
    payload = parse_json(stdout)
    assert "review-validate-fix: skipped; reason=suppressed;" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"
    assert summary["run_id"] == "rvf-child"
    assert run_dir.exists()


def test_prompt_suppression_marker_skips(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "state"
    write_user_session(
        transcript,
        "00000000-0000-0000-0000-000000000201",
        "diagnostic fork\n\nCODEX_RVF_SUPPRESS_STOP_HOOK=1",
    )
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    payload = parse_json(stdout)
    assert "review-validate-fix: skipped; reason=suppressed;" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"


def test_prior_cline_kanban_task_marker_skips_after_later_user_message(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "state"
    write_user_session_messages(
        transcript,
        "00000000-0000-0000-0000-000000000202",
        [
            "$review-validate-fix\n\nRVF_CLINE_KANBAN_TASK\nCODEX_RVF_SUPPRESS_STOP_HOOK=1",
            "later Kanban user turn without the suppress marker",
        ],
    )
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    payload = parse_json(stdout)
    assert "review-validate-fix: skipped; reason=suppressed;" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"


def test_kanban_task_suppression_marker_skips_without_prompt_marker(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    marker_dir = state / "kanban-task-suppressions"
    marker_dir.mkdir(parents=True)
    (marker_dir / "task-202.json").write_text(
        json.dumps(
            {
                "task_id": "task-202",
                "suppress_stop_hook": True,
                "reason": "rvf-created-cline-kanban-task",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
        },
        extra_env={"KANBAN_TASK_ID": "task-202"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "review-validate-fix: skipped; reason=suppressed;" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"


def test_session_without_owned_dirty_skips_fork(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "state"
    write_user_session(
        transcript,
        "session-with-background-dirty",
        "只是查看状态，没有修改文件。",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )

    # Slice 3 reason-code rename: dispatcher / packet-side assertions still
    # see the legacy substring (D4 transitional alias) but the structured
    # reason_code field has flipped to the new name.
    payload = assert_skip_reason(stdout, "no unassigned review scope")
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "no_unassigned_review_scope"
    assert summary.get("reason_code_legacy_alias") == "no_session_owned_dirty"
    assert summary["session_change_type"] == "no_codebase_changes"
    assert "app_server_requests_path" not in summary


def test_session_without_owned_dirty_legacy_disable_keeps_old_codes(tmp: Path) -> None:
    """`CODEX_RVF_TRACKER_DISABLE=1` must preserve Phase-0 reason codes byte-
    for-byte so disable-mode users see no churn during the Slice 3 rename."""
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
    state = tmp / "state"
    write_user_session(
        transcript,
        "session-disabled-tracker",
        "只是查看状态，没有修改文件。",
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "dry-run",
            "CODEX_RVF_TRACKER_DISABLE": "1",
        },
        state_dir=state,
    )

    payload = assert_skip_reason(stdout, "no session-owned dirty paths")
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "no_session_owned_dirty"


def _make_test_ledger(module, state: Path):
    """Set CODEX_RVF_LOG_ROOT to `state` and return a fresh stop-hook ledger.
    Caller is responsible for clearing the env when done."""
    state.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_RVF_LOG_ROOT"] = str(state)
    return module.start_run("stop-hook-test", repo=str(state), cwd=str(state))


def test_resolve_stop_context_returns_session_branch_worktree(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
    write_user_session(transcript, "sess-resolve-ctx", "只是查看状态，没有修改文件。")
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    try:
        ledger = _make_test_ledger(module, state)
        event = {
            "cwd": str(repo),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        }
        context = module.resolve_stop_context(event, str(repo), ledger)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
    assert context["repo"] == str(repo)
    assert context["cwd"] == str(repo)
    assert context["session_id"] == "sess-resolve-ctx"
    # `first_readable_session_path` resolves the path; compare resolved.
    assert context["transcript"] == transcript.resolve()
    assert context["session_paths"] == [transcript]


def test_evaluate_session_gate_suppresses_on_manual_marker(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_user_session(transcript, "sess-marker", "工作完成了。")
    state = tmp / "state"
    state_dir_root = tmp / "state-root"
    state_dir_root.mkdir()
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    os.environ["CODEX_RVF_STATE_DIR"] = str(state_dir_root)
    try:
        ledger = _make_test_ledger(module, state)
        marker_path = module.write_manual_rvf_session_marker(
            session_id="sess-marker",
            run_id="manual-run",
            repo=repo,
        )
        assert Path(marker_path).exists()
        event = {
            "cwd": str(repo),
            "transcript_path": str(transcript),
        }
        context = module.resolve_stop_context(event, str(repo), ledger)
        gated = module.evaluate_session_gate(context, ledger)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
    assert gated is not None
    # systemMessage carries `reason=manual_rvf_already_ran`; summary file holds
    # the structured `manual_rvf_run_id` field.
    assert "manual_rvf_already_ran" in gated.get("systemMessage", "")


def test_legacy_session_scope_gate_payload_used_when_tracker_disabled(tmp: Path) -> None:
    """With `CODEX_RVF_TRACKER_DISABLE=1`, the orchestrator must delegate to
    the verbatim Phase-0 body so legacy `session_owned_dirty` reason codes
    flow through unchanged."""
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: changed.txt\n"
        "@@\n"
        "-base\n"
        "+dirty\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "sess-legacy", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_disable = os.environ.get("CODEX_RVF_TRACKER_DISABLE")
    os.environ["CODEX_RVF_TRACKER_DISABLE"] = "1"
    try:
        ledger = _make_test_ledger(module, state)
        event = {
            "cwd": str(repo),
            "transcript_path": str(transcript),
        }
        result = module.session_scope_gate_payload(event, str(repo), ledger)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_disable is None:
            os.environ.pop("CODEX_RVF_TRACKER_DISABLE", None)
        else:
            os.environ["CODEX_RVF_TRACKER_DISABLE"] = old_disable
    assert result is None  # session-owned dirty → legacy path returns None to continue
    # Verify the legacy reason code went through the events log.
    events_path = ledger.events_path
    raw = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert "session_owned_dirty" in raw
    assert "no_unassigned_review_scope" not in raw


def test_allocate_auto_review_scope_writes_artifact_when_scope_present(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    # Transcript with apply_patch on changed.txt — this is what gives
    # `register_claims` a session-owned attribution to seed `session_units`.
    apply_patch = (
        "*** Begin Patch\n"
        "*** Update File: changed.txt\n"
        "@@\n"
        "-base\n"
        "+dirty\n"
        "*** End Patch\n"
    )
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sess-alloc", "cwd": str(repo)}}, ensure_ascii=False)
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": apply_patch,
                    "call_id": "call_patch",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    try:
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        context = module.resolve_stop_context(event, str(repo), ledger)
        # `refresh_global_diff_tracker` seeds session_units via build_manifest.
        module.refresh_global_diff_tracker(context, ledger)
        result = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
    # Allocator allocated → returns None (Stop hook continues to fork).
    assert result is None
    artifacts_dir = state / "runs" / ledger.run_id / "artifacts"
    assert (artifacts_dir / "tracker-scope.json").exists()
    # D12: tracker meta is stashed on the ledger as a convention.
    meta = getattr(ledger, "tracker_scope_meta", None)
    assert isinstance(meta, dict)
    assert "tracker_scope_path" in meta
    assert meta["tracker_lease_id"] is not None
    assert meta["tracker_scope_hash"] is not None


def test_kanban_followup_auto_review_scope_uses_one_hour_lease_ttl(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-followup-ttl")
    state = tmp / "state"
    original_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    try:
        os.environ["CODEX_RVF_FORK_MODE"] = "kanban-followup"
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript), "task_id": "task-ttl"}
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        result = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if original_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log
        if original_mode is None:
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
        else:
            os.environ["CODEX_RVF_FORK_MODE"] = original_mode

    assert result is None
    meta = getattr(ledger, "tracker_scope_meta", None)
    assert isinstance(meta, dict)
    db_path = Path(meta["tracker_dir"]) / "tracker.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT ttl_seconds FROM leases WHERE lease_id=?",
            (meta["tracker_lease_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 3600


def test_kanban_followup_without_task_id_does_not_allocate_review_scope(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-followup-no-task")
    state = tmp / "state"
    original_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    task_env_names = ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID")
    original_task_env = {name: os.environ.get(name) for name in task_env_names}
    try:
        os.environ["CODEX_RVF_FORK_MODE"] = "kanban-followup"
        for name in task_env_names:
            os.environ.pop(name, None)
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        result = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if original_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log
        if original_mode is None:
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
        else:
            os.environ["CODEX_RVF_FORK_MODE"] = original_mode
        for name, value in original_task_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert result is not None
    assert "reason=kanban_followup_missing_task_id" in result["systemMessage"]
    meta = getattr(ledger, "tracker_scope_meta", None)
    assert meta is None

    diff_tracker = sys.modules["diff_tracker"]
    _, _, tracker_dir, db_path, _, _ = diff_tracker._lease_repo_paths(
        repo,
        log_root_override=state / "global-diff-tracker",
    )
    assert Path(tracker_dir).exists()
    conn = sqlite3.connect(str(db_path))
    try:
        has_leases_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='leases'"
        ).fetchone()[0]
        count = (
            conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            if has_leases_table
            else 0
        )
    finally:
        conn.close()
    assert count == 0


def test_evaluate_session_gate_skips_when_manual_run_recorded_for_scope_hash(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_apply_patch_transcript(transcript, repo, session_id="sess-manual-db")
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    try:
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        context = module.resolve_stop_context(event, str(repo), ledger)
        module.refresh_global_diff_tracker(context, ledger)
        dry = module.allocate_auto_review_scope(context, ledger, dry_run=True)
        assert dry is not None and dry["would_proceed"] is True
        scope_hash = dry["result"]["scope_hash"]

        diff_tracker = sys.modules["diff_tracker"]
        diff_tracker.record_manual_rvf_run(
            repo=repo,
            session_id="manual-db-session",
            run_id="manual-db-run",
            scope_hash=scope_hash,
            completed_at="2026-05-05T00:00:00Z",
            log_root_override=state,
        )

        next_ledger = _make_test_ledger(module, state)
        gated = module.session_scope_gate_payload(event, str(repo), next_ledger)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
    assert gated is not None
    assert "manual_scope_already_completed" in gated.get("systemMessage", "")
    summary = summary_from_payload(gated)
    assert summary["reason_code"] == "manual_scope_already_completed"
    assert summary["manual_rvf_run_id"] == "manual-db-run"
    assert summary["tracker_scope_hash"] == scope_hash


def test_manual_scope_suppression_does_not_transfer_parent_takeover_units(tmp: Path) -> None:
    module = load_hook_module()
    diff_tracker = sys.modules["diff_tracker"]
    repo = init_repo_with_head(tmp / "dirty")
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    try:
        ledger = _make_test_ledger(module, state)
        parent = diff_tracker.allocate_review_scope(
            repo=repo,
            session_id="manual-parent-scope",
            run_id="manual-parent-run",
            reviewer_id="parent-reviewer",
            log_root_override=state,
        )
        assert parent["status"] == "allocated"
        db_path = Path(parent["tracker_dir"]) / "tracker.sqlite3"
        import sqlite3 as _sqlite

        conn = _sqlite.connect(str(db_path))
        try:
            conn.execute("UPDATE leases SET state='completed' WHERE lease_id=?", (parent["lease_id"],))
            conn.execute(
                "UPDATE units SET review_state='available' WHERE unit_id IN "
                "(SELECT unit_id FROM lease_units WHERE lease_id=?)",
                (parent["lease_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        diff_tracker.record_manual_rvf_run(
            repo=repo,
            session_id="manual-parent-scope",
            run_id="manual-completed-run",
            scope_hash=parent["scope_hash"],
            completed_at="2026-05-05T00:00:00Z",
            log_root_override=state,
        )
        context = {
            "repo": str(repo),
            "cwd": str(repo),
            "session_id": "manual-child-scope",
            "parent_session_id": "manual-parent-scope",
            "event": {"cwd": str(repo), "session_id": "manual-child-scope"},
        }
        gated = module.allocate_auto_review_scope(context, ledger, dry_run=False)
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
    assert gated is not None
    assert "manual_scope_already_completed" in gated.get("systemMessage", "")
    conn = _sqlite.connect(str(db_path))
    try:
        parent_kinds = {
            row[0]
            for row in conn.execute(
                "SELECT assignment_kind FROM session_units WHERE session_id='manual-parent-scope'"
            )
        }
        child_rows = list(
            conn.execute("SELECT assignment_kind FROM session_units WHERE session_id='manual-child-scope'")
        )
    finally:
        conn.close()
    assert parent_kinds == {"owned"}
    assert child_rows == []


def test_evaluate_session_gate_file_marker_takes_precedence_over_db_marker(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_user_session(transcript, "sess-file-marker-first", "手动 RVF 已完成。")
    state = tmp / "state"
    state_dir_root = tmp / "state-root"
    state_dir_root.mkdir()
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    original_find = module.find_manual_rvf_run_for_scope_hash
    os.environ["CODEX_RVF_STATE_DIR"] = str(state_dir_root)

    def _raise_if_db_marker_checked(*args, **kwargs):
        raise AssertionError("DB marker should not be checked before file marker")

    try:
        module.find_manual_rvf_run_for_scope_hash = _raise_if_db_marker_checked
        ledger = _make_test_ledger(module, state)
        module.write_manual_rvf_session_marker(
            session_id="sess-file-marker-first",
            run_id="file-marker-run",
            repo=repo,
        )
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        context = module.resolve_stop_context(event, str(repo), ledger)
        gated = module.evaluate_session_gate(context, ledger)
    finally:
        module.find_manual_rvf_run_for_scope_hash = original_find
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
    assert gated is not None
    assert "manual_rvf_already_ran" in gated.get("systemMessage", "")


def test_session_scope_gate_payload_emits_session_manifest_failed_when_refresh_fails(
    tmp: Path,
) -> None:
    """`refresh_global_diff_tracker` 在 `build_manifest` 抛错时返回带 `error`
    字段的 sentinel，而不再静默吞错；orchestrator 必须把它转成
    `session_manifest_failed` skip payload，与 legacy 路径行为一致。否则
    allocator 会看到空 session_units 并把 manifest 失败误判为
    `no_unassigned_review_scope` 干净跳过。"""
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "dirty")
    transcript = tmp / "session.jsonl"
    write_user_session(transcript, "sess-manifest-fail", "做了一些改动。")
    state = tmp / "state"
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_build_manifest = module.build_manifest

    def _raise(*args, **kwargs):
        raise RuntimeError("boom: synthetic build_manifest failure")

    try:
        module.build_manifest = _raise  # type: ignore[assignment]
        ledger = _make_test_ledger(module, state)
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        result = module.session_scope_gate_payload(event, str(repo), ledger)
    finally:
        module.build_manifest = original_build_manifest  # type: ignore[assignment]
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
    # orchestrator 必须返回 skip payload（不是 None，也不是
    # no_unassigned_review_scope skip）。
    assert result is not None
    system_message = result.get("systemMessage", "")
    assert "reason=session_manifest_failed" in system_message
    assert "no_unassigned_review_scope" not in system_message
    assert "no_session_owned_dirty" not in system_message
    # ledger 仍然 emit 了 `tracker_refresh_failed`，没有重复 log。
    events_path = ledger.events_path
    raw = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert "tracker_refresh_failed" in raw
    assert "session_manifest_failed" in raw


def test_session_hook_default_state_dir_is_skill_state_session_hook(tmp_path: Path) -> None:
    old_state = os.environ.pop("CODEX_RVF_STATE_DIR", None)
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    try:
        module = load_hook_module()
        expected = SCRIPT.parents[1] / "state" / "session-hook"
        assert module.session_hook_state_dir() == expected
    finally:
        if old_state is not None:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_session_hook_state_dir_respects_state_dir_override(tmp_path: Path) -> None:
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state-root")
    try:
        module = load_hook_module()
        assert module.session_hook_state_dir() == tmp_path / "state-root" / "session-hook"
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_manual_rvf_session_marker_write_read_clear_preserves_hook_state(tmp_path: Path) -> None:
    module = load_hook_module()
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state-root")
    try:
        module.set_session_hook_enabled(
            session_id="manual/session",
            enabled=False,
            latest_user="RVF_STOP_HOOK: off",
        )
        path = module.write_manual_rvf_session_marker(
            session_id="manual/session",
            run_id="rvf-manual-run",
            completed_at="2999-04-30T00:00:00+00:00",
        )
        assert path == tmp_path / "state-root" / "session-hook" / "manual_session.json"

        marker = module.read_manual_rvf_session_marker("manual/session")
        assert marker is not None
        assert marker["manual_rvf_run_id"] == "rvf-manual-run"
        assert marker["manual_rvf_completed_at"] == "2999-04-30T00:00:00+00:00"
        assert module.session_hook_disabled("manual/session") is True

        cleared = module.clear_manual_rvf_session_marker("manual/session")
        assert cleared == path
        assert module.read_manual_rvf_session_marker("manual/session") is None
        assert module.session_hook_disabled("manual/session") is True
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_manual_rvf_session_marker_skips_before_fork_gate(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    state = tmp_path / "state"
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(state)
    try:
        module.set_session_hook_enabled(
            session_id="manual-rvf-session",
            enabled=False,
            latest_user="RVF_STOP_HOOK: off",
        )
        module.write_manual_rvf_session_marker(
            session_id="manual-rvf-session",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2999-04-30T00:00:00+00:00",
        )
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "manual-rvf-session",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )

    assert "decision" not in payload
    assert "reason=manual_rvf_already_ran" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "manual_rvf_already_ran"
    assert summary["manual_rvf_run_id"] == "rvf-manual-run"
    assert summary["manual_rvf_completed_at"] == "2999-04-30T00:00:00+00:00"
    assert summary["manual_rvf_repo"] == str(dirty.resolve())
    assert summary["manual_rvf_dirty_hash"]
    assert "app_server_requests_path" not in summary
    assert latest_pointer(state)["reason_code"] == "manual_rvf_already_ran"


def test_manual_rvf_session_marker_dirty_change_does_not_suppress(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    state = tmp_path / "state"
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(state)
    try:
        module.write_manual_rvf_session_marker(
            session_id="manual-rvf-session-dirty-changed",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2999-04-30T00:00:00+00:00",
        )
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state

    (dirty / "changed.txt").write_text("new dirty content\n", encoding="utf-8")
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "manual-rvf-session-dirty-changed",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )

    assert "reason=manual_rvf_already_ran" not in payload["systemMessage"]
    assert "reason=dry_run" in payload["systemMessage"]


def test_manual_rvf_session_marker_expired_does_not_read(tmp_path: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp_path / "dirty")
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp_path / "state")
    try:
        module.write_manual_rvf_session_marker(
            session_id="manual-expired",
            run_id="rvf-manual-run",
            repo=dirty,
            completed_at="2000-01-01T00:00:00+00:00",
            ttl_seconds=1,
        )
        assert module.read_manual_rvf_session_marker("manual-expired", dirty) is None
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_socket_probe_reports_unavailable_reason(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)

    missing = tmp_path / "missing.sock"
    missing_probe = module.probe_app_server_socket(missing)
    assert missing_probe["connect_ok"] is False
    assert missing_probe["reason"] == "missing"
    assert missing_probe["parent_exists"] is True

    regular = tmp_path / "regular.sock"
    regular.write_text("not a socket\n", encoding="utf-8")
    regular_probe = module.probe_app_server_socket(regular)
    assert regular_probe["connect_ok"] is False
    assert regular_probe["reason"] == "not-a-socket"
    assert regular_probe["exists"] is True


def test_app_server_websocket_sends_http_upgrade(tmp_path: Path) -> None:
    module = load_hook_module()
    created: list[object] = []

    class FakeSocket:
        def __init__(self, *_: object) -> None:
            self.sent = b""
            self.timeout: float | None = None
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, path: str) -> None:
            assert path == str(tmp_path / "app-server.sock")

        def sendall(self, data: bytes) -> None:
            self.sent += data

        def recv(self, _: int) -> bytes:
            request = self.sent.decode("iso-8859-1")
            headers = {}
            for line in request.split("\r\n")[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            key = headers["sec-websocket-key"]
            accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            ).decode("ascii")
            return (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Connection: Upgrade\r\n"
                "Upgrade: websocket\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")

        def close(self) -> None:
            self.closed = True

    def fake_socket(*args: object) -> FakeSocket:
        sock = FakeSocket(*args)
        created.append(sock)
        return sock

    original_socket = module.socket.socket
    try:
        module.socket.socket = fake_socket
        client = module.AppServerWebSocket(tmp_path / "app-server.sock", timeout=2)
        client.close()
    finally:
        module.socket.socket = original_socket

    assert created
    request = created[0].sent.decode("iso-8859-1")
    assert request.startswith("GET / HTTP/1.1\r\n")
    assert "Upgrade: websocket\r\n" in request
    assert "Sec-WebSocket-Key:" in request


def test_app_server_websocket_masks_pong_frame(tmp_path: Path) -> None:
    module = load_hook_module()

    class FakeSocket:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, data: bytes) -> None:
            self.sent += data

    sock = FakeSocket()
    client = module.AppServerWebSocket.__new__(module.AppServerWebSocket)
    client.socket = sock

    original_urandom = module.os.urandom
    try:
        module.os.urandom = lambda length: b"\x01\x02\x03\x04"
        client.send_pong(b"ping")
    finally:
        module.os.urandom = original_urandom

    assert sock.sent[:2] == bytes([0x8A, 0x80 | 4])
    assert sock.sent[2:6] == b"\x01\x02\x03\x04"
    assert sock.sent[6:] == bytes(
        byte ^ b"\x01\x02\x03\x04"[index % 4] for index, byte in enumerate(b"ping")
    )


def test_socket_probe_requires_websocket_upgrade(tmp_path: Path) -> None:
    module = load_hook_module()
    socket_path = tmp_path / "app-server.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_text("socket placeholder\n", encoding="utf-8")

    original_is_socket = Path.is_socket
    original_client = module.AppServerWebSocket

    def fake_is_socket(path: Path) -> bool:
        if path == socket_path:
            return True
        return original_is_socket(path)

    class FailingHandshakeClient:
        def __init__(self, path: Path, timeout: float = 15) -> None:
            assert path == socket_path
            assert timeout == 0.5
            raise module.AppServerError("app-server websocket handshake failed: HTTP/1.1 200 OK")

    try:
        Path.is_socket = fake_is_socket
        module.AppServerWebSocket = FailingHandshakeClient
        probe = module.probe_app_server_socket(socket_path)
    finally:
        Path.is_socket = original_is_socket
        module.AppServerWebSocket = original_client

    assert probe["connect_ok"] is True
    assert probe["protocol_ok"] is False
    assert probe["reason"] == "websocket-failed"


def test_bridge_failure_preserves_desktop_probe(tmp_path: Path) -> None:
    module = load_hook_module()
    state = tmp_path / "state"
    desktop_socket = tmp_path / "missing-control.sock"
    bridge_socket = tmp_path / "missing-bridge.sock"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_bridge_policy = os.environ.get("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY")
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = "bridge"
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket

        def fail_bridge():
            raise module.AppServerError("simulated bridge startup failure")

        module.ensure_bridge_app_server = fail_bridge
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp_path),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=None,
        )
    finally:
        if original_state_dir is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = original_state_dir
        if original_bridge_policy is None:
            os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
        else:
            os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = original_bridge_policy
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = original_desktop_socket
        module.bridge_socket_path = original_bridge_socket_path
        module.ensure_bridge_app_server = original_ensure_bridge

    assert "reason=app_server_fork_failed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "app-server-failed"
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge"]["reason"] == "missing"


def test_missing_desktop_control_reports_failure_not_bridge_or_continuation(tmp_path: Path) -> None:
    module = load_hook_module()
    state = tmp_path / "state"
    desktop_socket = tmp_path / "missing-control.sock"
    bridge_socket = tmp_path / "missing-bridge.sock"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_bridge_policy = os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
    original_allow_bridge = os.environ.pop("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER", None)
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = "report"
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket
        module.ensure_bridge_app_server = lambda: (_ for _ in ()).throw(
            AssertionError("bridge should not start by default")
        )
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp_path),
            prompt="fork prompt should not be used",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=None,
            fallback_failure_reason="visible fork failure",
        )
    finally:
        if original_state_dir is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = original_state_dir
        if original_bridge_policy is not None:
            os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = original_bridge_policy
        else:
            os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
        if original_allow_bridge is not None:
            os.environ["CODEX_RVF_ALLOW_BRIDGE_APP_SERVER"] = original_allow_bridge
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = original_desktop_socket
        module.bridge_socket_path = original_bridge_socket_path
        module.ensure_bridge_app_server = original_ensure_bridge

    assert "decision" not in payload
    assert payload["continue"] is True
    assert "reason=desktop_control_unavailable_continuation_disabled" in payload["systemMessage"]
    assert "$review-validate-fix" not in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "desktop-control-unavailable-report"
    assert latest["report_reason"] == "visible fork failure"
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge_policy"] == "report"


def test_missing_desktop_control_auto_uses_existing_bridge(tmp_path: Path) -> None:
    module = load_hook_module()
    desktop_socket = tmp_path / "missing-control.sock"
    bridge_socket = tmp_path / "bridge.sock"
    original_bridge_policy = os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
    original_allow_bridge = os.environ.pop("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER", None)
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_probe = module.probe_app_server_socket
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket

        def fake_probe(path: Path) -> dict[str, object]:
            if path == bridge_socket:
                return {
                    "path": str(path),
                    "exists": True,
                    "parent_exists": True,
                    "is_socket": True,
                    "connect_ok": True,
                    "reason": "connect-ok",
                }
            return {
                "path": str(path),
                "exists": False,
                "parent_exists": True,
                "is_socket": False,
                "connect_ok": False,
                "reason": "missing",
            }

        module.probe_app_server_socket = fake_probe
        module.ensure_bridge_app_server = lambda: (_ for _ in ()).throw(
            AssertionError("existing bridge should be selected without restart")
        )

        socket_path, source, selection = module.select_app_server_socket()
    finally:
        if original_bridge_policy is not None:
            os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = original_bridge_policy
        if original_allow_bridge is not None:
            os.environ["CODEX_RVF_ALLOW_BRIDGE_APP_SERVER"] = original_allow_bridge
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = original_desktop_socket
        module.bridge_socket_path = original_bridge_socket_path
        module.probe_app_server_socket = original_probe
        module.ensure_bridge_app_server = original_ensure_bridge

    assert socket_path == bridge_socket
    assert source == "bridge"
    assert selection["desktop_control"]["reason"] == "missing"
    assert selection["bridge"]["reason"] == "connect-ok"
    assert selection["bridge_policy"] == "auto"
    assert selection["bridge_decision"] == "existing-bridge-connect-ok"


def test_bridge_app_server_listener_pids_filters_rvf_socket(tmp_path: Path) -> None:
    module = load_hook_module()
    bridge_socket = tmp_path / "rvf-app-server.sock"

    class FakeCompleted:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(args: list[str], **_: object) -> FakeCompleted:
        if args == ["lsof", "-nP", "-U"]:
            return FakeCompleted(
                0,
                "\n".join(
                    [
                        f"codex-aar 111 user 20u unix 0x1 0t0 {bridge_socket}",
                        f"codex-aar 222 user 21u unix 0x2 0t0 {bridge_socket}",
                        f"codex-aar 333 user 22u unix 0x3 0t0 {tmp_path / 'other.sock'}",
                    ]
                ),
            )
        if args[:3] == ["ps", "-p", "111"]:
            return FakeCompleted(
                0,
                f"/opt/homebrew/bin/codex app-server --listen unix://{bridge_socket}\n",
            )
        if args[:3] == ["ps", "-p", "222"]:
            return FakeCompleted(0, "codex exec something-else\n")
        if args[:3] == ["ps", "-p", "333"]:
            return FakeCompleted(
                0,
                f"/opt/homebrew/bin/codex app-server --listen unix://{tmp_path / 'other.sock'}\n",
            )
        raise AssertionError(args)

    original_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        assert module.bridge_app_server_listener_pids(bridge_socket) == [111]
    finally:
        module.subprocess.run = original_run


def test_restart_bridge_stops_existing_listener_before_relaunch(tmp_path: Path) -> None:
    module = load_hook_module()
    bridge_socket = tmp_path / "rvf-app-server.sock"
    bridge_socket.parent.mkdir(parents=True, exist_ok=True)
    bridge_socket.write_text("stale", encoding="utf-8")
    started = False
    calls: list[tuple[str, Path]] = []

    class FakePopen:
        def __init__(self, args: list[str], **_: object) -> None:
            nonlocal started
            assert args[-1] == f"unix://{bridge_socket}"
            started = True
            bridge_socket.write_text("fresh", encoding="utf-8")

    original_bridge_socket_path = module.bridge_socket_path
    original_bridge_log_path = module.bridge_log_path
    original_stop = module.stop_existing_bridge_app_servers
    original_can_connect = module.can_connect_app_server_socket
    original_popen = module.subprocess.Popen
    original_codex_bin = module.codex_bin
    try:
        module.bridge_socket_path = lambda: bridge_socket
        module.bridge_log_path = lambda: tmp_path / "rvf-app-server.log"
        module.stop_existing_bridge_app_servers = lambda path: calls.append(
            ("stop", path)
        ) or {"pids": [111], "stopped": [111], "failed": [], "still_running": []}
        module.can_connect_app_server_socket = lambda path: started and path == bridge_socket
        module.subprocess.Popen = FakePopen
        module.codex_bin = lambda: "codex"

        assert module.ensure_bridge_app_server(restart_existing=True) == bridge_socket
    finally:
        module.bridge_socket_path = original_bridge_socket_path
        module.bridge_log_path = original_bridge_log_path
        module.stop_existing_bridge_app_servers = original_stop
        module.can_connect_app_server_socket = original_can_connect
        module.subprocess.Popen = original_popen
        module.codex_bin = original_codex_bin

    assert calls == [("stop", bridge_socket)]
    assert bridge_socket.read_text(encoding="utf-8") == "fresh"


def test_bridge_app_server_error_restarts_bridge_once(tmp_path: Path) -> None:
    module = load_hook_module()
    first_socket = tmp_path / "stale.sock"
    retry_socket = tmp_path / "fresh.sock"
    active_path = tmp_path / "sessions" / "fork-retry.jsonl"
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text("{}\n", encoding="utf-8")
    calls: list[str] = []

    class FakeClient:
        instances = 0

        def __init__(self, socket_path: Path) -> None:
            self.socket_path = socket_path
            self.notifications: list[dict[str, object]] = []
            FakeClient.instances += 1
            self.instance = FakeClient.instances

        def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
            if method == "initialize":
                return {}
            if self.instance == 1 and method == "thread/fork":
                raise module.AppServerError(
                    '{"code": -32600, "message": "failed to load configuration: Operation not permitted (os error 1)"}'
                )
            if method == "thread/fork":
                assert self.socket_path == retry_socket
                return {
                    "thread": {
                        "id": "fork-retry",
                        "path": str(active_path),
                        "cwd": str(tmp_path),
                    }
                }
            if method == "turn/start":
                calls.append("turn/start")
                return {"turn": {"id": "turn-retry"}}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "fork-retry",
                        "path": str(active_path),
                        "cwd": str(tmp_path),
                    }
                }
            if method == "thread/list":
                return {
                    "data": [
                        {
                            "id": "fork-retry",
                            "path": str(active_path),
                            "cwd": str(tmp_path),
                        }
                    ],
                    "nextCursor": None,
                }
            if method == "thread/loaded/list":
                return {"data": ["fork-retry"], "nextCursor": None}
            raise AssertionError(method)

        def close(self) -> None:
            pass

    original_client = module.AppServerWebSocket
    original_select = module.select_app_server_socket
    original_ensure = module.ensure_bridge_app_server
    original_probe = module.probe_app_server_socket
    original_sessions_dir = module.DEFAULT_CODEX_SESSIONS_DIR
    original_open = module.maybe_open_fork_in_codex
    original_timeout = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    original_open_gui = os.environ.get("CODEX_RVF_OPEN_GUI_FORK")
    try:
        module.AppServerWebSocket = FakeClient
        module.select_app_server_socket = lambda: (
            first_socket,
            "bridge",
            {"bridge_policy": "auto", "bridge": {"reason": "connect-ok"}},
        )
        module.ensure_bridge_app_server = lambda restart_existing=False: (
            retry_socket if restart_existing else first_socket
        )
        module.probe_app_server_socket = lambda path: {
            "path": str(path),
            "exists": True,
            "parent_exists": True,
            "is_socket": True,
            "connect_ok": True,
            "reason": "connect-ok",
        }
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp_path / "sessions"
        module.maybe_open_fork_in_codex = lambda _: True
        os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = "0"
        os.environ["CODEX_RVF_OPEN_GUI_FORK"] = "0"
        result = module.run_app_server_fork(
            parent_thread_id="parent",
            parent_thread_path=None,
            cwd=str(tmp_path),
            prompt="$review-validate-fix",
            model=None,
            reasoning_effort=None,
            log_path=tmp_path / "hook.json",
        )
    finally:
        module.AppServerWebSocket = original_client
        module.select_app_server_socket = original_select
        module.ensure_bridge_app_server = original_ensure
        module.probe_app_server_socket = original_probe
        module.DEFAULT_CODEX_SESSIONS_DIR = original_sessions_dir
        module.maybe_open_fork_in_codex = original_open
        if original_timeout is None:
            os.environ.pop("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS", None)
        else:
            os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = original_timeout
        if original_open_gui is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK"] = original_open_gui

    assert calls == ["turn/start"]
    assert result["status"] == "app-server-started"
    assert result["socket_path"] == str(retry_socket)
    assert result["socket_selection"]["bridge_decision"] == "restarted-after-app-server-error"
    assert result["bridge_retry"]["reason"] == "app-server-error"
    assert "failed to load configuration" in result["bridge_retry"]["first_error"]


def test_cline_kanban_mode_creates_and_starts_task_with_same_run(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    prep_root = tmp_path / "prep-root"
    prep_root.mkdir()
    stale_prep = prep_root / "cccccccccccccccc.json"
    stale_prep.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": "cccccccccccccccc",
                "created_at": "2026-05-07T00:00:00Z",
                "expires_at": "2026-05-07T00:00:01Z",
                "origin_session_id": "old-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123', 'workspacePath': '/tmp/task-worktree'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-123', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
            "CODEX_RVF_CLINE_KANBAN_AGENT_ID",
            "CODEX_RVF_SUPPRESS_STOP_HOOK",
            "CODEX_RVF_PREP_ROOT",
            "FAKE_CLIENT_CALLS",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        os.environ["CODEX_RVF_CLINE_KANBAN_AGENT_ID"] = "codex"
        os.environ["CODEX_RVF_SUPPRESS_STOP_HOOK"] = "1"
        os.environ["CODEX_RVF_PREP_ROOT"] = str(prep_root)
        os.environ["FAKE_CLIENT_CALLS"] = str(client_calls)
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model="gpt-test",
            reasoning_effort="high",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    assert "pause_origin_edits=true" in payload["systemMessage"]
    assert "workspace=/tmp/task-worktree" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert "请暂停在 origin worktree 继续编辑" in latest["message"]
    assert latest["rvf_backend"] == "kanban-task"
    assert latest["rvf_backend_raw"] == "cline-kanban"
    assert latest["rvf_state_phase"] == "prepare"
    assert latest["rvf_scope_contract_path"].endswith("artifacts/inputs/scope.contract.json")
    assert latest["rvf_review_packet_path"].endswith("artifacts/review-packet.md")
    assert latest["rvf_state"]["phases"] == [
        "prepare",
        "review",
        "merge",
        "validate_fix",
        "verify",
        "handoff",
        "complete",
    ]
    assert latest["cline_kanban_task_id"] == "task-123"
    assert latest["workspace_path"] == "/tmp/task-worktree"
    assert "app_server_requests_path" not in latest
    assert Path(latest["startup_prepare_metadata_path"]).exists()
    assert Path(latest["worktree_bootstrap_path"]).exists()
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["ensure", "create", "start"]
    create_argv = calls[1]["argv"]
    assert "--base-ref" in create_argv
    assert "--prompt" in create_argv
    prompt_text = create_argv[create_argv.index("--prompt") + 1]
    assert "RVF_CLINE_KANBAN_TASK" in prompt_text
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt_text
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
    assert "RVF_TARGET_REPO: ." in prompt_text
    assert f"RVF_PARENT_REPO: {repo}" in prompt_text
    assert f"RVF_PARENT_CWD: {repo}" in prompt_text
    assert f"RVF_TARGET_REPO: {repo}" not in prompt_text
    assert "RVF_ARTIFACTS_DIR: $RVF_RUN_DIR/artifacts" in prompt_text
    assert 'RVF_TASK_REPO="$(git rev-parse --show-toplevel)"' in prompt_text
    assert "export CODEX_RVF_LOG_ROOT=" in prompt_text
    assert "export CODEX_RVF_RUN_ID=" in prompt_text
    assert 'export CODEX_RVF_RUN_DIR="$RVF_RUN_DIR"' in prompt_text
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in prompt_text
    assert '. "$RVF_ARTIFACTS_DIR/review-env.sh"' in prompt_text
    assert 'export RVF_REPO="$RVF_TASK_REPO"' in prompt_text
    assert '--metadata "$RVF_WORKTREE_BOOTSTRAP" --repo "$RVF_REPO"' in prompt_text
    assert "- scope contract: `$RVF_SCOPE_CONTRACT`" in prompt_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in prompt_text
    assert "- session manifest: `$RVF_SESSION_MANIFEST`" in prompt_text
    assert "review scope 只能以 `$RVF_SCOPE_CONTRACT`" in prompt_text
    assert "和 review packet 为准" not in prompt_text
    assert "review packet 仅作为冻结 reviewer 输入" in prompt_text
    assert "session manifest 只作为 ownership evidence" in prompt_text
    assert "`$RVF_ARTIFACTS_DIR/handoff.md`" in prompt_text
    assert "rvf_handoff.py" in prompt_text
    assert 'open "$RVF_ARTIFACTS_DIR/handoff.md"' in prompt_text
    assert "不要在当前 Cline Kanban worktree 里重新运行 `prepare_review_run.py`" in prompt_text
    artifacts_dir = latest["artifacts_dir"]
    assert f"{artifacts_dir}/review-packet.md" not in prompt_text
    assert f"{artifacts_dir}/session-manifest.json" not in prompt_text
    assert f"{artifacts_dir}/worktree-bootstrap.json" not in prompt_text
    startup_scope = (Path(artifacts_dir) / "headless-startup-scope-of-work.md").read_text(encoding="utf-8")
    assert "scope 只能以本 run artifacts 中已经生成的 scope.contract.json" in startup_scope
    assert "review packet、session manifest、workspace snapshot 和 worktree bootstrap 仅作为冻结证据" in startup_scope
    assert "作为启动时 scope anchor" not in startup_scope
    task_title = create_argv[create_argv.index("--title") + 1]
    assert task_title.startswith("RVF from Codex parent-thread run ")
    assert " repo " not in task_title
    assert latest["parent_conversation_ref"] == "Codex parent-thread"
    assert latest["parent_codex_url"] == "codex://local/parent-thread"
    assert Path(latest["parent_origin_path"]).exists()
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert f"RVF_DISPATCH=token={prep_token}" in prompt_text
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt_text
    assert prep["origin_session_id"] == "parent-thread"
    assert Path(str(prep["origin_repo"])).resolve() == repo.resolve()
    assert prep["target_flow"] == "flow-2-branch"
    assert prep["target_worktree"] == "/tmp/task-worktree"
    assert prep["target_kanban_task_id"] == "task-123"
    assert latest["rvf_dispatch_target_worktree"] == "/tmp/task-worktree"
    assert latest["rvf_dispatch_target_kanban_task_id"] == "task-123"
    assert not stale_prep.exists()
    sweep_events = [
        event
        for event in latest_events(state)
        if event.get("event") == "dispatch_prep_file_sweep_completed"
    ]
    assert sweep_events
    assert sweep_events[-1]["removed_count"] == 1
    assert sweep_events[-1]["removed_paths"] == [str(stale_prep)]
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert "RVF_PARENT_CONVERSATION_REF: Codex parent-thread" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME: Codex parent-thread" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: session_ref_fallback" in prompt_text
    assert "RVF_PARENT_CODEX_URL: codex://local/parent-thread" in prompt_text
    assert "## Origin" in prompt_text
    assert "origin metadata: `$RVF_ARTIFACTS_DIR/origin.json`" in prompt_text
    assert create_argv[create_argv.index("--agent-id") + 1] == "codex"
    assert all(call["suppress"] is None for call in calls)
    suppression_path = Path(latest["cline_kanban_stop_hook_suppression_path"])
    assert suppression_path.exists()
    suppression_marker = json.loads(suppression_path.read_text(encoding="utf-8"))
    assert suppression_marker["task_id"] == "task-123"
    assert suppression_marker["suppress_stop_hook"] is True
    assert suppression_marker["run_id"] == latest["run_id"]


def test_cline_kanban_mode_requires_workspace_path(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import json, sys\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-no-workspace'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-no-workspace', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_unavailable" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert "workspace_path/workspacePath" in latest["error"]
    assert "workspace_path" not in latest
    assert latest["rvf_dispatch_target_worktree"] is None
    prep = dispatch_prep_payload(latest)
    assert prep["target_worktree"] is None


def test_auto_mode_creates_cline_kanban_task_by_default(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-auto', 'workspace_path': '/tmp/task-worktree'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-auto', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "session_id": "auto-parent",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={
                "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )[0]
    )

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert latest["backend"] == "kanban"
    assert latest["backend_selection_mode"] == "auto"
    assert latest["cline_kanban_task_id"] == "task-auto"
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["ensure", "create", "start"]


def test_auto_mode_reports_kanban_unavailable_without_default_gui_fallback(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import sys\n"
        "raise SystemExit('kanban unavailable for fallback test')\n",
        encoding="utf-8",
    )

    original_state = os.environ.get("CODEX_RVF_STATE_DIR")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    original_client = os.environ.get("CODEX_RVF_CLINE_KANBAN_CLIENT")
    original_task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD")
    original_legacy = os.environ.get("CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK")
    original_lookup = module.parent_thread_name_from_app_server
    original_gui = module.run_app_server_fork
    gui_calls: list[dict[str, object]] = []
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "auto"
        os.environ.pop("CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK", None)
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        module.run_app_server_fork = lambda **kwargs: gui_calls.append(kwargs) or {
            "status": "app-server-started",
            "fork_thread_id": "unexpected-gui-fork",
        }
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=transcript,
            launch_mode="cline-kanban",
            extra_summary={"backend_selection_mode": "auto", "backend": "kanban"},
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        module.run_app_server_fork = original_gui
        for key, value in {
            "CODEX_RVF_STATE_DIR": original_state,
            "CODEX_RVF_FORK_MODE": original_mode,
            "CODEX_RVF_CLINE_KANBAN_CLIENT": original_client,
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": original_task_cmd,
            "CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK": original_legacy,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_unavailable" in payload["systemMessage"]
    assert gui_calls == []
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert latest["mode"] == "cline-kanban"
    assert latest["backend"] == "kanban"
    assert latest["legacy_gui_fallback_enabled"] is False
    assert "legacy_gui_fallback" not in latest


def test_auto_mode_can_opt_into_legacy_gui_as_backup_of_backup(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import sys\n"
        "raise SystemExit('kanban unavailable for fallback test')\n",
        encoding="utf-8",
    )

    original_state = os.environ.get("CODEX_RVF_STATE_DIR")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    original_client = os.environ.get("CODEX_RVF_CLINE_KANBAN_CLIENT")
    original_task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD")
    original_legacy = os.environ.get("CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK")
    original_lookup = module.parent_thread_name_from_app_server
    original_gui = module.run_app_server_fork
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "auto"
        os.environ["CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK"] = "1"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        module.run_app_server_fork = lambda **_: {
            "status": "app-server-started",
            "socket_path": str(tmp_path / "legacy.sock"),
            "socket_source": "test",
            "socket_selection": {},
            "fork_thread_id": "legacy-fork",
            "turn_id": "legacy-turn",
            "gui_visibility": "legacy-fallback-test",
            "opened_gui_deeplink": False,
            "open_gui_deeplink": {"opened": False, "attempts": []},
            "notifications": [],
        }
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=transcript,
            launch_mode="cline-kanban",
            extra_summary={"backend_selection_mode": "auto", "backend": "kanban"},
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        module.run_app_server_fork = original_gui
        for key, value in {
            "CODEX_RVF_STATE_DIR": original_state,
            "CODEX_RVF_FORK_MODE": original_mode,
            "CODEX_RVF_CLINE_KANBAN_CLIENT": original_client,
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": original_task_cmd,
            "CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK": original_legacy,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=fork_started" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "app-server-started"
    assert latest["mode"] == "legacy-gui"
    assert latest["effective_backend"] == "legacy-gui"
    assert latest["legacy_gui_fallback_enabled"] is True
    assert latest["backend"] == "kanban"
    assert latest["legacy_gui_fallback"]["started"] is True
    assert latest["legacy_gui_fallback"]["primary_backend"] == "cline-kanban"
    assert latest["legacy_gui_fallback"]["fallback_backend"] == "gui"
    assert latest["legacy_gui_fallback"]["primary_failure"]["status"] == "cline-kanban-unavailable"


def test_auto_mode_reports_stale_kanban_listener_without_gui_fallback(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import sys\n"
        "print('cline-kanban error: KanbanError: Kanban CLI reached a server on "
        "127.0.0.1:3484, but no listener pane belongs to tmux session `cline-kanban` "
        "or `cline-kanban-*`. Listener(s): pid=123 cwd=/tmp/other tmux=rvf-vibe-kanban "
        "command=kanban. Stop the foreign listener or restart Kanban from a correctly "
        "named tmux session before creating RVF tasks.', file=sys.stderr)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    original_state = os.environ.get("CODEX_RVF_STATE_DIR")
    original_mode = os.environ.get("CODEX_RVF_FORK_MODE")
    original_client = os.environ.get("CODEX_RVF_CLINE_KANBAN_CLIENT")
    original_task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD")
    original_lookup = module.parent_thread_name_from_app_server
    original_gui = module.run_app_server_fork
    gui_calls: list[dict[str, object]] = []
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "auto"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        module.run_app_server_fork = lambda **kwargs: gui_calls.append(kwargs) or {
            "status": "app-server-started",
            "fork_thread_id": "unexpected-gui-fork",
        }
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=transcript,
            launch_mode="cline-kanban",
            extra_summary={"backend_selection_mode": "auto", "backend": "kanban"},
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        module.run_app_server_fork = original_gui
        for key, value in {
            "CODEX_RVF_STATE_DIR": original_state,
            "CODEX_RVF_FORK_MODE": original_mode,
            "CODEX_RVF_CLINE_KANBAN_CLIENT": original_client,
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": original_task_cmd,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_unavailable" in payload["systemMessage"]
    assert gui_calls == []
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert latest["backend"] == "kanban"
    assert "no listener pane belongs to tmux session `cline-kanban`" in str(latest["error"])
    assert "legacy_gui_fallback" not in latest


def test_cline_kanban_mode_without_transcript_fail_closes_before_task_start(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-123', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": "parent-thread",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "cline-kanban",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=cline_kanban_missing_scope_anchor" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "cline_kanban_missing_scope_anchor"
    assert latest["backend"] == "kanban"
    assert "startup_prepare_metadata_path" not in latest
    assert not client_calls.exists()


def test_cline_kanban_mode_blocks_expired_codex_login_before_task_start(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print('session expired; please login again', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(f'unexpected codex argv: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'should-not-start'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "cline-kanban",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CODEX_BIN": str(fake_codex),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=provider_health_failed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "provider_health_failed"
    assert latest["backend"] == "kanban"
    assert latest["gate_status"] == "DIRTY"
    assert "codex login" in str(latest["message"])
    assert "startup_prepare_metadata_path" not in latest
    assert not client_calls.exists()
    health = read_json_artifact(latest, "provider_health_path")
    assert isinstance(health, dict)
    results = health["results"]
    assert isinstance(results, list)
    assert results[0]["provider"] == "codex"
    assert results[0]["status"] == "failed"
    assert "session expired" in results[0]["stderr"]


def test_kanban_followup_mode_injects_current_task_message(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77', 'title': 'Fix RVF follow-up source metadata'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'msg-77', 'status': 'queued', 'checkpoint_id': 'checkpoint-1'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "kanban-followup-enqueued"
    assert latest["backend"] == "kanban-followup"
    assert latest["rvf_backend"] == "kanban-followup"
    assert latest["rvf_state_phase"] == "prepare"
    assert latest["cline_kanban_task_id"] == "task-77"
    assert latest["cline_kanban_attempt_id"] == "attempt-9"
    assert latest["cline_kanban_message_id"] == "msg-77"
    assert latest["cline_kanban_checkpoint_id"] == "checkpoint-1"
    assert latest["cline_kanban_task_title"] == "Fix RVF follow-up source metadata"
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_task_lookup"
    assert latest["parent_thread_id"] == session_id
    assert latest["parent_thread_path"] == str(transcript.resolve())
    assert latest["parent_source_kind"] == "cline-kanban-task"
    assert latest["parent_conversation_ref"] == "Fix RVF follow-up source metadata"
    assert latest["parent_conversation_name"] == latest["parent_conversation_ref"]
    assert latest["parent_conversation_name_source"] == "cline_kanban_task_lookup"
    assert latest["parent_codex_session_ref"] == "Codex 2026-05-01T11-25-17 019de191"
    assert latest["parent_codex_session_name_source"] == "session_ref_fallback"
    assert latest["parent_codex_url"] == f"codex://local/{session_id}"
    assert Path(str(latest["parent_origin_path"])).exists()
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "message"]
    message_argv = calls[1]["argv"]
    assert "--task-id" in message_argv
    assert message_argv[message_argv.index("--task-id") + 1] == "task-77"
    assert "--attempt-id" in message_argv
    assert message_argv[message_argv.index("--attempt-id") + 1] == "attempt-9"
    assert "--prompt-file" in message_argv
    prompt_path = Path(message_argv[message_argv.index("--prompt-file") + 1])
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert prompt_text.startswith("$review-validate-fix\n")
    assert "RVF_KANBAN_FOLLOWUP_TRIGGER" in prompt_text
    assert "RVF_CURRENT_TASK_ID: task-77" in prompt_text
    assert "RVF_CURRENT_ATTEMPT_ID: attempt-9" in prompt_text
    assert "RVF_PARENT_CONVERSATION_REF: Fix RVF follow-up source metadata" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME: Fix RVF follow-up source metadata" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: cline_kanban_task_lookup" in prompt_text
    assert "RVF_PARENT_SOURCE_KIND: cline-kanban-task" in prompt_text
    assert "RVF_PARENT_KANBAN_TASK_ID: task-77" in prompt_text
    assert "RVF_PARENT_KANBAN_ATTEMPT_ID: attempt-9" in prompt_text
    assert "RVF_PARENT_KANBAN_TASK_TITLE: Fix RVF follow-up source metadata" in prompt_text
    assert "`source Kanban task id`" in prompt_text
    assert "`source Kanban attempt id`" in prompt_text
    assert "`source Kanban task title at trigger`" in prompt_text
    assert "RVF_PARENT_CODEX_SESSION_REF: Codex 2026-05-01T11-25-17 019de191" in prompt_text
    assert f"RVF_PARENT_CODEX_URL: codex://local/{session_id}" in prompt_text
    assert f"RVF_PARENT_TRANSCRIPT_PATH: {transcript.resolve()}" in prompt_text
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert f"RVF_DISPATCH=token={prep_token}" in prompt_text
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt_text
    assert prep["origin_session_id"] == session_id
    assert Path(str(prep["origin_repo"])).resolve() == repo.resolve()
    assert prep["target_flow"] == "flow-1-self-rising"
    assert prep["target_kanban_task_id"] == "task-77"
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert "如果当前会话位于 Cline Kanban task 内，它们应优先使用 Kanban task" in prompt_text
    assert "RVF_CLINE_KANBAN_TASK" not in prompt_text
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
    assert calls[0]["suppress"] is None
    assert calls[1]["suppress"] is None


def test_kanban_followup_title_falls_back_to_local_board_state(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191.jsonl"
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    write_apply_patch_transcript(transcript, repo, session_id=session_id)
    kanban_state = tmp_path / "kanban"
    workspace = kanban_state / "workspaces" / "repo"
    workspace.mkdir(parents=True)
    (workspace / "board.json").write_text(
        json.dumps(
            {
                "columns": [
                    {
                        "id": "in_progress",
                        "cards": [
                            {
                                "id": "task-77",
                                "title": 'The kanban "follow up" rvf handoff source chat title',
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_ATTEMPT_ID": "attempt-9",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    expected_title = 'The kanban "follow up" rvf handoff source chat title'
    assert latest["cline_kanban_task_title"] == expected_title
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_board_lookup"
    assert latest["parent_conversation_ref"] == expected_title
    assert latest["parent_conversation_name_source"] == "cline_kanban_board_lookup"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_board_lookup"
    assert task_lookup["task_list_lookup"]["source"] == "cline_kanban_task_lookup_missing_title"
    assert Path(task_lookup["artifact"]).exists()

    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "message"]
    prompt_path = Path(calls[1]["argv"][calls[1]["argv"].index("--prompt-file") + 1])
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert f"RVF_PARENT_CONVERSATION_REF: {expected_title}" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: cline_kanban_board_lookup" in prompt_text
    assert f"RVF_PARENT_KANBAN_TASK_TITLE: {expected_title}" in prompt_text
    assert "`source Kanban task id`" in prompt_text


def test_kanban_followup_title_ignores_unrelated_board_with_same_task_id(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    kanban_state = tmp_path / "kanban"
    stale_workspace = kanban_state / "workspaces" / "stale-project"
    stale_workspace.mkdir(parents=True)
    (stale_workspace / "board.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-77",
                        "title": "Wrong stale workspace title",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cline_kanban_task_title"] is None
    assert latest["cline_kanban_task_title_source"] is None
    assert latest["parent_conversation_ref"] == "Cline Kanban task task-77"
    assert latest["parent_conversation_name_source"] == "cline_kanban_task_id_fallback"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_task_lookup_missing_title"


def test_kanban_followup_title_uses_session_matched_board_state(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    kanban_state = tmp_path / "kanban"
    matched_workspace = kanban_state / "workspaces" / "task-workspace"
    matched_workspace.mkdir(parents=True)
    (matched_workspace / "sessions.json").write_text(
        json.dumps(
            {
                "session-1": {
                    "taskId": "task-77",
                    "workspacePath": str(repo),
                }
            }
        ),
        encoding="utf-8",
    )
    (matched_workspace / "board.json").write_text(
        json.dumps(
            {
                "columns": [
                    {
                        "cards": [
                            {
                                "id": "task-77",
                                "title": "Session matched workspace title",
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stale_workspace = kanban_state / "workspaces" / "aaa-stale-project"
    stale_workspace.mkdir(parents=True)
    (stale_workspace / "board.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-77",
                        "title": "Wrong stale workspace title",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(kanban_state),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cline_kanban_task_title"] == "Session matched workspace title"
    assert latest["cline_kanban_task_title_source"] == "cline_kanban_board_lookup"
    assert latest["parent_conversation_ref"] == "Session matched workspace title"
    task_lookup = latest["cline_kanban_task_lookup"]
    assert task_lookup["source"] == "cline_kanban_board_lookup"
    board_lookup = json.loads(Path(task_lookup["artifact"]).read_text(encoding="utf-8"))
    assert board_lookup["matched_board"] == str(matched_workspace / "board.json")
    assert str(stale_workspace / "board.json") not in board_lookup["checked"]


def test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    subdir = repo / "nested"
    subdir.mkdir()
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': [{'id': 'task-77', 'title': 'Subdir follow-up'}]}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'message_id': 'msg-77', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.pop(key, None)
        for key in ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH")
    }
    try:
        stdout, _ = invoke(
            {
                "cwd": str(subdir),
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_FORK_MODE": "kanban-followup",
                "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "KANBAN_TASK_ID": "task-77",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )
    finally:
        for key, value in original_env.items():
            if value is not None:
                os.environ[key] = value

    payload = parse_json(stdout)
    assert "reason=kanban_followup_enqueued" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["cwd"] == str(subdir.resolve())
    assert latest["cline_kanban_project_path"] == str(repo.resolve())
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["list", "message"]
    list_argv = calls[0]["argv"]
    assert list_argv[list_argv.index("--repo") + 1] == str(repo.resolve())
    message_argv = calls[1]["argv"]
    assert message_argv[message_argv.index("--repo") + 1] == str(repo.resolve())


def test_kanban_followup_blocks_expired_codex_login_before_message(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print('session expired; please login again', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(f'unexpected codex argv: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'task-77', 'message_id': 'should-not-enqueue'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
            "CODEX_RVF_CODEX_BIN": str(fake_codex),
            "KANBAN_TASK_ID": "task-77",
            "KANBAN_PROJECT_PATH": str(repo),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=provider_health_failed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "provider_health_failed"
    assert latest["backend"] == "kanban-followup"
    assert "codex login" in str(latest["message"])
    assert not client_calls.exists()
    health = read_json_artifact(latest, "provider_health_path")
    results = health["results"]
    assert results[0]["provider"] == "codex"
    assert results[0]["status"] == "failed"
    assert "session expired" in results[0]["stderr"]


def test_kanban_followup_mode_without_task_id_reports_without_fallback(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "kanban-followup",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_missing_task_id" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_missing_task_id"
    assert latest["backend"] == "kanban-followup"
    assert not client_calls.exists()


def test_kanban_followup_trigger_marker_skips_one_turn(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "last_user_message": "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER",
        },
        extra_env={"CODEX_RVF_FORK_MODE": "kanban-followup"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=kanban_followup_trigger_turn" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "kanban_followup_trigger_turn"


def test_rvf_analyze_followup_trigger_marker_skips_one_turn(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "last_user_message": "$rvf-analyze /tmp/rvf-run\n\nRVF_KANBAN_ANALYZE_TRIGGER",
        },
        extra_env={"CODEX_RVF_FORK_MODE": "kanban-followup"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=rvf_analyze_followup_trigger_turn" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "rvf_analyze_followup_trigger_turn"


def test_cline_kanban_mode_marks_unavailable_when_task_start_fails(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'ensure':\n"
        "    print(json.dumps({'ok': True}))\n"
        "elif sys.argv[1] == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123'}))\n"
        "else:\n"
        "    print('start boom', file=sys.stderr)\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in ("CODEX_RVF_STATE_DIR", "CODEX_RVF_FORK_MODE", "CODEX_RVF_CLINE_KANBAN_CLIENT")
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=write_apply_patch_transcript(tmp_path / "session.jsonl", repo),
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert "start boom" in str(latest["message"])


def test_fork_experiment_missing_desktop_control_prepares_manual_not_continuation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_"):
            env.pop(name, None)
    env["HOME"] = str(home)
    env["CODEX_RVF_STATE_DIR"] = str(state)
    env["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = "report"
    completed = subprocess.run(
        [sys.executable, str(DIAGNOSTIC_SCRIPT)],
        input=json.dumps({"session_id": "parent-thread", "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    payload = parse_json(completed.stdout)

    assert "decision" not in payload
    assert payload["continue"] is True
    assert "reason=manual_prepared" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "manual-prepared"
    assert latest["desktop_control_unavailable_fallback"] == "manual"
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge_policy"] == "report"
    assert latest["marker"] == "RVF_FORK_EXPERIMENT"


def test_missing_desktop_control_fail_policy_reports(tmp_path: Path) -> None:
    module = load_hook_module()
    state = tmp_path / "state"
    desktop_socket = tmp_path / "missing-control.sock"
    bridge_socket = tmp_path / "missing-bridge.sock"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_bridge_policy = os.environ.get("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY")
    original_allow_bridge = os.environ.pop("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER", None)
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = "fail"
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket
        module.ensure_bridge_app_server = lambda: (_ for _ in ()).throw(
            AssertionError("bridge should not start when policy=fail")
        )
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp_path),
            prompt="fork prompt should not be used",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=None,
            fallback_failure_reason="visible fork failure",
        )
    finally:
        if original_state_dir is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = original_state_dir
        if original_bridge_policy is None:
            os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
        else:
            os.environ["CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY"] = original_bridge_policy
        if original_allow_bridge is not None:
            os.environ["CODEX_RVF_ALLOW_BRIDGE_APP_SERVER"] = original_allow_bridge
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = original_desktop_socket
        module.bridge_socket_path = original_bridge_socket_path
        module.ensure_bridge_app_server = original_ensure_bridge

    assert "decision" not in payload
    assert payload["continue"] is True
    assert "reason=desktop_control_unavailable_fail_policy" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "desktop-control-unavailable-fail"
    assert "CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY=fail" in latest["report_reason"]
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge_policy"] == "fail"


def test_fork_session_visibility_waits_only_for_active_session(tmp_path: Path) -> None:
    module = load_hook_module()
    original_sessions_dir = module.DEFAULT_CODEX_SESSIONS_DIR
    try:
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp_path / "sessions"
        active_path = (
            module.DEFAULT_CODEX_SESSIONS_DIR
            / "2026"
            / "04"
            / "26"
            / "rollout-2026-04-26T21-28-28-fork-visible.jsonl"
        )
        active_path.parent.mkdir(parents=True, exist_ok=True)
        active_path.write_text("{}\n", encoding="utf-8")

        active = module.fork_session_visibility("fork-visible", str(active_path))
        assert active["location"] == "active"
        assert active["hinted_exists"] is True
        assert str(active_path) in active["active_paths"]

        active_path.unlink()
        missing = module.wait_for_fork_session_visibility(
            "fork-visible",
            str(active_path),
            timeout_seconds=0,
        )
        assert missing["location"] == "missing"
        assert missing["active_paths"] == []
    finally:
        module.DEFAULT_CODEX_SESSIONS_DIR = original_sessions_dir


def test_app_server_fork_waits_for_session_file_before_deeplink(tmp_path: Path) -> None:
    module = load_hook_module()
    socket_path = tmp_path / "app-server.sock"
    active_path = tmp_path / "sessions" / "rollout-fork-wait.jsonl"
    calls: list[str] = []

    class FakeClient:
        def __init__(self, socket: Path) -> None:
            assert socket == socket_path
            self.notifications: list[dict[str, object]] = []

        def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
            if method == "initialize":
                return {}
            if method == "thread/fork":
                return {"thread": {"id": "fork-wait", "path": str(active_path)}}
            if method == "turn/start":
                calls.append("turn/start")
                active_path.parent.mkdir(parents=True, exist_ok=True)
                active_path.write_text("{}\n", encoding="utf-8")
                return {"turn": {"id": "turn-wait"}}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "fork-wait",
                        "path": str(active_path),
                        "cwd": str(tmp_path),
                        "source": "vscode",
                    }
                }
            if method == "thread/list":
                assert params is not None
                assert params["sortKey"] == "updated_at"
                assert params["useStateDbOnly"] is False
                assert params["cwd"] == str(tmp_path)
                return {
                    "data": [
                        {
                            "id": "fork-wait",
                            "path": str(active_path),
                            "cwd": str(tmp_path),
                            "source": "vscode",
                        }
                    ],
                    "nextCursor": None,
                }
            if method == "thread/loaded/list":
                return {"data": ["fork-wait"], "nextCursor": None}
            raise AssertionError(method)

        def close(self) -> None:
            pass

    original_client = module.AppServerWebSocket
    original_select = module.select_app_server_socket
    original_open = module.maybe_open_fork_in_codex
    original_sessions_dir = module.DEFAULT_CODEX_SESSIONS_DIR
    original_platform = module.sys.platform
    original_timeout = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    original_open_attempts = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    original_open_delay = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    try:
        module.AppServerWebSocket = FakeClient
        module.select_app_server_socket = lambda: (socket_path, "bridge", {})
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp_path / "sessions"
        module.sys.platform = "darwin"

        def fake_open(thread_id: str) -> bool:
            calls.append("open")
            assert thread_id == "fork-wait"
            assert active_path.exists()
            return True

        module.maybe_open_fork_in_codex = fake_open
        os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = "1"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = "2"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = "0"
        result = module.run_app_server_fork(
            parent_thread_id="parent",
            parent_thread_path=None,
            cwd=str(tmp_path),
            prompt="$review-validate-fix",
            model=None,
            reasoning_effort=None,
            log_path=tmp_path / "hook.json",
        )
    finally:
        module.AppServerWebSocket = original_client
        module.select_app_server_socket = original_select
        module.maybe_open_fork_in_codex = original_open
        module.DEFAULT_CODEX_SESSIONS_DIR = original_sessions_dir
        module.sys.platform = original_platform
        if original_timeout is None:
            os.environ.pop("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS", None)
        else:
            os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = original_timeout
        if original_open_attempts is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = original_open_attempts
        if original_open_delay is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = original_open_delay

    assert calls == ["turn/start", "open"]
    assert result["fork_thread_id"] == "fork-wait"
    assert result["turn_id"] == "turn-wait"
    assert result["session_visibility"]["location"] == "active"
    assert result["app_server_visibility"]["thread_read"]["contains_thread"] is True
    assert result["app_server_visibility"]["thread_list"]["contains_thread"] is True
    assert result["app_server_visibility"]["thread_loaded_list"]["contains_thread"] is True
    assert result["gui_visibility"] == "unverified-bridge-only"
    assert result["opened_gui_deeplink"] is True
    assert len(result["open_gui_deeplink"]["attempts"]) == 1


def test_desktop_control_fork_requires_active_session_for_verified_gui(
    tmp_path: Path,
) -> None:
    module = load_hook_module()
    socket_path = tmp_path / "app-server.sock"
    missing_path = tmp_path / "sessions" / "rollout-fork-missing.jsonl"
    calls: list[str] = []

    class FakeClient:
        def __init__(self, socket: Path) -> None:
            assert socket == socket_path
            self.notifications: list[dict[str, object]] = []

        def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
            if method == "initialize":
                return {}
            if method == "thread/fork":
                return {"thread": {"id": "fork-missing", "path": str(missing_path)}}
            if method == "turn/start":
                calls.append("turn/start")
                return {"turn": {"id": "turn-missing"}}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "fork-missing",
                        "path": str(missing_path),
                        "cwd": str(tmp_path),
                        "source": "vscode",
                    }
                }
            if method == "thread/list":
                return {
                    "data": [
                        {
                            "id": "fork-missing",
                            "path": str(missing_path),
                            "cwd": str(tmp_path),
                            "source": "vscode",
                        }
                    ],
                    "nextCursor": None,
                }
            if method == "thread/loaded/list":
                return {"data": ["fork-missing"], "nextCursor": None}
            raise AssertionError(method)

        def close(self) -> None:
            pass

    original_client = module.AppServerWebSocket
    original_select = module.select_app_server_socket
    original_open = module.maybe_open_fork_in_codex
    original_sessions_dir = module.DEFAULT_CODEX_SESSIONS_DIR
    original_platform = module.sys.platform
    original_timeout = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    original_open_attempts = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    original_open_delay = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    try:
        module.AppServerWebSocket = FakeClient
        module.select_app_server_socket = lambda: (socket_path, "desktop-control", {})
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp_path / "sessions"
        module.sys.platform = "darwin"

        def fake_open(thread_id: str) -> bool:
            calls.append("open")
            assert thread_id == "fork-missing"
            assert not missing_path.exists()
            return True

        module.maybe_open_fork_in_codex = fake_open
        os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = "0"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = "1"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = "0"
        result = module.run_app_server_fork(
            parent_thread_id="parent",
            parent_thread_path=None,
            cwd=str(tmp_path),
            prompt="$review-validate-fix",
            model=None,
            reasoning_effort=None,
            log_path=tmp_path / "hook.json",
        )
    finally:
        module.AppServerWebSocket = original_client
        module.select_app_server_socket = original_select
        module.maybe_open_fork_in_codex = original_open
        module.DEFAULT_CODEX_SESSIONS_DIR = original_sessions_dir
        module.sys.platform = original_platform
        if original_timeout is None:
            os.environ.pop("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS", None)
        else:
            os.environ["CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS"] = original_timeout
        if original_open_attempts is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = original_open_attempts
        if original_open_delay is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = original_open_delay

    assert calls == ["turn/start", "open"]
    assert result["status"] == "app-server-started"
    assert result["session_visibility"]["location"] == "missing"
    assert result["gui_visibility"] == "unverified-session-missing"


def test_bridge_fork_message_marks_gui_visibility_unverified(tmp_path: Path) -> None:
    module = load_hook_module()
    state = tmp_path / "state"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_run = module.run_app_server_fork
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)

        def fake_run_app_server_fork(**_: object) -> dict[str, object]:
            return {
                "status": "app-server-started",
                "socket_source": "bridge",
                "socket_selection": {
                    "desktop_control": {"reason": "missing"},
                    "bridge": {"reason": "connect-ok"},
                },
                "fork_thread_id": "fork-message",
                "session_visibility": {"location": "active"},
                "gui_visibility": "unverified-bridge-only",
            }

        module.run_app_server_fork = fake_run_app_server_fork
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp_path),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=None,
        )
    finally:
        module.run_app_server_fork = original_run
        if original_state_dir is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = original_state_dir

    assert "review-validate-fix: app-server-started; reason=fork_started;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["socket_source"] == "bridge"
    assert latest["gui_visibility"] == "unverified-bridge-only"


def test_open_gui_fork_disabled_skips_retry_sleep(tmp_path: Path) -> None:
    module = load_hook_module()
    calls: list[str] = []
    original_sleep = module.time.sleep
    original_open_gui = os.environ.get("CODEX_RVF_OPEN_GUI_FORK")
    original_open_attempts = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    original_open_delay = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    try:
        module.time.sleep = lambda delay: calls.append(f"sleep:{delay}")
        os.environ["CODEX_RVF_OPEN_GUI_FORK"] = "0"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = "3"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = "0.75"

        result = module.open_fork_in_codex_with_retries("fork-disabled")
    finally:
        module.time.sleep = original_sleep
        if original_open_gui is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK"] = original_open_gui
        if original_open_attempts is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = original_open_attempts
        if original_open_delay is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = original_open_delay

    assert result["opened"] is False
    assert result["skipped_retries_reason"] == "disabled"
    assert len(result["attempts"]) == 1
    assert calls == []


def test_open_gui_fork_success_stops_retries(tmp_path: Path) -> None:
    module = load_hook_module()
    calls: list[str] = []
    original_sleep = module.time.sleep
    original_open = module.maybe_open_fork_in_codex
    original_platform = module.sys.platform
    original_open_gui = os.environ.get("CODEX_RVF_OPEN_GUI_FORK")
    original_open_attempts = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    original_open_delay = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    try:
        module.time.sleep = lambda delay: calls.append(f"sleep:{delay}")
        module.sys.platform = "darwin"
        module.maybe_open_fork_in_codex = lambda thread_id: calls.append(thread_id) or True
        os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = "3"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = "0.75"

        result = module.open_fork_in_codex_with_retries("fork-success")
    finally:
        module.time.sleep = original_sleep
        module.maybe_open_fork_in_codex = original_open
        module.sys.platform = original_platform
        if original_open_gui is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK"] = original_open_gui
        if original_open_attempts is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = original_open_attempts
        if original_open_delay is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = original_open_delay

    assert result["opened"] is True
    assert len(result["attempts"]) == 1
    assert calls == ["fork-success"]


def test_open_gui_fork_unsupported_platform_skips_retry_sleep(tmp_path: Path) -> None:
    module = load_hook_module()
    calls: list[str] = []
    original_sleep = module.time.sleep
    original_platform = module.sys.platform
    original_open_gui = os.environ.get("CODEX_RVF_OPEN_GUI_FORK")
    original_open_attempts = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    original_open_delay = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    try:
        module.time.sleep = lambda delay: calls.append(f"sleep:{delay}")
        module.sys.platform = "linux"
        os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = "3"
        os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = "0.75"

        result = module.open_fork_in_codex_with_retries("fork-unsupported")
    finally:
        module.time.sleep = original_sleep
        module.sys.platform = original_platform
        if original_open_gui is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK"] = original_open_gui
        if original_open_attempts is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS"] = original_open_attempts
        if original_open_delay is None:
            os.environ.pop("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS", None)
        else:
            os.environ["CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS"] = original_open_delay

    assert result["opened"] is False
    assert result["skipped_retries_reason"] == "unsupported-platform"
    assert len(result["attempts"]) == 1
    assert calls == []


def test_session_hook_control_disables_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-disabled",
        "先不要自动 review。\nRVF_STOP_HOOK: off",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_disabled"
    assert summary["control_action"] == "off"
    assert summary["session_hook_gate_state"] == "disabled"
    assert "disabled" in str(summary["message"])
    assert "不是关闭全局 Stop hook" in str(summary["message"])
    assert "dispatcher 仍会运行" in str(summary["message"])
    assert (state / "session-hook" / "session-disabled.json").exists()
    assert latest_pointer(state)["status"] == "session-hook-control"

    write_user_session(
        transcript,
        "session-disabled",
        "这次普通停止也不应触发 hook。",
    )
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )
    assert_skip_reason(stdout, "已禁用")
    assert latest_pointer(state)["status"] == "skipped"


def test_session_hook_control_status_reports_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-status",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )

    write_user_session(
        transcript,
        "session-status",
        "RVF_STOP_HOOK: status",
    )
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            state_dir=state,
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_status"
    assert summary["control_action"] == "status"
    assert summary["session_hook_gate_state"] == "disabled"
    assert "disabled" in str(summary["message"])
    assert "不表示全局 Stop hook 是否安装或运行" in str(summary["message"])
    assert "session-status" in str(summary["message"])


def test_session_hook_control_status_works_when_env_suppressed(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-status-suppressed",
        "RVF_STOP_HOOK: status",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={"CODEX_RVF_SUPPRESS_STOP_HOOK": "1"},
            state_dir=state,
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_status"
    assert summary["session_hook_gate_state"] == "enabled"
    assert "enabled" in str(summary["message"])
    assert "session-status-suppressed" in str(summary["message"])


def test_session_hook_control_reenables_current_session(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-reenabled",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    assert (state / "session-hook" / "session-reenabled.json").exists()

    write_apply_patch_transcript(transcript, dirty, session_id="session-reenabled")
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_user_message": "RVF_STOP_HOOK: on",
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "dry_run"
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    assert not (state / "session-hook" / "session-reenabled.json").exists()
    assert latest_pointer(state)["status"] == "dry-run"
    events = latest_events(state)
    assert any(
        event["event"] == "session_hook_control_continue"
        and event["reason_code"] == "session_hook_gate_enabled"
        for event in events
    )


def test_session_hook_control_reenable_starts_cline_kanban_task(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-kanban-reenabled",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )
    assert (state / "session-hook" / "session-kanban-reenabled.json").exists()

    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-reenabled', 'workspace_path': '/tmp/task-worktree'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-reenabled', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    write_apply_patch_transcript(transcript, repo, session_id="session-kanban-reenabled")

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_user_message": "RVF_STOP_HOOK: on",
            },
            extra_env={
                "CODEX_RVF_FORK_MODE": "cline-kanban",
                "CODEX_RVF_PROVIDER_HEALTH_CHECK": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
            state_dir=state,
        )[0]
    )

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    assert not (state / "session-hook" / "session-kanban-reenabled.json").exists()
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert latest["cline_kanban_task_id"] == "task-reenabled"
    prep = dispatch_prep_payload(latest)
    prep_tracker_scope = prep["rvf_run"]["tracker_scope_path"]
    assert isinstance(prep_tracker_scope, str)
    assert prep_tracker_scope.endswith("artifacts/tracker-scope.json")
    startup_command = json.loads(
        (Path(latest["artifacts_dir"]) / "cline-kanban-startup-prepare-command.json").read_text(
            encoding="utf-8"
        )
    )
    command = startup_command["command"]
    assert command[command.index("--tracker-scope") + 1] == prep_tracker_scope
    startup_metadata = read_json_artifact(latest, "startup_prepare_metadata_path")
    assert isinstance(startup_metadata, dict)
    assert startup_metadata["input_tracker_scope_file"].endswith("artifacts/inputs/tracker-scope.json")
    assert startup_metadata["tracker_scope_file"].endswith("artifacts/tracker-scope.json")
    scope_contract = json.loads(Path(startup_metadata["scope_contract"]).read_text(encoding="utf-8"))
    assert scope_contract["primary_files"] == ["changed.txt"]
    assert scope_contract["fix_allowlist"] == ["changed.txt"]
    assert scope_contract["primary_units"]
    assert scope_contract["tracker_lease_id"]
    assert scope_contract["tracker_scope_hash"]
    assert startup_metadata["worktree_bootstrap_metadata"]["owned_dirty_paths"] == ["changed.txt"]
    packet_metadata = json.loads(
        Path(startup_metadata["review_packet_metadata"]).read_text(encoding="utf-8")
    )
    assert packet_metadata["tracker_scope_present"] is True
    assert packet_metadata["tracker_scope_unit_count"] == len(scope_contract["primary_units"])
    events = latest_events(state)
    assert any(
        event["event"] == "session_hook_control_continue"
        and event["reason_code"] == "session_hook_gate_enabled"
        for event in events
    )
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["ensure", "create", "start"]


def test_disabled_session_skips_fork_experiment_marker(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "session-disabled-experiment",
        "RVF_STOP_HOOK: off",
    )
    invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        state_dir=state,
    )

    write_user_session(
        transcript,
        "session-disabled-experiment",
        "RVF_FORK_EXPERIMENT: should not fork while disabled",
    )
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={"CODEX_RVF_FORK_EXPERIMENT_MODE": "dry-run"},
        state_dir=state,
    )
    assert_skip_reason(stdout, "已禁用")
    assert latest_pointer(state)["status"] == "skipped"


def test_subagent_source_ignores_session_hook_control(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "subagent-control",
            "stop_hook_active": False,
            "last_user_message": "RVF_STOP_HOOK: off",
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "subagent")
    assert not (state / "session-hook" / "subagent-control.json").exists()


def test_subagent_source_skips(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
        }
    )
    assert_skip_reason(stdout, "subagent")


def test_subagent_session_meta_skips(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    transcript = tmp_path / "subagent.jsonl"
    write_subagent_session(transcript)
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        }
    )
    assert_skip_reason(stdout, "subagent")


def test_clean_repo_skips(tmp_path: Path) -> None:
    clean = init_repo(tmp_path / "clean", dirty=False)
    stdout, _ = invoke({"cwd": str(clean), "stop_hook_active": False})
    assert_skip_reason(stdout, "clean")


def test_plan_document_only_routes_out_of_full_rvf(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "doc-plan", dirty=False)
    state = tmp_path / "state"
    plan = repo / "docs" / "codebase-slimdown-plan.md"
    plan.parent.mkdir()
    plan.write_text("# Plan\n\nDoc-only planning change.\n", encoding="utf-8")

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "session_id": "plan-doc-session",
            "stop_hook_active": False,
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "decision" not in payload
    assert "reason=plan_document_only" in payload["systemMessage"], payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert "Plan/Doc Maintainer Review" in summary["message"]
    assert summary["reason_code"] == "plan_document_only"
    assert summary["route"] == "plan-doc-maintainer-review"
    assert summary["changed_paths"] == ["docs/codebase-slimdown-plan.md"]
    assert summary["rvf_backend_raw"] == "plan-doc-review"
    events = latest_events(state)
    assert any(event.get("event") == "plan_doc_review_routed" for event in events)


def test_read_only_session_with_background_plan_docs_does_not_plan_route(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "doc-plan-background", dirty=False)
    state = tmp_path / "state"
    plan = repo / "docs" / "codebase-slimdown-plan.md"
    plan.parent.mkdir()
    plan.write_text("# Plan\n\nBackground planning change.\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    write_user_session(
        transcript,
        "read-only-session-with-background-plan-doc",
        "只是问答和代码库探索，没有修改文件。",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "session_id": "read-only-session-with-background-plan-doc",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=plan_document_only" not in payload["systemMessage"], payload["systemMessage"]
    assert "reason=no_unassigned_review_scope" in payload["systemMessage"], payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["reason_code"] == "no_unassigned_review_scope"
    assert summary["session_change_type"] == "no_codebase_changes"
    assert summary["session_owned_dirty_paths"] == []
    events = latest_events(state)
    assert not any(event.get("event") == "plan_doc_review_routed" for event in events)


def test_plan_document_route_does_not_hide_source_rename(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "rename-to-plan")
    state = tmp_path / "state"
    (repo / "docs").mkdir()
    run(["git", "mv", "changed.txt", "docs/codebase-slimdown-plan.md"], repo)

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "session_id": "plan-doc-rename-session",
            "stop_hook_active": False,
        },
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=plan_document_only" not in payload["systemMessage"], payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "dry-run"


def test_dirty_repo_dry_run_prepares_legacy_gui_requests(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000002",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "dry-run"
    assert latest["mode"] == "dry-run"
    requests = app_server_requests(latest)
    prompt = prompt_text(latest)
    assert requests[0]["method"] == "thread/fork"
    assert requests[1]["method"] == "turn/start"
    assert "$review-validate-fix" in prompt
    assert str(dirty) in prompt


def test_dirty_repo_manual_mode_only_prepares_prompt(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000022",
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "manual",
            },
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: manual-prepared; reason=manual_prepared;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "manual-prepared"
    assert latest["rvf_backend"] == "manual"
    assert latest["rvf_state_phase"] == "prepare"
    assert Path(latest["prompt_path"]).exists()


def test_dirty_repo_fork_dry_run(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000003",
                "model": "gpt-test",
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
                "CODEX_RVF_FORK_REASONING_EFFORT": "high",
            },
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    prompt = prompt_text(latest)
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert "$review-validate-fix" in prompt
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt
    assert f"RVF_DISPATCH=token={prep_token}" in prompt
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt
    assert prep["origin_session_id"] == "00000000-0000-0000-0000-000000000003"
    assert prep["origin_repo"] == str(dirty.resolve())
    assert prep["target_flow"] == "flow-3-inplace"
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert str(dirty) in prompt
    assert "RVF_STOP_HOOK: off" in prompt
    assert "会话控制元数据" in prompt
    assert "不要把它们当成用户分配的代码任务" in prompt
    assert latest["suppress_child_stop_hook"] is False
    assert latest["model"] == "gpt-test"
    assert latest["reasoning_effort"] == "high"
    requests = app_server_requests(latest)
    assert requests[0]["method"] == "thread/fork"
    assert requests[0]["params"]["model"] == "gpt-test"
    assert requests[1]["method"] == "turn/start"
    assert requests[1]["params"]["model"] == "gpt-test"
    assert requests[1]["params"]["effort"] == "high"


def test_dirty_repo_fork_inherits_parent_cwd_inside_worktree(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    subdir = dirty / "nested"
    subdir.mkdir()
    state = tmp_path / "state"

    payload = parse_json(
        invoke(
            {
                "cwd": str(subdir),
                "session_id": "00000000-0000-0000-0000-000000000103",
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
            },
            state_dir=state,
        )[0]
    )

    assert "decision" not in payload
    latest = latest_summary(state)
    requests = app_server_requests(latest)
    prompt = prompt_text(latest)
    assert latest["cwd"] == str(subdir.resolve())
    assert requests[0]["params"]["cwd"] == str(subdir.resolve())
    assert requests[1]["params"]["cwd"] == str(subdir.resolve())
    assert f"RVF_PARENT_CWD: {subdir.resolve()}" in prompt
    assert f"RVF_TARGET_REPO: {dirty.resolve()}" in prompt


def test_no_git_cwd_skips_even_with_dirty_trusted_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir(parents=True)
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    config = tmp_path / "config.toml"
    state = tmp_path / "state"
    write_config(config, [dirty])

    payload = parse_json(
        invoke(
            {
                "cwd": str(plain),
                "session_id": "00000000-0000-0000-0000-000000000104",
                "stop_hook_active": False,
            },
            config=config,
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
            },
            state_dir=state,
        )[0]
    )

    assert "decision" not in payload
    assert payload["continue"] is True
    summary = summary_from_payload(payload)
    assert "当前 cwd 不在 git repo/worktree 内" in str(summary["message"])
    assert "提供要运行 review-validate-fix 的目标 repo 路径" in str(summary["message"])
    assert latest_pointer(state)["status"] == "skipped"


def test_stop_event_transcript_path_overrides_bad_env_thread_id(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "state"
    write_apply_patch_transcript(
        transcript,
        dirty,
        session_id="00000000-0000-0000-0000-000000000099",
    )
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "bad-event-thread-id",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_THREAD_ID": "bad-env-thread-id",
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
            },
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    latest = latest_summary(state)
    assert latest["parent_thread_id"] == "00000000-0000-0000-0000-000000000099"
    assert latest["parent_thread_path"] == str(transcript.resolve())
    fork_params = app_server_requests(latest)[0]["params"]
    assert fork_params["threadId"] == "00000000-0000-0000-0000-000000000099"
    assert fork_params["path"] == str(transcript.resolve())


def test_stop_event_log_path_is_not_used_as_fork_rollout_path(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    log_path = tmp_path / "hook.log"
    log_path.write_text("not a rollout jsonl\n", encoding="utf-8")
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000100",
                "log_path": str(log_path),
                "stop_hook_active": False,
            },
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
            },
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    latest = latest_summary(state)
    assert latest["parent_thread_id"] == "00000000-0000-0000-0000-000000000100"
    assert latest["parent_thread_path"] is None
    assert "path" not in app_server_requests(latest)[0]["params"]


def test_dirty_repo_continuation_mode_reports_removed_fallback(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000004",
                "stop_hook_active": False,
            },
            extra_env={"CODEX_RVF_MODE": "continuation"},
        )[0]
    )
    assert "decision" not in payload
    assert payload["continue"] is True
    assert "reason=continuation_disabled" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert "$review-validate-fix" in str(summary["message"])
    assert str(dirty) in str(summary["message"])
    assert "Stop continuation prompt 已禁用" in str(summary["message"])


def test_forked_rvf_session_gets_programmatic_handoff_advisory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    handoff = tmp_path / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    event = {
        "cwd": str(tmp_path),
        "session_id": "child-session",
        "stop_hook_active": False,
        "last_user_message": fork_prompt,
        "last_assistant_message": f"完成。\nRVF_HANDOFF_FILE: {handoff}",
    }
    payload = parse_json(
        invoke(
            event,
            state_dir=state,
            extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
        )[0]
    )
    assert "decision" not in payload
    assert "reason=handoff_file_ready" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["handoff_path"] == str(handoff.resolve())
    assert summary["rvf_state_phase"] == "complete"
    assert summary["rvf_completion_gate"] == "handoff_file_ready"
    assert summary["rvf_handoff_path"] == str(handoff.resolve())
    assert summary["handoff_open_result"]["opened"] is True
    assert opener_marker.read_text(encoding="utf-8") == str(handoff.resolve())

    stdout, _ = invoke(
        event,
        state_dir=state,
        extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
    )
    payload = parse_json(stdout)
    summary = summary_from_payload(payload)
    assert summary["already_advised"] is True
    assert summary["handoff_open_result"]["reason"] == "already_advised"


def test_manual_handoff_open_suppresses_followup_advisory_open(tmp_path: Path) -> None:
    state = tmp_path / "state"
    handoff = tmp_path / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_log = tmp_path / "opened.txt"
    opener = tmp_path / "open_handoff.py"
    opener.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(opener_log)!r}).open('a', encoding='utf-8').write(sys.argv[-1] + '\\n')\n",
        encoding="utf-8",
    )
    opener.chmod(0o755)
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_"):
            env.pop(name, None)
    env.update(
        {
            "CODEX_RVF_IDE_OPEN_CMD": str(opener),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(RVF_HANDOFF), "open", str(handoff)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    manual_payload = parse_json(completed.stdout)
    assert manual_payload["opened"] is True
    assert manual_payload["manual_open_marker"]["marker_written"] is True

    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
        },
        state_dir=state,
        extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
    )
    payload = parse_json(stdout)
    summary = summary_from_payload(payload)
    assert summary["already_opened"] is True
    assert summary["handoff_open_result"]["reason"] == "already_opened"
    assert opener_log.read_text(encoding="utf-8").splitlines() == [str(handoff.resolve())]


def test_handoff_advisory_surfaces_finalize_record_errors(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repo = init_repo_with_head(tmp_path / "repo")
    run_dir, handoff = seed_finalize_run_dir(state=state, repo=repo)
    transcript = write_same_session_transcript_with_marker(
        tmp_path / "rollout.jsonl",
        repo,
    )
    (run_dir / "artifacts" / "analysis").write_text(
        "blocks analysis scaffold directory\n",
        encoding="utf-8",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "session_id": "child-session",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={"CODEX_RVF_OPEN_HANDOFF": "0"},
        )[0]
    )

    assert "finalize_errors=1" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["finalize_status"] == "warning"
    assert summary["finalize_error_count"] == 1
    assert summary["finalized_run_dir"] == str(run_dir.resolve())
    assert summary["finalize_errors"][0]["stage"] == "analysis_scaffold"
    run_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert run_summary["finalize"]["errors"][0]["stage"] == "analysis_scaffold"
    events = latest_events(state)
    assert any(event["event"] == "finalize_completed_with_errors" for event in events)


def test_handoff_advisory_surfaces_manual_rvf_analyze_trigger(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repo = init_repo_with_head(tmp_path / "repo")
    run_dir, handoff = seed_finalize_run_dir(state=state, repo=repo)
    transcript = write_same_session_transcript_with_marker(
        tmp_path / "rollout.jsonl",
        repo,
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "session_id": "child-session",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={"CODEX_RVF_OPEN_HANDOFF": "0"},
        )[0]
    )

    assert "rvf_analyze=manual_required" in payload["systemMessage"]
    assert f"$rvf-analyze {run_dir.resolve()}" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["rvf_analyze_status"] == "manual-required"
    assert summary["rvf_analyze_run_dir"] == str(run_dir.resolve())
    assert summary["rvf_analyze_summary_md_path"].endswith("/artifacts/analysis/summary.md")
    assert summary["rvf_analyze_causality_json_path"].endswith("/artifacts/analysis/causality.json")
    assert (run_dir / "artifacts" / "analysis" / "summary.md").is_file()
    assert (run_dir / "artifacts" / "analysis" / "causality.json").is_file()
    events = latest_events(state)
    assert any(event["event"] == "rvf_analyze_manual_advisory" for event in events)


def test_handoff_advisory_injects_rvf_analyze_in_kanban_task(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repo = init_repo_with_head(tmp_path / "repo")
    run_dir, handoff = seed_finalize_run_dir(state=state, repo=repo)
    transcript = write_same_session_transcript_with_marker(
        tmp_path / "rollout.jsonl",
        repo,
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-99', 'attempt_id': 'attempt-3', 'message_id': 'msg-analyze', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {sys.argv[1]}')\n",
        encoding="utf-8",
    )

    payload = parse_json(
        invoke(
            {
                "cwd": str(repo),
                "session_id": "child-session",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={
                "CODEX_RVF_OPEN_HANDOFF": "0",
                "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "fake task",
                "KANBAN_TASK_ID": "task-99",
                "KANBAN_ATTEMPT_ID": "attempt-3",
                "KANBAN_PROJECT_PATH": str(repo),
                "FAKE_CLIENT_CALLS": str(client_calls),
            },
        )[0]
    )

    assert "rvf_analyze=kanban_injected" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["rvf_analyze_status"] == "kanban-injected"
    assert summary["rvf_analyze_kanban_task_id"] == "task-99"
    assert summary["rvf_analyze_kanban_attempt_id"] == "attempt-3"
    assert summary["rvf_analyze_kanban_message_id"] == "msg-analyze"
    prompt_text = Path(summary["rvf_analyze_followup_prompt_path"]).read_text(encoding="utf-8")
    assert f"$rvf-analyze {run_dir.resolve()}" in prompt_text
    assert "RVF_KANBAN_ANALYZE_TRIGGER" in prompt_text
    call = json.loads(client_calls.read_text(encoding="utf-8").splitlines()[0])
    assert call["argv"][0] == "message"
    assert "--source" in call["argv"]
    assert "rvf-analyze" in call["argv"]


def test_handoff_advisory_respects_open_disabled(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp_path),
                "session_id": "child-session",
                "stop_hook_active": False,
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={
                "CODEX_RVF_OPEN_HANDOFF": "0",
                "CODEX_RVF_IDE_OPEN_CMD": str(opener),
            },
        )[0]
    )
    summary = summary_from_payload(payload)
    assert summary["handoff_open_enabled"] is False
    assert summary["handoff_open_result"]["reason"] == "disabled"
    assert not opener_marker.exists()


def test_handoff_advisory_records_open_failure(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker, fail=True)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp_path),
                "session_id": "child-session",
                "stop_hook_active": False,
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
        )[0]
    )
    assert payload["continue"] is True
    summary = summary_from_payload(payload)
    assert summary["handoff_open_result"]["opened"] is False
    assert summary["handoff_open_result"]["reason"] == "command_failed"
    assert opener_marker.read_text(encoding="utf-8") == str(handoff.resolve())


def test_suppress_env_skips_handoff_marker_before_advisory(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp_path),
                "session_id": "headless-child",
                "stop_hook_active": False,
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={
                "CODEX_RVF_SUPPRESS_STOP_HOOK": "1",
                "CODEX_RVF_IDE_OPEN_CMD": str(opener),
            },
        )[0]
    )
    assert payload["continue"] is True
    assert "reason=suppressed" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"
    assert not opener_marker.exists()
    assert not (state / "handoff-advised").exists()


def test_stop_hook_active_skips_handoff_marker_before_advisory(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "child-session",
                "stop_hook_active": True,
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
        )[0]
    )
    assert payload["continue"] is True
    assert "reason=stop_hook_active" in payload["systemMessage"]
    assert not opener_marker.exists()
    assert not (state / "handoff-advised").exists()


def test_handoff_marker_in_dirty_repo_does_not_create_new_fork(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "dirty", dirty=True)
    state = tmp_path / "state"
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener = write_fake_opener(tmp_path / "open_handoff.py", tmp_path / "opened.txt")

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "child-session",
                "stop_hook_active": False,
                "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
            },
            state_dir=state,
            extra_env={
                "CODEX_RVF_MODE": "fork",
                "CODEX_RVF_FORK_MODE": "dry-run",
                "CODEX_RVF_IDE_OPEN_CMD": str(opener),
            },
        )[0]
    )
    assert "reason=handoff_file_ready" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert "app_server_requests_path" not in summary


def test_forked_rvf_session_waits_for_handoff_before_advisory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
            "last_assistant_message": "我还需要继续检查，尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-advised").exists()


def test_forked_rvf_session_waits_when_handoff_message_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {tmp_path / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-advised").exists()


def test_invalid_handoff_marker_continues_existing_gate(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    missing = tmp_path / "missing.md"
    stdout, _ = invoke(
        {
            "cwd": str(tmp_path),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_assistant_message": f"RVF_HANDOFF_FILE: {missing}",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "当前 cwd 不在 git repo/worktree 内")


def test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "repo", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {dirty}\n"
    )
    write_user_session_messages(
        transcript,
        "child-session",
        [
            fork_prompt,
            "后续用户消息遮住了最初的 fork marker。",
        ],
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "child-session",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "last_assistant_message": "尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert latest_pointer(state)["status"] == "skipped"


def test_forked_rvf_marker_scan_skips_incomplete_earlier_marker(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "repo", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp_path}\n"
        f"RVF_TARGET_REPO: {dirty}\n"
    )
    write_user_session_messages(
        transcript,
        "child-session",
        [
            "早先普通讨论里提到了 RVF_FORKED_REVIEW_VALIDATE_FIX，但没有完整 metadata。",
            fork_prompt,
            "后续用户消息遮住了最初的 fork marker。",
        ],
    )

    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "session_id": "child-session",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "last_assistant_message": "尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert latest_pointer(state)["status"] == "skipped"


def test_incomplete_fork_marker_in_transcript_does_not_skip_dirty_repo(tmp_path: Path) -> None:
    dirty = init_repo(tmp_path / "repo", dirty=True)
    state = tmp_path / "state"
    transcript = tmp_path / "session.jsonl"
    write_user_session_messages(
        transcript,
        "ordinary-session",
        [
            "普通讨论里提到了 RVF_FORKED_REVIEW_VALIDATE_FIX，但没有 fork metadata。",
            "请继续处理当前 dirty repo。",
        ],
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "input": (
                            "*** Begin Patch\n"
                            "*** Update File: changed.txt\n"
                            "@@\n"
                            "-old\n"
                            "+new\n"
                            "*** End Patch\n"
                        ),
                        "call_id": "call_patch",
                    },
                }
            )
            + "\n"
        )

    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "ordinary-session",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            extra_env={"CODEX_RVF_FORK_MODE": "dry-run"},
            state_dir=state,
        )[0]
    )
    assert "decision" not in payload
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "dry-run"
    prompt = prompt_text(latest)
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt
    assert str(dirty) in prompt


def test_missing_cwd_skips_and_requests_target_repo(tmp_path: Path) -> None:
    payload = parse_json(invoke({"stop_hook_active": False})[0])
    assert "decision" not in payload
    assert payload["continue"] is True
    summary = summary_from_payload(payload)
    assert "Stop event 未提供可检查的 cwd" in str(summary["message"])
    assert "提供要运行 review-validate-fix 的目标 repo 路径" in str(summary["message"])


def test_log_unavailable_does_not_break_hook_payload(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_file = tmp_path / "state-is-a-file"
    state_file.write_text("not a directory\n", encoding="utf-8")
    payload = parse_json(invoke({"stop_hook_active": False}, state_dir=state_file)[0])
    assert "decision" not in payload
    assert payload["continue"] is True
    assert "log_unavailable=true" in payload["systemMessage"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")

    tests = [
        test_normalize_backend_from_env,
        test_dispatch_flow_helpers_lock_route_and_fallback_contract,
        test_parent_conversation_origin_prefers_app_server_chat_name,
        test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed,
        test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed,
        test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed,
        test_parent_conversation_origin_uses_stable_ref_when_chat_lookup_fails,
        test_rvf_fork_prompt_includes_parent_origin_metadata_for_legacy_gui,
        test_parent_thread_name_from_app_server_reads_thread_name,
        test_fork_experiment_marker_no_longer_triggers_stop_hook_fork,
        test_diagnose_codex_fork_dry_run_writes_requests,
        test_stop_hook_active_skips,
        test_env_suppression_skips,
        test_prompt_suppression_marker_skips,
        test_prior_cline_kanban_task_marker_skips_after_later_user_message,
        test_kanban_task_suppression_marker_skips_without_prompt_marker,
        test_session_without_owned_dirty_skips_fork,
        test_session_without_owned_dirty_legacy_disable_keeps_old_codes,
        test_resolve_stop_context_returns_session_branch_worktree,
        test_evaluate_session_gate_suppresses_on_manual_marker,
        test_legacy_session_scope_gate_payload_used_when_tracker_disabled,
        test_allocate_auto_review_scope_writes_artifact_when_scope_present,
        test_kanban_followup_auto_review_scope_uses_one_hour_lease_ttl,
        test_kanban_followup_without_task_id_does_not_allocate_review_scope,
        test_evaluate_session_gate_skips_when_manual_run_recorded_for_scope_hash,
        test_manual_scope_suppression_does_not_transfer_parent_takeover_units,
        test_evaluate_session_gate_file_marker_takes_precedence_over_db_marker,
        test_session_scope_gate_payload_emits_session_manifest_failed_when_refresh_fails,
        test_session_hook_default_state_dir_is_skill_state_session_hook,
        test_session_hook_state_dir_respects_state_dir_override,
        test_manual_rvf_session_marker_write_read_clear_preserves_hook_state,
        test_manual_rvf_session_marker_skips_before_fork_gate,
        test_manual_rvf_session_marker_dirty_change_does_not_suppress,
        test_manual_rvf_session_marker_expired_does_not_read,
        test_socket_probe_reports_unavailable_reason,
        test_app_server_websocket_sends_http_upgrade,
        test_app_server_websocket_masks_pong_frame,
        test_socket_probe_requires_websocket_upgrade,
        test_bridge_failure_preserves_desktop_probe,
        test_missing_desktop_control_reports_failure_not_bridge_or_continuation,
        test_missing_desktop_control_auto_uses_existing_bridge,
        test_bridge_app_server_listener_pids_filters_rvf_socket,
        test_restart_bridge_stops_existing_listener_before_relaunch,
        test_bridge_app_server_error_restarts_bridge_once,
        test_cline_kanban_mode_creates_and_starts_task_with_same_run,
        test_cline_kanban_mode_requires_workspace_path,
        test_auto_mode_creates_cline_kanban_task_by_default,
        test_auto_mode_reports_kanban_unavailable_without_default_gui_fallback,
        test_auto_mode_can_opt_into_legacy_gui_as_backup_of_backup,
        test_auto_mode_reports_stale_kanban_listener_without_gui_fallback,
        test_cline_kanban_mode_without_transcript_fail_closes_before_task_start,
        test_cline_kanban_mode_blocks_expired_codex_login_before_task_start,
        test_kanban_followup_mode_injects_current_task_message,
        test_kanban_followup_title_falls_back_to_local_board_state,
        test_kanban_followup_title_ignores_unrelated_board_with_same_task_id,
        test_kanban_followup_title_uses_session_matched_board_state,
        test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd,
        test_kanban_followup_blocks_expired_codex_login_before_message,
        test_kanban_followup_mode_without_task_id_reports_without_fallback,
        test_kanban_followup_trigger_marker_skips_one_turn,
        test_rvf_analyze_followup_trigger_marker_skips_one_turn,
        test_cline_kanban_mode_marks_unavailable_when_task_start_fails,
        test_fork_experiment_missing_desktop_control_prepares_manual_not_continuation,
        test_missing_desktop_control_fail_policy_reports,
        test_fork_session_visibility_waits_only_for_active_session,
        test_app_server_fork_waits_for_session_file_before_deeplink,
        test_desktop_control_fork_requires_active_session_for_verified_gui,
        test_bridge_fork_message_marks_gui_visibility_unverified,
        test_open_gui_fork_disabled_skips_retry_sleep,
        test_open_gui_fork_success_stops_retries,
        test_open_gui_fork_unsupported_platform_skips_retry_sleep,
        test_session_hook_control_disables_current_session,
        test_session_hook_control_status_reports_current_session,
        test_session_hook_control_status_works_when_env_suppressed,
        test_session_hook_control_reenables_current_session,
        test_session_hook_control_reenable_starts_cline_kanban_task,
        test_disabled_session_skips_fork_experiment_marker,
        test_subagent_source_ignores_session_hook_control,
        test_subagent_source_skips,
        test_subagent_session_meta_skips,
        test_clean_repo_skips,
        test_plan_document_only_routes_out_of_full_rvf,
        test_read_only_session_with_background_plan_docs_does_not_plan_route,
        test_plan_document_route_does_not_hide_source_rename,
        test_dirty_repo_dry_run_prepares_legacy_gui_requests,
        test_dirty_repo_manual_mode_only_prepares_prompt,
        test_dirty_repo_fork_dry_run,
        test_dirty_repo_fork_inherits_parent_cwd_inside_worktree,
        test_no_git_cwd_skips_even_with_dirty_trusted_repo,
        test_stop_event_transcript_path_overrides_bad_env_thread_id,
        test_stop_event_log_path_is_not_used_as_fork_rollout_path,
        test_dirty_repo_continuation_mode_reports_removed_fallback,
        test_forked_rvf_session_gets_programmatic_handoff_advisory,
        test_handoff_advisory_surfaces_finalize_record_errors,
        test_handoff_advisory_surfaces_manual_rvf_analyze_trigger,
        test_handoff_advisory_injects_rvf_analyze_in_kanban_task,
        test_handoff_advisory_respects_open_disabled,
        test_handoff_advisory_records_open_failure,
        test_suppress_env_skips_handoff_marker_before_advisory,
        test_stop_hook_active_skips_handoff_marker_before_advisory,
        test_handoff_marker_in_dirty_repo_does_not_create_new_fork,
        test_forked_rvf_session_waits_for_handoff_before_advisory,
        test_forked_rvf_session_waits_when_handoff_message_missing,
        test_invalid_handoff_marker_continues_existing_gate,
        test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message,
        test_forked_rvf_marker_scan_skips_incomplete_earlier_marker,
        test_incomplete_fork_marker_in_transcript_does_not_skip_dirty_repo,
        test_missing_cwd_skips_and_requests_target_repo,
        test_log_unavailable_does_not_break_hook_payload,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        selected = [
            test
            for index, test in enumerate(tests)
            if args.shard_count <= 1 or index % args.shard_count == args.shard_index
        ]
        for test in selected:
            test(root / test.__name__)
    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )
    print(f"codex stop review-validate-fix hook tests OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
