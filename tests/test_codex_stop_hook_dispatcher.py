#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "codex_stop_hook_dispatcher.py"
)


def load_dispatcher_module():
    spec = importlib.util.spec_from_file_location("rvf_stop_hook_dispatcher_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=path)
    return path


def write_fake_dev_scripts(repo: Path, marker: Path, *, fail_sync: bool = False) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    contract_body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'sync-ran')!r}).write_text('sync\\n', encoding='utf-8')\n"
    )
    if fail_sync:
        contract_body += "sys.exit(7)\n"
    (scripts / "check_plugin_contracts.py").write_text(contract_body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'install-ran')!r}).write_text("
        "'install ' + ' '.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def write_env_check_dev_scripts(repo: Path, marker: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "if any(key.startswith('CODEX_RVF_') for key in os.environ):\n"
        "    sys.exit(9)\n"
        f"pathlib.Path({str(marker)!r}).write_text('clean\\n', encoding='utf-8')\n"
    )
    (scripts / "check_plugin_contracts.py").write_text(body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(body, encoding="utf-8")


def write_fake_installed_hook(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "raw = sys.stdin.read()\n"
        f"pathlib.Path({str(marker / 'hook-input.json')!r}).write_text(raw, encoding='utf-8')\n"
        "print(json.dumps({'continue': True, 'systemMessage': 'real hook ran'}))\n",
        encoding="utf-8",
    )


def write_fake_opener(path: Path, marker: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(sys.argv[-1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
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


def write_assistant_plan_transcript(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "<proposed_plan>\n# Plan\n</proposed_plan>",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_plan_then_normal_transcript(path: Path) -> Path:
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "<proposed_plan>\n# Plan\n</proposed_plan>",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "execute it"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "Implementation complete.",
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def write_failing_installed_hook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('boom', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )


def write_slow_installed_hook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )


def write_user_transcript(path: Path, repo: Path, session_id: str = "session") -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(repo)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "status only"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_apply_patch_transcript(path: Path, repo: Path, rel_path: str) -> Path:
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {rel_path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-with-edit", "cwd": str(repo)},
            }
        )
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


def invoke_result(
    event: dict[str, object],
    *,
    dev_repo: Path | None,
    hook: Path,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "CODEX_RVF_DEV_REPO",
        "CODEX_RVF_INSTALLED_STOP_HOOK",
        "CODEX_RVF_DEV_SYNC_STATE_DIR",
        "CODEX_RVF_LOG_ROOT",
        "CODEX_RVF_DEV_SYNC",
        "CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT",
        "CODEX_RVF_CORRELATION_ID",
        "CODEX_RVF_FORK_MODE",
        "CODEX_RVF_RUN_DIR",
        "CODEX_RVF_RUN_ID",
        "CODEX_RVF_VK_PROJECT_AUTO",
        "CODEX_RVF_VK_PROJECT_ID",
        "CODEX_RVF_VK_MANAGEMENT_MODE",
        "CODEX_RVF_SUPPRESS",
        "CODEX_RVF_SUPPRESS_STOP_HOOK",
        "CODEX_RVF_OPEN_HANDOFF",
        "CODEX_RVF_IDE_OPEN_CMD",
    ):
        env.pop(key, None)
    if dev_repo is not None:
        env["CODEX_RVF_DEV_REPO"] = str(dev_repo)
    env["CODEX_RVF_INSTALLED_STOP_HOOK"] = str(hook)
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )


def invoke(
    event: dict[str, object],
    *,
    dev_repo: Path | None,
    hook: Path,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> str:
    completed = invoke_result(
        event,
        dev_repo=dev_repo,
        hook=hook,
        state=state,
        extra_env=extra_env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def latest_summary(state: Path) -> dict[str, object]:
    pointer = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert set(pointer) >= {"run_id", "summary_path", "events_path", "status", "reason_code"}
    assert Path(str(pointer["events_path"])).exists()
    return json.loads(Path(str(pointer["summary_path"])).read_text(encoding="utf-8"))


def test_dev_repo_main_session_syncs_before_running_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_handoff_marker_opens_before_dev_sync_or_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    handoff = tmp_path / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)

    event = {
        "cwd": str(repo),
        "session_id": "child-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
    }
    stdout = invoke(
        event,
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_IDE_OPEN_CMD": str(opener)},
    )

    payload = json.loads(stdout)
    assert "reason=handoff_file_ready" in payload["systemMessage"]
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    assert opener_marker.read_text(encoding="utf-8") == str(handoff.resolve())
    summary = latest_summary(tmp_path / "state")
    assert summary["handoff_path"] == str(handoff.resolve())


def test_plan_operation_skips_before_dev_sync_or_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_assistant_plan_transcript(tmp_path / "session.jsonl")
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "session_id": "parent-session",
            "turn_id": "turn",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": "<proposed_plan>\n# Plan\n</proposed_plan>",
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=plan_operation" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "plan_operation"


def test_literal_plan_markers_in_completion_do_not_skip_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_assistant_message": (
            "Implementation complete. Documented literal markers "
            "<proposed_plan> and </proposed_plan> for regression coverage."
        ),
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_prior_plan_output_does_not_suppress_future_turn(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_plan_then_normal_transcript(tmp_path / "session.jsonl")

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn-after-plan",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "last_assistant_message": "Implementation complete.",
        "mode": "plan",
        "source": {"agent_mode": "plan"},
    }
    stdout = invoke(
        event,
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_session_hook_off_still_syncs_before_running_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_user_message": "RVF_STOP_HOOK: off",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_non_matching_repo_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    other = init_repo(tmp_path / "other")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {"cwd": str(other), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert (marker / "hook-input.json").exists()


def test_subagent_stop_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = tmp_path / "subagent.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()


def test_suppress_env_skips_before_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": "headless-child",
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_SUPPRESS_STOP_HOOK": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=suppressed" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_suppress_env_skips_handoff_marker_before_opening(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    opener_marker = tmp_path / "opened.txt"
    opener = write_fake_opener(tmp_path / "open_handoff.py", opener_marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": "headless-child",
            "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "CODEX_RVF_SUPPRESS_STOP_HOOK": "1",
            "CODEX_RVF_IDE_OPEN_CMD": str(opener),
        },
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=suppressed" in payload["systemMessage"]
    assert not opener_marker.exists()
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_sync_failure_skips_installed_hook_to_avoid_stale_fork(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker, fail_sync=True)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=sync_command_failed" in payload["systemMessage"]
    assert completed.stderr == ""
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "failed"
    assert summary["reason_code"] == "sync_command_failed"


def test_installed_hook_failure_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_failing_installed_hook(hook)

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_failed" in payload["systemMessage"]
    assert completed.stderr == ""


def test_missing_installed_hook_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "missing" / "codex_stop_review_validate_fix.py"

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_failed" in payload["systemMessage"]
    assert completed.stderr == ""


def test_installed_hook_timeout_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_slow_installed_hook(hook)

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_timeout" in payload["systemMessage"]
    assert completed.stderr == ""


def test_dev_repo_without_session_owned_dirty_skips_sync_and_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    transcript = write_user_transcript(tmp_path / "session.jsonl", repo)
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_session_hook_control_forwards_without_session_owned_dirty(tmp_path: Path) -> None:
    for action in ("off", "on", "status"):
        root = tmp_path / action
        repo = init_repo(root / "rvf")
        (repo / "background.txt").write_text("background\n", encoding="utf-8")
        transcript = write_user_transcript(root / "session.jsonl", repo)
        marker = root / "marker"
        marker.mkdir()
        write_fake_dev_scripts(repo, marker)
        hook = root / "installed" / "codex_stop_review_validate_fix.py"
        write_fake_installed_hook(hook, marker)
        event = {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": f"session-{action}",
            "transcript_path": str(transcript),
            "last_user_message": f"RVF_STOP_HOOK: {action}",
        }

        stdout = invoke(
            event,
            dev_repo=repo,
            hook=hook,
            state=root / "state",
        )

        payload = json.loads(stdout)
        assert payload["systemMessage"] == "real hook ran"
        assert not (marker / "sync-ran").exists()
        assert not (marker / "install-ran").exists()
        assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_session_manifest_failure_skips_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    transcript = tmp_path / "bad-session.jsonl"
    transcript.write_bytes(b"\xff")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=session_manifest_failed" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "session_manifest_failed"


def test_provided_missing_transcript_skips_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    missing_transcript = tmp_path / "missing-session.jsonl"
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(missing_transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=transcript_unavailable" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "transcript_unavailable"


def test_dev_repo_with_session_owned_dirty_syncs_and_runs_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    (repo / "owned.txt").write_text("new\n", encoding="utf-8")
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo, "owned.txt")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert (marker / "install-ran").exists()
    assert (marker / "hook-input.json").exists()


def test_coerce_text_handles_timeout_bytes() -> None:
    module = load_dispatcher_module()
    assert module.coerce_text(b"\xffstdout") == "�stdout"
    assert module.coerce_text(None) == ""


def test_sync_subprocesses_do_not_inherit_rvf_runtime_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "clean-env"
    hook_marker = tmp_path / "hook"
    hook_marker.mkdir()
    write_env_check_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, hook_marker)

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run", "CODEX_RVF_STATE_DIR": "/tmp/rvf-test"},
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert marker.exists()


def test_dev_sync_preserves_vibe_kanban_installer_args(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "CODEX_RVF_FORK_MODE": "vibe-kanban",
            "CODEX_RVF_VK_MANAGEMENT_MODE": "remote-project",
            "CODEX_RVF_VK_PROJECT_ID": "project-abc",
            "CODEX_RVF_VK_MCP_CMD": "env VK_SHARED_API_BASE=http://localhost:3000 npx -y vibe-kanban@0.1.44 --mcp",
            "CODEX_RVF_VK_START_CMD": "env VK_SHARED_API_BASE=http://localhost:3000 npx -y vibe-kanban@0.1.44",
            "CODEX_RVF_VK_BACKEND_URL": "http://127.0.0.1:50280",
            "CODEX_RVF_OPEN_HANDOFF": "0",
            "CODEX_RVF_IDE_OPEN_CMD": "code -r",
        },
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    install_args = (marker / "install-ran").read_text(encoding="utf-8")
    assert "--configure-stop-hook" in install_args
    assert "--fork-mode vibe-kanban" in install_args
    assert "--vibe-kanban-management-mode remote-project" in install_args
    assert "--vibe-kanban-project-id project-abc" in install_args
    assert "--vibe-kanban-mcp-cmd" in install_args
    assert "--vibe-kanban-start-cmd" in install_args
    assert "--vibe-kanban-backend-url http://127.0.0.1:50280" in install_args
    assert "--no-open-handoff" in install_args
    assert "--ide-open-cmd code -r" in install_args


def test_dev_sync_prefers_hooks_json_over_stale_cached_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    home = tmp_path / "home"
    hooks_path = home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "CODEX_RVF_MODE=fork "
                                        "CODEX_RVF_FORK_MODE=vibe-kanban "
                                        "CODEX_RVF_VK_MANAGEMENT_MODE=remote-project "
                                        "CODEX_RVF_VK_PROJECT_AUTO=1 "
                                        "CODEX_RVF_VK_BACKEND_URL=http://127.0.0.1:50280 "
                                        f"python3 {SCRIPT}"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "HOME": str(home),
            "CODEX_RVF_FORK_MODE": "gui",
            "CODEX_RVF_VK_PROJECT_ID": "stale-project",
        },
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    install_args = (marker / "install-ran").read_text(encoding="utf-8")
    assert "--configure-stop-hook" in install_args
    assert "--fork-mode vibe-kanban" in install_args
    assert "--vibe-kanban-management-mode remote-project" in install_args
    assert "--vibe-kanban-project-id" not in install_args
    assert "--vibe-kanban-backend-url http://127.0.0.1:50280" in install_args
    assert "stale-project" not in install_args


def test_installed_hook_receives_hooks_json_mode_over_stale_cached_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(marker / 'hook-env.json')!r}).write_text(json.dumps({{'mode': os.environ.get('CODEX_RVF_FORK_MODE'), 'management_mode': os.environ.get('CODEX_RVF_VK_MANAGEMENT_MODE'), 'auto': os.environ.get('CODEX_RVF_VK_PROJECT_AUTO'), 'project_id': os.environ.get('CODEX_RVF_VK_PROJECT_ID'), 'backend': os.environ.get('CODEX_RVF_VK_BACKEND_URL')}}), encoding='utf-8')\n"
        "print(json.dumps({'continue': True, 'systemMessage': 'real hook ran'}))\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    hooks_path = home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "CODEX_RVF_MODE=fork "
                                        "CODEX_RVF_FORK_MODE=vibe-kanban "
                                        "CODEX_RVF_VK_MANAGEMENT_MODE=remote-project "
                                        "CODEX_RVF_VK_PROJECT_AUTO=1 "
                                        "CODEX_RVF_VK_BACKEND_URL=http://127.0.0.1:50280 "
                                        f"python3 {SCRIPT}"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "HOME": str(home),
            "CODEX_RVF_FORK_MODE": "gui",
            "CODEX_RVF_VK_PROJECT_ID": "stale-project",
        },
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    hook_env = json.loads((marker / "hook-env.json").read_text(encoding="utf-8"))
    assert hook_env == {
        "mode": "vibe-kanban",
        "management_mode": "remote-project",
        "auto": "1",
        "project_id": None,
        "backend": "http://127.0.0.1:50280",
    }


def main() -> int:
    tests = [
        test_dev_repo_main_session_syncs_before_running_installed_hook,
        test_handoff_marker_opens_before_dev_sync_or_installed_hook,
        test_plan_operation_skips_before_dev_sync_or_installed_hook,
        test_literal_plan_markers_in_completion_do_not_skip_hook,
        test_prior_plan_output_does_not_suppress_future_turn,
        test_session_hook_off_still_syncs_before_running_installed_hook,
        test_non_matching_repo_runs_installed_hook_without_sync,
        test_subagent_stop_runs_installed_hook_without_sync,
        test_suppress_env_skips_before_sync_and_installed_hook,
        test_suppress_env_skips_handoff_marker_before_opening,
        test_sync_failure_skips_installed_hook_to_avoid_stale_fork,
        test_installed_hook_failure_blocks_instead_of_continuing,
        test_missing_installed_hook_blocks_instead_of_continuing,
        test_installed_hook_timeout_blocks_instead_of_continuing,
        test_dev_repo_without_session_owned_dirty_skips_sync_and_hook,
        test_session_hook_control_forwards_without_session_owned_dirty,
        test_session_manifest_failure_skips_sync_and_installed_hook,
        test_provided_missing_transcript_skips_sync_and_installed_hook,
        test_dev_repo_with_session_owned_dirty_syncs_and_runs_hook,
        test_coerce_text_handles_timeout_bytes,
        test_sync_subprocesses_do_not_inherit_rvf_runtime_env,
        test_dev_sync_preserves_vibe_kanban_installer_args,
        test_dev_sync_prefers_hooks_json_over_stale_cached_env,
        test_installed_hook_receives_hooks_json_mode_over_stale_cached_env,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            if test is test_coerce_text_handles_timeout_bytes:
                test()
            else:
                test(root / test.__name__)
    print("codex stop hook dispatcher tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
