#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("codex_stop_review_validate_fix.py")


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def init_repo(path: Path, dirty: bool) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], path)
    if dirty:
        (path / "changed.txt").write_text("dirty\n", encoding="utf-8")
    return path


def invoke(
    event: dict[str, object],
    config: Path | None = None,
    extra_env: dict[str, str] | None = None,
    state_dir: Path | None = None,
) -> tuple[str, str]:
    env = os.environ.copy()
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


def assert_skip_reason(stdout: str, expected: str) -> dict[str, object]:
    payload = parse_json(stdout)
    assert "decision" not in payload
    assert "未创建 fork" in payload["systemMessage"]
    assert expected in payload["systemMessage"]
    return payload


def load_hook_module():
    spec = importlib.util.spec_from_file_location("rvf_stop_hook_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_fork_experiment_marker_dry_run(tmp: Path) -> None:
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
    assert "fork-experiment triggered" in payload["systemMessage"]


def test_stop_hook_active_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke({"cwd": str(dirty), "stop_hook_active": True})
    assert_skip_reason(stdout, "stop_hook_active=true")


def test_env_suppression_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke(
        {"cwd": str(dirty), "stop_hook_active": False},
        extra_env={"CODEX_RVF_SUPPRESS_STOP_HOOK": "1"},
    )
    assert_skip_reason(stdout, "suppress")


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

    assert "desktop_control_unavailable=missing" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
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
    assert "visible fork failure" in payload["systemMessage"]
    assert "$review-validate-fix" not in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "desktop-control-unavailable-report"
    assert latest["report_reason"] == "visible fork failure"
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge_policy"] == "report"


def test_fork_experiment_missing_desktop_control_prepares_manual_not_continuation(
    tmp: Path,
) -> None:
    module = load_hook_module()
    state = tmp / "state"
    desktop_socket = tmp / "missing-control.sock"
    bridge_socket = tmp / "missing-bridge.sock"
    original_state_dir = os.environ.get("CODEX_RVF_STATE_DIR")
    original_bridge_policy = os.environ.pop("CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY", None)
    original_allow_bridge = os.environ.pop("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER", None)
    original_experiment_mode = os.environ.pop("CODEX_RVF_FORK_EXPERIMENT_MODE", None)
    original_desktop_socket = module.DEFAULT_APP_SERVER_CONTROL_SOCKET
    original_bridge_socket_path = module.bridge_socket_path
    original_ensure_bridge = module.ensure_bridge_app_server
    try:
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = desktop_socket
        module.bridge_socket_path = lambda: bridge_socket
        module.ensure_bridge_app_server = lambda: (_ for _ in ()).throw(
            AssertionError("bridge should not start for fork experiment by default")
        )
        payload = module.run_fork_experiment(
            {"session_id": "parent-thread", "cwd": str(tmp)},
            "RVF_FORK_EXPERIMENT: diagnose fork behavior",
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
        if original_experiment_mode is not None:
            os.environ["CODEX_RVF_FORK_EXPERIMENT_MODE"] = original_experiment_mode
        module.DEFAULT_APP_SERVER_CONTROL_SOCKET = original_desktop_socket
        module.bridge_socket_path = original_bridge_socket_path
        module.ensure_bridge_app_server = original_ensure_bridge

    assert "decision" not in payload
    assert payload["continue"] is True
    assert "fork-experiment prepared" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "manual-prepared"
    assert latest["desktop_control_unavailable_fallback"] == "manual"
    assert latest["socket_selection"]["desktop_control"]["reason"] == "missing"
    assert latest["socket_selection"]["bridge_policy"] == "report"
    assert latest["marker"] == "RVF_FORK_EXPERIMENT"
    assert latest["latest_user_message"] == "RVF_FORK_EXPERIMENT: diagnose fork behavior"


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
    assert "CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY=fail" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
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

    assert "forked in Codex app-server bridge" in payload["systemMessage"]
    assert "gui_visibility=unverified-bridge-only" in payload["systemMessage"]


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
    assert "disabled" in payload["systemMessage"]
    assert (state / "session-hook" / "session-disabled.json").exists()
    assert not (state / "latest.json").exists()

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
    assert not (state / "latest.json").exists()


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
    assert "disabled" in payload["systemMessage"]
    assert "session-status" in payload["systemMessage"]


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
    assert "enabled" in payload["systemMessage"]
    assert "session-status-suppressed" in payload["systemMessage"]


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
    assert "enabled" in payload["systemMessage"]
    assert not (state / "session-hook" / "session-reenabled.json").exists()
    assert not (state / "latest.json").exists()

    write_user_session(
        transcript,
        "session-reenabled",
        "普通停止现在应恢复触发。",
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
    assert "review-validate-fix-fork triggered" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
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
    assert not (state / "latest.json").exists()


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
    assert "review-validate-fix-fork triggered" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "dry-run"
    assert latest["mode"] == "dry-run"
    assert latest["app_server_requests"][0]["method"] == "thread/fork"
    assert latest["app_server_requests"][1]["method"] == "turn/start"
    assert "$review-validate-fix" in latest["prompt"]
    assert str(dirty) in latest["prompt"]


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
    assert "review-validate-fix-fork prepared" in payload["systemMessage"]
    assert "prompt=" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
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
    assert "review-validate-fix-fork triggered" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert "$review-validate-fix" in latest["prompt"]
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in latest["prompt"]
    assert str(dirty) in latest["prompt"]
    assert "RVF_STOP_HOOK: off" in latest["prompt"]
    assert "会话控制元数据" in latest["prompt"]
    assert "不要把它们当成用户分配的代码任务" in latest["prompt"]
    assert latest["suppress_child_stop_hook"] is False
    assert latest["model"] == "gpt-test"
    assert latest["reasoning_effort"] == "high"
    requests = latest["app_server_requests"]
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
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    requests = latest["app_server_requests"]
    assert latest["cwd"] == str(subdir.resolve())
    assert requests[0]["params"]["cwd"] == str(subdir.resolve())
    assert requests[1]["params"]["cwd"] == str(subdir.resolve())
    assert f"RVF_PARENT_CWD: {subdir.resolve()}" in latest["prompt"]
    assert f"RVF_TARGET_REPO: {dirty.resolve()}" in latest["prompt"]


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
    assert "当前 cwd 不在 git repo/worktree 内" in payload["systemMessage"]
    assert "提供要运行 review-validate-fix 的目标 repo 路径" in payload["systemMessage"]
    assert not (state / "latest.json").exists()


def test_stop_event_transcript_path_overrides_bad_env_thread_id(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    transcript = tmp / "session.jsonl"
    state = tmp / "state"
    write_user_session(
        transcript,
        "00000000-0000-0000-0000-000000000099",
        "normal parent prompt",
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
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["parent_thread_id"] == "00000000-0000-0000-0000-000000000099"
    assert latest["parent_thread_path"] == str(transcript.resolve())
    fork_params = latest["app_server_requests"][0]["params"]
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
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["parent_thread_id"] == "00000000-0000-0000-0000-000000000100"
    assert latest["parent_thread_path"] is None
    assert "path" not in latest["app_server_requests"][0]["params"]


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
    assert "$review-validate-fix" in payload["systemMessage"]
    assert str(dirty) in payload["systemMessage"]
    assert "Stop continuation prompt 已禁用" in payload["systemMessage"]
    assert "不会创建真正的新用户 prompt" in payload["systemMessage"]


def test_forked_rvf_session_gets_programmatic_handoff_advisory(tmp: Path) -> None:
    state = tmp / "state"
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
        "last_assistant_message": "完成。\n<handoff-context>\n...\n</handoff-context>",
    }
    payload = parse_json(invoke(event, state_dir=state)[0])
    assert "decision" not in payload
    assert "<handoff-context>" in payload["systemMessage"]
    assert "粘贴回原始 chat session" in payload["systemMessage"]
    assert "parent-session" in payload["systemMessage"]

    stdout, _ = invoke(event, state_dir=state)
    assert_skip_reason(stdout, "已是 review-validate-fix fork")


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
    assert not (state / "child-session.handoff-advised").exists()


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
    assert not (state / "child-session.handoff-advised").exists()


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
    assert not (state / "latest.json").exists()


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
    assert not (state / "latest.json").exists()


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
    assert "review-validate-fix-fork triggered" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "dry-run"
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in latest["prompt"]
    assert str(dirty) in latest["prompt"]


def test_missing_cwd_skips_and_requests_target_repo(tmp: Path) -> None:
    payload = parse_json(invoke({"stop_hook_active": False})[0])
    assert "decision" not in payload
    assert payload["continue"] is True
    assert "Stop event 未提供可检查的 cwd" in payload["systemMessage"]
    assert "提供要运行 review-validate-fix 的目标 repo 路径" in payload["systemMessage"]


def main() -> int:
    tests = [
        test_fork_experiment_marker_dry_run,
        test_stop_hook_active_skips,
        test_env_suppression_skips,
        test_session_hook_default_state_dir_is_skill_state_session_hook,
        test_session_hook_state_dir_respects_state_dir_override,
        test_socket_probe_reports_unavailable_reason,
        test_bridge_failure_preserves_desktop_probe,
        test_missing_desktop_control_reports_failure_not_bridge_or_continuation,
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
        test_forked_rvf_session_waits_for_handoff_before_advisory,
        test_forked_rvf_session_waits_when_handoff_message_missing,
        test_forked_rvf_marker_in_transcript_prevents_refork_after_later_user_message,
        test_forked_rvf_marker_scan_skips_incomplete_earlier_marker,
        test_incomplete_fork_marker_in_transcript_does_not_skip_dirty_repo,
        test_missing_cwd_skips_and_requests_target_repo,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            test(root / test.__name__)
    print("codex stop review-validate-fix hook tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
