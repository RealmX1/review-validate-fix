#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "config" / "alternative-reviewer.json"
DEFAULT_PROMPT = SKILL_DIR / "references" / "review-prompt.md"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_ACTIVITY_CHECK_INTERVAL_SECONDS = 300.0
EXTERNAL_REVIEWER_TIMEOUT_FLAG = "RVF_EXTERNAL_REVIEWER_TIMEOUT"
EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE = 124
OUTPUT_FORMAT_TEXT = "text"
OUTPUT_FORMAT_CLAUDE_STREAM_JSON = "claude_stream_json"
SUPPORTED_OUTPUT_FORMATS = {OUTPUT_FORMAT_TEXT, OUTPUT_FORMAT_CLAUDE_STREAM_JSON}


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("config root must be a JSON object")
    return config


def string_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string array")
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def positive_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive number")
    return parsed


def check_command(command: list[str]) -> str | None:
    return shutil.which(command[0])


def is_claude_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == "claude" and any(
        item in {"-p", "--print"} for item in command
    )


def claude_output_format_arg(command: list[str]) -> str | None:
    for index, item in enumerate(command):
        if item.startswith("--output-format="):
            return item.split("=", 1)[1]
        if item == "--output-format" and index + 1 < len(command):
            return command[index + 1]
    return None


def ensure_claude_stream_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    equals_index = next(
        (
            index
            for index, item in enumerate(patched)
            if item.startswith("--output-format=")
        ),
        None,
    )
    if equals_index is not None:
        patched[equals_index] = "--output-format=stream-json"
    elif "--output-format" in patched:
        index = patched.index("--output-format")
        if index + 1 < len(patched):
            patched[index + 1] = "stream-json"
        else:
            patched.append("stream-json")
    else:
        patched.extend(["--output-format", "stream-json"])
    if "--include-hook-events" not in patched:
        patched.append("--include-hook-events")
    if "--include-partial-messages" not in patched:
        patched.append("--include-partial-messages")
    return patched


def check_repo(repo: Path) -> None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"not a git repo: {repo}")


def build_prompt(prompt_file: Path, session_context: Path | None, review_packet: Path | None, repo: Path | None) -> str:
    parts = []
    if session_context is not None:
        parts.append(session_context.read_text(encoding="utf-8").strip())
    if repo is not None:
        parts.append(
            "\n".join(
                [
                    "## Runtime helpers",
                    f"- RVF command lock wrapper: `{COMMAND_LOCK}`",
                    f"- Example: `python3 {COMMAND_LOCK} --repo {repo} --name <stable-lock-name> -- <command ...>`",
                    "- If a potentially conflicting command needs coordination and no lock is available, output `RVF_LOCK_REQUEST name=<stable-lock-name> command=<command> reason=<why>` as the only response so the main agent can provide a locked retry.",
                ]
            )
        )
    parts.append(prompt_file.read_text(encoding="utf-8").strip())
    if review_packet is not None:
        parts.append(review_packet.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def scrub_env(env_unset: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    for name in env_unset:
        env.pop(name, None)
    env["RVF_SKILL_DIR"] = str(SKILL_DIR)
    env["RVF_COMMAND_LOCK"] = str(COMMAND_LOCK)
    return env


def run_health(command: list[str], env: dict[str, str], timeout: int) -> int:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if completed.returncode != 0:
        return fail(completed.stderr.strip() or completed.stdout.strip() or "health command failed")
    print(completed.stdout.strip())
    return 0


def _payload_length(payload: bytes | str | None) -> int:
    if payload is None:
        return 0
    return len(payload)


def run_with_activity_timeout(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str],
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """运行外部 reviewer，并按 stdout/stderr 可观测活动刷新空闲超时。

    这里刻意不把未来的 reviewer-owned subagent 能力写进 prompt：后续如果允许
    review agent 自己再派生一层子代理，应在调度层显式建模后再开放。
    """

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        env=env,
    )
    last_activity_at = time.monotonic()
    last_stdout_len = 0
    last_stderr_len = 0
    pending_input: str | None = input_text

    while True:
        now = time.monotonic()
        idle_for = now - last_activity_at
        remaining_idle = max(0.0, idle_timeout_seconds - idle_for)
        wait_for = max(0.01, min(activity_check_interval_seconds, remaining_idle))

        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=wait_for)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            pending_input = None
            now = time.monotonic()
            stdout_len = _payload_length(exc.stdout)
            stderr_len = _payload_length(exc.stderr)
            if stdout_len > last_stdout_len or stderr_len > last_stderr_len:
                last_activity_at = now
                last_stdout_len = stdout_len
                last_stderr_len = stderr_len
                continue

            if now - last_activity_at < idle_timeout_seconds:
                continue

            process.kill()
            stdout, stderr = process.communicate()
            timeout_line = (
                f"{EXTERNAL_REVIEWER_TIMEOUT_FLAG} "
                f"idle_timeout_seconds={idle_timeout_seconds:g} "
                f"activity_check_interval_seconds={activity_check_interval_seconds:g} "
                "reason=no_observable_activity"
            )
            stderr = (stderr or "").rstrip()
            stderr = f"{stderr}\n{timeout_line}" if stderr else timeout_line
            return subprocess.CompletedProcess(
                command,
                EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE,
                stdout,
                stderr,
            )


def extract_claude_stream_result(output: str) -> str:
    """从 Claude Code stream-json stdout 中提取最终 result 文本。"""

    result: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "result" and isinstance(payload.get("result"), str):
            result = payload["result"]
    if result is not None:
        return result.strip()
    return output.strip()


def normalize_review_output(output: str) -> str:
    return output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run configured review-validate-fix alternative reviewer.")
    parser.add_argument("--repo", help="Target git repository.")
    parser.add_argument("--session-context", help="Optional file containing the Session context block.")
    parser.add_argument("--review-packet", help="Self-contained packet generated by build_review_packet.py.")
    parser.add_argument("--prompt-file", default=str(DEFAULT_PROMPT), help="Review prompt file.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Alternative reviewer config JSON.")
    parser.add_argument("--output", help="Optional file to write reviewer stdout.")
    parser.add_argument("--check", action="store_true", help="Validate config and command availability only.")
    parser.add_argument("--preflight", action="store_true", help="Validate config, command availability, and configured health command when present.")
    parser.add_argument("--health", action="store_true", help="Run the configured health command.")
    parser.add_argument("--print-label", action="store_true", help="Print configured provenance label.")
    parser.add_argument("--dry-run", action="store_true", help="Print command and prompt length without invoking reviewer.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        return fail(f"缺少 alternative reviewer 配置: {config_path}", 2)

    try:
        config = load_config(config_path)
        if config.get("enabled") is not True:
            return fail("alternative reviewer 未启用", 2)
        label = config.get("label")
        if not isinstance(label, str) or not label.startswith("alternative-reviewer:"):
            raise ValueError("label must start with alternative-reviewer:")
        command = string_list(config, "command")
        allow_repo_cwd = config.get("allow_repo_cwd", False)
        if not isinstance(allow_repo_cwd, bool):
            raise ValueError("allow_repo_cwd must be a boolean")
        env_unset = config.get("env_unset", [])
        if not isinstance(env_unset, list) or not all(isinstance(item, str) for item in env_unset):
            raise ValueError("env_unset must be a string array")
        if "idle_timeout_seconds" in config:
            idle_timeout = positive_float(config, "idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
        elif "timeout_seconds" in config:
            idle_timeout = positive_float(config, "timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
        else:
            idle_timeout = DEFAULT_IDLE_TIMEOUT_SECONDS
        activity_check_interval = positive_float(
            config,
            "activity_check_interval_seconds",
            DEFAULT_ACTIVITY_CHECK_INTERVAL_SECONDS,
        )
        health_timeout = int(positive_float(config, "health_timeout_seconds", 30.0))
        output_format = config.get("output_format")
        if output_format is None and is_claude_print_command(command):
            cli_output_format = claude_output_format_arg(command)
            if cli_output_format in {None, "stream-json"}:
                output_format = OUTPUT_FORMAT_CLAUDE_STREAM_JSON
                command = ensure_claude_stream_json_command(command)
            else:
                output_format = OUTPUT_FORMAT_TEXT
        elif output_format is None:
            output_format = OUTPUT_FORMAT_TEXT
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}"
            )
        if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON and is_claude_print_command(command):
            command = ensure_claude_stream_json_command(command)
    except Exception as exc:
        return fail(f"alternative reviewer 配置无效: {exc}", 2)

    if args.print_label:
        print(label)
        return 0

    command_path = check_command(command)
    if command_path is None:
        return fail(f"找不到 alternative reviewer 命令: {command[0]}", 2)

    env = scrub_env(env_unset)

    if args.check:
        print(f"OK {label} {command_path}")
        return 0

    if args.preflight:
        print(f"OK {label} {command_path}")
        health_command = config.get("health_command")
        if health_command is None:
            print("health command not configured")
            return 0
        return run_health(string_list(config, "health_command"), env, health_timeout)

    if args.health:
        health_command = string_list(config, "health_command")
        return run_health(health_command, env, health_timeout)

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
    review_packet = Path(args.review_packet).expanduser().resolve() if args.review_packet else None

    try:
        if repo is None and review_packet is None:
            raise ValueError("缺少 --repo 或 --review-packet")
        if repo is not None:
            check_repo(repo)
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        if review_packet is not None and not review_packet.exists():
            raise ValueError(f"review packet file not found: {review_packet}")
        prompt = build_prompt(prompt_file, session_context, review_packet, repo)
    except Exception as exc:
        return fail(str(exc), 2)

    if repo is not None:
        env["RVF_REPO"] = str(repo)
    if review_packet is not None:
        env["RVF_REVIEW_PACKET"] = str(review_packet)

    if args.dry_run:
        cwd = str(repo) if repo is not None and allow_repo_cwd else str(review_packet.parent if review_packet is not None else SKILL_DIR)
        print(json.dumps({"label": label, "command": command, "cwd": cwd, "prompt_chars": len(prompt)}, ensure_ascii=False))
        return 0

    cwd = repo if repo is not None and allow_repo_cwd else (review_packet.parent if review_packet is not None else SKILL_DIR)

    completed = run_with_activity_timeout(
        command,
        input_text=prompt,
        cwd=cwd,
        env=env,
        idle_timeout_seconds=idle_timeout,
        activity_check_interval_seconds=activity_check_interval,
    )

    stdout = completed.stdout.strip()
    if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON:
        stdout = extract_claude_stream_result(stdout)
    stdout = normalize_review_output(stdout)
    stderr = completed.stderr.strip()
    timed_out = (
        completed.returncode == EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE
        and EXTERNAL_REVIEWER_TIMEOUT_FLAG in stderr
    )
    if args.output:
        output_text = f"{EXTERNAL_REVIEWER_TIMEOUT_FLAG}\n" if timed_out else stdout + ("\n" if stdout else "")
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(stdout)

    if completed.returncode != 0:
        if stderr:
            print(stderr, file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
