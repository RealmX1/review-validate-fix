#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("codex_stop_review_validate_fix.py")


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
    env = None
    if config is not None or extra_env is not None or state_dir is not None:
        env = os.environ.copy()
    if config is not None:
        env["CODEX_RVF_CONFIG"] = str(config)
    if state_dir is not None:
        env["CODEX_RVF_STATE_DIR"] = str(state_dir)
    if env is not None and extra_env is not None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(path.parent),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": message,
            },
        },
    ]
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
    assert stdout == ""


def test_env_suppression_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke(
        {"cwd": str(dirty), "stop_hook_active": False},
        extra_env={"CODEX_RVF_SUPPRESS_STOP_HOOK": "1"},
    )
    assert stdout == ""


def test_subagent_source_skips(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    stdout, _ = invoke(
        {
            "cwd": str(dirty),
            "stop_hook_active": False,
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
        }
    )
    assert stdout == ""


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
    assert stdout == ""


def test_clean_repo_skips(tmp: Path) -> None:
    clean = init_repo(tmp / "clean", dirty=False)
    stdout, _ = invoke({"cwd": str(clean), "stop_hook_active": False})
    assert stdout == ""


def test_dirty_repo_continues_in_gui_by_default(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000002",
                "stop_hook_active": False,
            }
        )[0]
    )
    assert payload["decision"] == "block"
    assert "$review-validate-fix" in payload["reason"]
    assert str(dirty) in payload["reason"]


def test_dirty_repo_manual_mode_only_prepares_launcher(tmp: Path) -> None:
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
    assert "launcher=" in payload["systemMessage"]
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "manual-prepared"
    assert Path(latest["launcher_path"]).exists()


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
    assert latest["suppress_child_stop_hook"] is False
    assert latest["model"] == "gpt-test"
    assert latest["reasoning_effort"] == "high"
    launcher = Path(latest["launcher_path"]).read_text(encoding="utf-8")
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in launcher
    assert "-m gpt-test" in launcher
    assert 'model_reasoning_effort="high"' in launcher


def test_dirty_repo_continuation_mode(tmp: Path) -> None:
    dirty = init_repo(tmp / "dirty", dirty=True)
    payload = parse_json(
        invoke(
            {
                "cwd": str(dirty),
                "session_id": "00000000-0000-0000-0000-000000000004",
                "stop_hook_active": False,
            },
        )[0]
    )
    assert payload["decision"] == "block"
    assert "$review-validate-fix" in payload["reason"]
    assert str(dirty) in payload["reason"]


def test_no_git_unique_dirty_trusted_repo_forks_by_default(tmp: Path) -> None:
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
                "session_id": "00000000-0000-0000-0000-000000000005",
                "stop_hook_active": False,
            },
            config=config,
            state_dir=state,
        )[0]
    )
    assert payload["decision"] == "block"
    assert "$review-validate-fix" in payload["reason"]
    assert str(dirty) in payload["reason"]


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
    assert stdout == ""


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
    assert stdout == ""
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
    assert stdout == ""
    assert not (state / "child-session.handoff-advised").exists()


def test_no_git_multiple_dirty_trusted_repos_skips(tmp: Path) -> None:
    plain = tmp / "plain"
    plain.mkdir(parents=True)
    first = init_repo(tmp / "first", dirty=True)
    second = init_repo(tmp / "second", dirty=True)
    config = tmp / "config.toml"
    write_config(config, [first, second])

    payload = parse_json(
        invoke({"cwd": str(plain), "stop_hook_active": False}, config=config)[0]
    )
    assert "decision" not in payload
    assert "多个 dirty trusted repo" in payload["systemMessage"]


def main() -> int:
    tests = [
        test_fork_experiment_marker_dry_run,
        test_stop_hook_active_skips,
        test_env_suppression_skips,
        test_subagent_source_skips,
        test_subagent_session_meta_skips,
        test_clean_repo_skips,
        test_dirty_repo_continues_in_gui_by_default,
        test_dirty_repo_manual_mode_only_prepares_launcher,
        test_dirty_repo_fork_dry_run,
        test_dirty_repo_continuation_mode,
        test_no_git_unique_dirty_trusted_repo_forks_by_default,
        test_forked_rvf_session_gets_programmatic_handoff_advisory,
        test_forked_rvf_session_waits_for_handoff_before_advisory,
        test_forked_rvf_session_waits_when_handoff_message_missing,
        test_no_git_multiple_dirty_trusted_repos_skips,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            test(root / test.__name__)
    print("codex stop review-validate-fix hook tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
