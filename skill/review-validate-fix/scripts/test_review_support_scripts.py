#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"


def run(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=path)
    run(["git", "config", "user.name", "RVF Test"], cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=path)
    run(["git", "commit", "-q", "-m", "base"], cwd=path)
    (path / "tracked.txt").write_text("base\nchange\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")
    return path


def write_alternative_reviewer_config(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    output_format: str | None = "text",
) -> Path:
    payload = {
        "enabled": True,
        "label": "alternative-reviewer:test",
        "command": command,
        "allow_repo_cwd": True,
        "idle_timeout_seconds": idle_timeout_seconds,
        "activity_check_interval_seconds": activity_check_interval_seconds,
        "env_unset": [],
    }
    if output_format is not None:
        payload["output_format"] = output_format
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_check_review_output_lock_request() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="RVF_LOCK_REQUEST name=npm-test command=npm test reason=shared-cache\n",
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "lock_request"
    assert payload["lock_request_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name=n command=x reason=y\nNO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0


def test_build_packet_metadata_and_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
            "--primary-file",
            "tracked.txt",
            "--background-file",
            "new.txt",
        ]
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Review Scope" in packet_text
    assert "## Session Context" in packet_text
    assert payload["session_context_provided"] is True
    assert payload["session_context_bytes"] > 0
    assert payload["primary_files"] == ["tracked.txt"]
    assert payload["background_files"] == ["new.txt"]
    assert payload["packet_bytes"] == len(packet_text.encode("utf-8"))


def test_build_packet_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_prepare_review_run_and_command_lock(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared review run\n",
        encoding="utf-8",
    )
    result = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp / "runs"),
            "--primary-file",
            "tracked.txt",
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["review_packet"]).exists()
    assert Path(payload["review_packet_metadata"]).exists()
    assert Path(payload["before_workspace_snapshot"]).exists()
    assert payload["session_context"] == str(context.resolve())
    assert payload["session_context_provided"] is True

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contract-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ]
    )
    assert "locked" in locked.stdout


def test_prepare_review_run_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--base-dir",
            str(tmp / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_alternative_reviewer_idle_timeout_flag(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr


def test_alternative_reviewer_activity_refreshes_idle_timeout(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time; sys.stdin.read(); "
                "[print(f'tick-{i}', flush=True) or time.sleep(0.08) for i in range(4)]; "
                "print('NO_ISSUES', flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NO_ISSUES" in completed.stdout
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_claude_stream_json_extracts_result(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_legacy_claude_config_gets_stream_json(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--include-hook-events" in argv
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert "--disable-slash-commands" in argv


def test_alternative_reviewer_respects_explicit_claude_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p", "--output-format", "text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "text"]


def test_alternative_reviewer_respects_explicit_claude_equals_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        ["claude", "-p", "--output-format=text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format=text"]


def test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "stream_wrapper"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-u", str(shim), "--native-stream"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    assert json.loads(sink.read_text(encoding="utf-8")) == ["--native-stream"]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        test_check_review_output_lock_request()
        test_build_packet_metadata_and_scope(root / "packet")
        test_build_packet_requires_session_context(root / "packet-requires-context")
        test_prepare_review_run_and_command_lock(root / "prepare")
        test_prepare_review_run_requires_session_context(root / "prepare-requires-context")
        test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout")
        test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity")
        test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json")
        test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config")
        test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config")
        test_alternative_reviewer_respects_explicit_claude_equals_text_output(
            root / "alternative-equals-text-config"
        )
        test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(root / "alternative-wrapper")
    print("review support script tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
