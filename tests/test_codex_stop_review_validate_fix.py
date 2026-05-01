#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
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
        "-old\n"
        "+new\n"
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


def test_normalize_backend_from_env(tmp: Path) -> None:
    module = load_hook_module()
    original = {name: os.environ.get(name) for name in ("CODEX_RVF_MODE", "CODEX_RVF_FORK_MODE")}
    cases = [
        ({}, "gui"),
        ({"CODEX_RVF_MODE": "off"}, "off"),
        ({"CODEX_RVF_MODE": "continuation"}, "report-only"),
        ({"CODEX_RVF_MODE": "block"}, "report-only"),
        ({"CODEX_RVF_FORK_MODE": "manual"}, "manual"),
        ({"CODEX_RVF_FORK_MODE": "prepare"}, "manual"),
        ({"CODEX_RVF_FORK_MODE": "dry-run"}, "dry-run"),
        ({"CODEX_RVF_FORK_MODE": "cline-kanban"}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "cline"}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "ck"}, "kanban"),
        ({"CODEX_RVF_FORK_MODE": "kanban-followup"}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "kanban-message"}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "kanban-inject"}, "kanban-followup"),
        ({"CODEX_RVF_FORK_MODE": "surprise"}, "surprise"),
    ]
    try:
        for env, expected in cases:
            os.environ.pop("CODEX_RVF_MODE", None)
            os.environ.pop("CODEX_RVF_FORK_MODE", None)
            os.environ.update(env)
            assert module.normalize_backend_from_env() == expected
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_parent_conversation_origin_prefers_app_server_chat_name(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)
    transcript = tmp / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
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


def test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)
    transcript = tmp / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
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


def test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)
    transcript = tmp / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
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


def test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)
    transcript = tmp / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
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


def test_parent_conversation_origin_uses_stable_ref_when_chat_lookup_fails(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)
    transcript = tmp / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
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


def test_parent_thread_name_from_app_server_reads_thread_name(tmp: Path) -> None:
    module = load_hook_module()
    socket_path = tmp / "app-server.sock"
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
        lookup = module.parent_thread_name_from_app_server("parent-thread", str(tmp))
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


def test_fork_experiment_marker_no_longer_triggers_stop_hook_fork(tmp: Path) -> None:
    transcript = tmp / "session.jsonl"
    state = tmp / "state"
    write_user_session(
        transcript,
        "00000000-0000-0000-0000-000000000001",
        "RVF_FORK_EXPERIMENT: what is 2+2?",
    )
    stdout, _ = invoke(
        {
            "cwd": str(tmp),
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


def test_diagnose_codex_fork_dry_run_writes_requests(tmp: Path) -> None:
    state = tmp / "state"
    message = "RVF_FORK_EXPERIMENT: custom diagnostic message"
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_"):
            env.pop(name, None)
    env["CODEX_RVF_STATE_DIR"] = str(state)
    completed = subprocess.run(
        [sys.executable, str(DIAGNOSTIC_SCRIPT), "--mode", "dry-run", "--message", message],
        input=json.dumps({"session_id": "parent-thread", "cwd": str(tmp)}),
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


def test_stop_hook_active_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke({"cwd": str(dirty), "stop_hook_active": True})
    payload = assert_skip_reason(stdout, "stop_hook_active=true")
    assert "detail=Codex 已在执行 Stop hook，RVF 跳过以避免递归" in payload["systemMessage"]


def test_env_suppression_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    run_dir = tmp / "state" / "runs" / "rvf-child"
    stdout, _ = invoke(
        {"cwd": str(dirty), "stop_hook_active": False},
        extra_env={
            "CODEX_RVF_SUPPRESS_STOP_HOOK": "1",
            "CODEX_RVF_RUN_ID": "rvf-child",
            "CODEX_RVF_RUN_DIR": str(run_dir),
        },
    )
    payload = parse_json(stdout)
    assert payload["systemMessage"] == "review-validate-fix: skipped; reason=suppressed"
    assert "summary=" not in payload["systemMessage"]
    assert not run_dir.exists()


def test_prompt_suppression_marker_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
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
    )
    payload = parse_json(stdout)
    assert payload["systemMessage"] == "review-validate-fix: skipped; reason=suppressed"
    assert "summary=" not in payload["systemMessage"]


def test_prior_cline_kanban_task_marker_skips_after_later_user_message(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
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
    )
    payload = parse_json(stdout)
    assert payload["systemMessage"] == "review-validate-fix: skipped; reason=suppressed"
    assert "summary=" not in payload["systemMessage"]


def test_kanban_task_suppression_marker_skips_without_prompt_marker(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
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
    assert payload["systemMessage"] == "review-validate-fix: skipped; reason=suppressed"
    assert "summary=" not in payload["systemMessage"]


def test_session_without_owned_dirty_skips_fork(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
    state = tmp / "state"
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

    payload = assert_skip_reason(stdout, "no session-owned dirty paths")
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "no_session_owned_dirty"
    assert "app_server_requests_path" not in summary


def test_session_hook_default_state_dir_is_skill_state_session_hook(tmp: Path) -> None:
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


def test_session_hook_state_dir_respects_state_dir_override(tmp: Path) -> None:
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp / "state-root")
    try:
        module = load_hook_module()
        assert module.session_hook_state_dir() == tmp / "state-root" / "session-hook"
    finally:
        if old_state is None:
            os.environ.pop("CODEX_RVF_STATE_DIR", None)
        else:
            os.environ["CODEX_RVF_STATE_DIR"] = old_state
        if old_session_state is not None:
            os.environ["CODEX_RVF_SESSION_HOOK_STATE_DIR"] = old_session_state


def test_manual_rvf_session_marker_write_read_clear_preserves_hook_state(tmp: Path) -> None:
    module = load_hook_module()
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp / "state-root")
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
        assert path == tmp / "state-root" / "session-hook" / "manual_session.json"

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


def test_manual_rvf_session_marker_skips_before_fork_gate(tmp: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp / "dirty")
    state = tmp / "state"
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


def test_manual_rvf_session_marker_dirty_change_does_not_suppress(tmp: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp / "dirty")
    state = tmp / "state"
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


def test_manual_rvf_session_marker_expired_does_not_read(tmp: Path) -> None:
    module = load_hook_module()
    dirty = init_repo_with_head(tmp / "dirty")
    old_state = os.environ.get("CODEX_RVF_STATE_DIR")
    old_session_state = os.environ.pop("CODEX_RVF_SESSION_HOOK_STATE_DIR", None)
    os.environ["CODEX_RVF_STATE_DIR"] = str(tmp / "state")
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


def test_socket_probe_reports_unavailable_reason(tmp: Path) -> None:
    module = load_hook_module()
    tmp.mkdir(parents=True, exist_ok=True)

    missing = tmp / "missing.sock"
    missing_probe = module.probe_app_server_socket(missing)
    assert missing_probe["connect_ok"] is False
    assert missing_probe["reason"] == "missing"
    assert missing_probe["parent_exists"] is True

    regular = tmp / "regular.sock"
    regular.write_text("not a socket\n", encoding="utf-8")
    regular_probe = module.probe_app_server_socket(regular)
    assert regular_probe["connect_ok"] is False
    assert regular_probe["reason"] == "not-a-socket"
    assert regular_probe["exists"] is True


def test_bridge_failure_preserves_desktop_probe(tmp: Path) -> None:
    module = load_hook_module()
    state = tmp / "state"
    desktop_socket = tmp / "missing-control.sock"
    bridge_socket = tmp / "missing-bridge.sock"
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
            cwd=str(tmp),
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


def test_missing_desktop_control_reports_failure_not_bridge_or_continuation(tmp: Path) -> None:
    module = load_hook_module()
    state = tmp / "state"
    desktop_socket = tmp / "missing-control.sock"
    bridge_socket = tmp / "missing-bridge.sock"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_bridge_policy = os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
    original_allow_bridge = os.environ.pop("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER", None)
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket
        module.ensure_bridge_app_server = lambda: (_ for _ in ()).throw(
            AssertionError("bridge should not start by default")
        )
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(tmp),
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


def test_cline_kanban_mode_creates_and_starts_task_with_same_run(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123', 'workspace_path': '/tmp/task-worktree'}))\n"
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
        os.environ["FAKE_CLIENT_CALLS"] = str(client_calls)
        transcript = write_apply_patch_transcript(tmp / "session.jsonl", repo)
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
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert latest["cline_kanban_task_id"] == "task-123"
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
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in prompt_text
    assert '. "$RVF_ARTIFACTS_DIR/review-env.sh"' in prompt_text
    assert 'export RVF_REPO="$RVF_TASK_REPO"' in prompt_text
    assert '--metadata "$RVF_WORKTREE_BOOTSTRAP" --repo "$RVF_REPO"' in prompt_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in prompt_text
    assert "- session manifest: `$RVF_SESSION_MANIFEST`" in prompt_text
    assert "`$RVF_ARTIFACTS_DIR/handoff.md`" in prompt_text
    artifacts_dir = latest["artifacts_dir"]
    assert f"{artifacts_dir}/review-packet.md" not in prompt_text
    assert f"{artifacts_dir}/session-manifest.json" not in prompt_text
    assert f"{artifacts_dir}/worktree-bootstrap.json" not in prompt_text
    task_title = create_argv[create_argv.index("--title") + 1]
    assert task_title.startswith("RVF from Codex parent-thread run ")
    assert " repo " not in task_title
    assert latest["parent_conversation_ref"] == "Codex parent-thread"
    assert latest["parent_codex_url"] == "codex://local/parent-thread"
    assert Path(latest["parent_origin_path"]).exists()
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


def test_cline_kanban_mode_without_transcript_fail_closes_before_task_start(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
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


def test_cline_kanban_mode_blocks_expired_codex_login_before_task_start(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    transcript = write_apply_patch_transcript(tmp / "session.jsonl", repo)
    fake_codex = tmp / "fake_codex.py"
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
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
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


def test_kanban_followup_mode_injects_current_task_message(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "if sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-77', 'attempt_id': 'attempt-9', 'message_id': 'msg-77', 'status': 'queued', 'checkpoint_id': 'checkpoint-1'}))\n"
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
    assert latest["cline_kanban_task_id"] == "task-77"
    assert latest["cline_kanban_attempt_id"] == "attempt-9"
    assert latest["cline_kanban_message_id"] == "msg-77"
    assert latest["cline_kanban_checkpoint_id"] == "checkpoint-1"
    assert "parent_thread_id" not in latest or latest["parent_thread_id"] is None
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["message"]
    message_argv = calls[0]["argv"]
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
    assert "RVF_CLINE_KANBAN_TASK" not in prompt_text
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
    assert calls[0]["suppress"] is None


def test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    subdir = repo / "nested"
    subdir.mkdir()
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1] == 'message':\n"
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
    message_argv = calls[0]["argv"]
    assert message_argv[message_argv.index("--repo") + 1] == str(repo.resolve())


def test_kanban_followup_blocks_expired_codex_login_before_message(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_codex = tmp / "fake_codex.py"
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
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
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


def test_kanban_followup_mode_without_task_id_reports_without_fallback(tmp: Path) -> None:
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
    client_calls = tmp / "client-calls.jsonl"
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


def test_kanban_followup_trigger_marker_skips_one_turn(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"

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


def test_cline_kanban_mode_marks_unavailable_when_task_start_fails(tmp: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp / "repo")
    state = tmp / "state"
    fake_client = tmp / "fake_cline_kanban_client.py"
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
            parent_thread_path=write_apply_patch_transcript(tmp / "session.jsonl", repo),
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
    tmp: Path,
) -> None:
    state = tmp / "state"
    home = tmp / "home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CODEX_RVF_"):
            env.pop(name, None)
    env["HOME"] = str(home)
    env["CODEX_RVF_STATE_DIR"] = str(state)
    completed = subprocess.run(
        [sys.executable, str(DIAGNOSTIC_SCRIPT)],
        input=json.dumps({"session_id": "parent-thread", "cwd": str(tmp)}),
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


def test_missing_desktop_control_fail_policy_reports(tmp: Path) -> None:
    module = load_hook_module()
    state = tmp / "state"
    desktop_socket = tmp / "missing-control.sock"
    bridge_socket = tmp / "missing-bridge.sock"
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
            cwd=str(tmp),
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


def test_fork_session_visibility_waits_only_for_active_session(tmp: Path) -> None:
    module = load_hook_module()
    original_sessions_dir = module.DEFAULT_CODEX_SESSIONS_DIR
    try:
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp / "sessions"
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


def test_app_server_fork_waits_for_session_file_before_deeplink(tmp: Path) -> None:
    module = load_hook_module()
    socket_path = tmp / "app-server.sock"
    active_path = tmp / "sessions" / "rollout-fork-wait.jsonl"
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
                        "cwd": str(tmp),
                        "source": "vscode",
                    }
                }
            if method == "thread/list":
                assert params is not None
                assert params["sortKey"] == "updated_at"
                assert params["useStateDbOnly"] is False
                assert params["cwd"] == str(tmp)
                return {
                    "data": [
                        {
                            "id": "fork-wait",
                            "path": str(active_path),
                            "cwd": str(tmp),
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
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp / "sessions"
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
            cwd=str(tmp),
            prompt="$review-validate-fix",
            model=None,
            reasoning_effort=None,
            log_path=tmp / "hook.json",
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
    tmp: Path,
) -> None:
    module = load_hook_module()
    socket_path = tmp / "app-server.sock"
    missing_path = tmp / "sessions" / "rollout-fork-missing.jsonl"
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
                        "cwd": str(tmp),
                        "source": "vscode",
                    }
                }
            if method == "thread/list":
                return {
                    "data": [
                        {
                            "id": "fork-missing",
                            "path": str(missing_path),
                            "cwd": str(tmp),
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
        module.DEFAULT_CODEX_SESSIONS_DIR = tmp / "sessions"
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
            cwd=str(tmp),
            prompt="$review-validate-fix",
            model=None,
            reasoning_effort=None,
            log_path=tmp / "hook.json",
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


def test_bridge_fork_message_marks_gui_visibility_unverified(tmp: Path) -> None:
    module = load_hook_module()
    state = tmp / "state"
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
            cwd=str(tmp),
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


def test_open_gui_fork_disabled_skips_retry_sleep(tmp: Path) -> None:
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


def test_open_gui_fork_success_stops_retries(tmp: Path) -> None:
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


def test_open_gui_fork_unsupported_platform_skips_retry_sleep(tmp: Path) -> None:
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


def test_session_hook_control_disables_current_session(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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


def test_session_hook_control_status_reports_current_session(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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


def test_session_hook_control_status_works_when_env_suppressed(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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


def test_session_hook_control_reenables_current_session(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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

    write_user_session(
        transcript,
        "session-reenabled",
        "RVF_STOP_HOOK: on",
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
    summary = summary_from_payload(payload)
    assert summary["reason_code"] == "session_hook_gate_enabled"
    assert summary["control_action"] == "on"
    assert summary["session_hook_gate_state"] == "enabled"
    assert "enabled" in str(summary["message"])
    assert "不是关闭全局 Stop hook" in str(summary["message"])
    assert not (state / "session-hook" / "session-reenabled.json").exists()
    assert latest_pointer(state)["status"] == "session-hook-control"

    write_apply_patch_transcript(transcript, dirty)
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
    assert "review-validate-fix: dry-run; reason=dry_run;" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "dry-run"


def test_disabled_session_skips_fork_experiment_marker(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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


def test_subagent_source_ignores_session_hook_control(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
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


def test_subagent_source_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
        }
    )
    assert_skip_reason(stdout, "subagent")


def test_subagent_session_meta_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "subagent.jsonl"
    write_subagent_session(transcript)
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        }
    )
    assert_skip_reason(stdout, "subagent")


def test_clean_repo_skips(tmp: Path) -> None:
    clean = init_repo(tmp / "clean", dirty=False)
    stdout, _ = invoke({"cwd": str(clean), "stop_hook_active": False})
    assert_skip_reason(stdout, "clean")


def test_dirty_repo_forks_in_gui_by_default(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
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


def test_dirty_repo_manual_mode_only_prepares_prompt(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
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
    assert Path(latest["prompt_path"]).exists()


def test_dirty_repo_fork_dry_run(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
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
    assert "$review-validate-fix" in prompt
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt
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


def test_dirty_repo_fork_inherits_parent_cwd_inside_worktree(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    subdir = dirty / "nested"
    subdir.mkdir()
    state = tmp / "state"

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


def test_no_git_cwd_skips_even_with_dirty_trusted_repo(tmp: Path) -> None:
    plain = tmp / "plain"
    plain.mkdir(parents=True)
    dirty = init_repo(tmp / "dirty", dirty=True)
    config = tmp / "config.toml"
    state = tmp / "state"
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


def test_stop_event_transcript_path_overrides_bad_env_thread_id(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
    state = tmp / "state"
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


def test_stop_event_log_path_is_not_used_as_fork_rollout_path(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    log_path = tmp / "hook.log"
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


def test_dirty_repo_continuation_mode_reports_removed_fallback(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
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


def test_forked_rvf_session_gets_programmatic_handoff_advisory(tmp: Path) -> None:
    state = tmp / "state"
    handoff = tmp / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp / "opened.txt"
    opener = write_fake_opener(tmp / "open_handoff.py", opener_marker)
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp}\n"
        f"RVF_TARGET_REPO: {tmp / 'repo'}\n"
    )

    event = {
        "cwd": str(tmp),
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


def test_handoff_advisory_respects_open_disabled(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state"
    handoff = tmp / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp / "opened.txt"
    opener = write_fake_opener(tmp / "open_handoff.py", opener_marker)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp),
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


def test_handoff_advisory_records_open_failure(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state"
    handoff = tmp / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp / "opened.txt"
    opener = write_fake_opener(tmp / "open_handoff.py", opener_marker, fail=True)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp),
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


def test_suppress_env_skips_handoff_marker_before_advisory(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state"
    handoff = tmp / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp / "opened.txt"
    opener = write_fake_opener(tmp / "open_handoff.py", opener_marker)

    payload = parse_json(
        invoke(
            {
                "cwd": str(tmp),
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
    assert not opener_marker.exists()
    assert not (state / "handoff-advised").exists()


def test_stop_hook_active_skips_handoff_marker_before_advisory(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    handoff = tmp / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp / "opened.txt"
    opener = write_fake_opener(tmp / "open_handoff.py", opener_marker)

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


def test_handoff_marker_in_dirty_repo_does_not_create_new_fork(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    state = tmp / "state"
    handoff = tmp / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener = write_fake_opener(tmp / "open_handoff.py", tmp / "opened.txt")

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


def test_forked_rvf_session_waits_for_handoff_before_advisory(tmp: Path) -> None:
    state = tmp / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp}\n"
        f"RVF_TARGET_REPO: {tmp / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
            "last_assistant_message": "我还需要继续检查，尚未生成 handoff。",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-advised").exists()


def test_forked_rvf_session_waits_when_handoff_message_missing(tmp: Path) -> None:
    state = tmp / "state"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp}\n"
        f"RVF_TARGET_REPO: {tmp / 'repo'}\n"
    )

    stdout, _ = invoke(
        {
            "cwd": str(tmp),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_user_message": fork_prompt,
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "已是 review-validate-fix fork")
    assert not (state / "handoff-advised").exists()


def test_invalid_handoff_marker_continues_existing_gate(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state"
    missing = tmp / "missing.md"
    stdout, _ = invoke(
        {
            "cwd": str(tmp),
            "session_id": "child-session",
            "stop_hook_active": False,
            "last_assistant_message": f"RVF_HANDOFF_FILE: {missing}",
        },
        state_dir=state,
    )
    assert_skip_reason(stdout, "当前 cwd 不在 git repo/worktree 内")


def test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message(tmp: Path) -> None:
    dirty = init_repo(tmp / "repo", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp}\n"
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


def test_forked_rvf_marker_scan_skips_incomplete_earlier_marker(tmp: Path) -> None:
    dirty = init_repo(tmp / "repo", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
    fork_prompt = (
        "$review-validate-fix\n\n"
        "RVF_FORKED_REVIEW_VALIDATE_FIX\n"
        "RVF_PARENT_SESSION_ID: parent-session\n"
        f"RVF_PARENT_CWD: {tmp}\n"
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


def test_incomplete_fork_marker_in_transcript_does_not_skip_dirty_repo(tmp: Path) -> None:
    dirty = init_repo(tmp / "repo", dirty=True)
    state = tmp / "state"
    transcript = tmp / "session.jsonl"
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


def test_missing_cwd_skips_and_requests_target_repo(tmp: Path) -> None:
    payload = parse_json(invoke({"stop_hook_active": False})[0])
    assert "decision" not in payload
    assert payload["continue"] is True
    summary = summary_from_payload(payload)
    assert "Stop event 未提供可检查的 cwd" in str(summary["message"])
    assert "提供要运行 review-validate-fix 的目标 repo 路径" in str(summary["message"])


def test_log_unavailable_does_not_break_hook_payload(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    state_file = tmp / "state-is-a-file"
    state_file.write_text("not a directory\n", encoding="utf-8")
    payload = parse_json(invoke({"stop_hook_active": False}, state_dir=state_file)[0])
    assert "decision" not in payload
    assert payload["continue"] is True
    assert "log_unavailable=true" in payload["systemMessage"]


def main() -> int:
    tests = [
        test_normalize_backend_from_env,
        test_parent_conversation_origin_prefers_app_server_chat_name,
        test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed,
        test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed,
        test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed,
        test_parent_conversation_origin_uses_stable_ref_when_chat_lookup_fails,
        test_parent_thread_name_from_app_server_reads_thread_name,
        test_fork_experiment_marker_no_longer_triggers_stop_hook_fork,
        test_diagnose_codex_fork_dry_run_writes_requests,
        test_stop_hook_active_skips,
        test_env_suppression_skips,
        test_prompt_suppression_marker_skips,
        test_prior_cline_kanban_task_marker_skips_after_later_user_message,
        test_kanban_task_suppression_marker_skips_without_prompt_marker,
        test_session_without_owned_dirty_skips_fork,
        test_session_hook_default_state_dir_is_skill_state_session_hook,
        test_session_hook_state_dir_respects_state_dir_override,
        test_manual_rvf_session_marker_write_read_clear_preserves_hook_state,
        test_manual_rvf_session_marker_skips_before_fork_gate,
        test_manual_rvf_session_marker_dirty_change_does_not_suppress,
        test_manual_rvf_session_marker_expired_does_not_read,
        test_socket_probe_reports_unavailable_reason,
        test_bridge_failure_preserves_desktop_probe,
        test_missing_desktop_control_reports_failure_not_bridge_or_continuation,
        test_cline_kanban_mode_creates_and_starts_task_with_same_run,
        test_cline_kanban_mode_without_transcript_fail_closes_before_task_start,
        test_cline_kanban_mode_blocks_expired_codex_login_before_task_start,
        test_kanban_followup_mode_injects_current_task_message,
        test_kanban_followup_mode_uses_repo_root_project_path_for_subdir_cwd,
        test_kanban_followup_blocks_expired_codex_login_before_message,
        test_kanban_followup_mode_without_task_id_reports_without_fallback,
        test_kanban_followup_trigger_marker_skips_one_turn,
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
        test_disabled_session_skips_fork_experiment_marker,
        test_subagent_source_ignores_session_hook_control,
        test_subagent_source_skips,
        test_subagent_session_meta_skips,
        test_clean_repo_skips,
        test_dirty_repo_forks_in_gui_by_default,
        test_dirty_repo_manual_mode_only_prepares_prompt,
        test_dirty_repo_fork_dry_run,
        test_dirty_repo_fork_inherits_parent_cwd_inside_worktree,
        test_no_git_cwd_skips_even_with_dirty_trusted_repo,
        test_stop_event_transcript_path_overrides_bad_env_thread_id,
        test_stop_event_log_path_is_not_used_as_fork_rollout_path,
        test_dirty_repo_continuation_mode_reports_removed_fallback,
        test_forked_rvf_session_gets_programmatic_handoff_advisory,
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
        for test in tests:
            test(root / test.__name__)
    print("codex stop review-validate-fix hook tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
