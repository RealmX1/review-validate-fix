#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_tracker
from rvf_logging import start_run


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "config" / "alternative-reviewer.json"
DEFAULT_PROMPT = SKILL_DIR / "prompts" / "reviewer.md"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
WRITE_REVIEW_RESULT = SKILL_DIR / "scripts" / "write_review_result.py"
CHECK_REVIEW_RESULT = SKILL_DIR / "scripts" / "check_review_result.py"
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_ACTIVITY_CHECK_INTERVAL_SECONDS = 300.0
DEFAULT_ACTIVITY_PROBE_TIMEOUT_SECONDS = 10.0
DEFAULT_ACTIVITY_PROBE_FAILURE_THRESHOLD = 3
DEFAULT_MAX_RUNTIME_SECONDS: float | None = None
DEFAULT_LEASE_HEARTBEAT_SECONDS = 60.0
LEASE_HEARTBEAT_ENV = "CODEX_RVF_LEASE_HEARTBEAT_SECONDS"
EXTERNAL_REVIEWER_TIMEOUT_FLAG = "RVF_EXTERNAL_REVIEWER_TIMEOUT"
EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE = 124
CODEX_BACKEND_CHALLENGE_FLAG = "RVF_CODEX_BACKEND_CHALLENGE"
OUTPUT_FORMAT_TEXT = "text"
OUTPUT_FORMAT_CLAUDE_STREAM_JSON = "claude_stream_json"
OUTPUT_FORMAT_CODEX_JSON = "codex_json"
SUPPRESS_STOP_HOOK_ENV = "CODEX_RVF_SUPPRESS_STOP_HOOK"
SUPPORTED_OUTPUT_FORMATS = {
    OUTPUT_FORMAT_TEXT,
    OUTPUT_FORMAT_CLAUDE_STREAM_JSON,
    OUTPUT_FORMAT_CODEX_JSON,
}
CHILD_RVF_ENV_KEYS = {
    "RVF_RUN_DIR",
    "RVF_ARTIFACTS_DIR",
    "RVF_INPUTS_DIR",
    "RVF_REPO",
    "RVF_SCOPE_CONTRACT",
    "RVF_SCOPE_OF_WORK",
    "RVF_SESSION_CONTEXT",
    "RVF_SESSION_MANIFEST",
    "RVF_REVIEW_PACKET",
    "RVF_REVIEW_PACKET_METADATA",
    "RVF_REVIEW_RESULT",
    "RVF_REVIEWER_ID",
}
_ACTIVE_REVIEWER_PROCESS_LOCK = threading.Lock()
_ACTIVE_REVIEWER_PROCESS: subprocess.Popen[str] | None = None


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


def optional_string_list(config: dict[str, Any], key: str) -> list[str] | None:
    if key not in config:
        return None
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string array or null")
    if not value:
        raise ValueError(f"{key} must not be empty when configured")
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


def positive_int(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def optional_positive_float(config: dict[str, Any], key: str, default: float | None) -> float | None:
    if key not in config:
        return default
    value = config.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive number or null") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive number or null")
    return parsed


def check_command(command: list[str]) -> str | None:
    return shutil.which(command[0])


def is_claude_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == "claude" and any(
        item in {"-p", "--print"} for item in command
    )


def codex_subcommand_index(command: list[str]) -> int | None:
    if not command or Path(command[0]).name != "codex":
        return None

    options_with_values = {
        "-a",
        "--add-dir",
        "--ask-for-approval",
        "-c",
        "-C",
        "--cd",
        "--config",
        "--disable",
        "--enable",
        "-i",
        "--image",
        "--local-provider",
        "-m",
        "--model",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "-s",
        "--sandbox",
    }
    index = 1
    while index < len(command):
        item = command[index]
        if item in {"exec", "e"}:
            return index
        if item == "--":
            return None
        if item in options_with_values:
            index += 2
            continue
        if item.startswith("--") and "=" in item:
            index += 1
            continue
        if item.startswith("-"):
            index += 1
            continue
        return None
    return None


def is_codex_exec_command(command: list[str]) -> bool:
    return codex_subcommand_index(command) is not None


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
    if "--verbose" not in patched:
        patched.append("--verbose")
    if "--disable-slash-commands" not in patched:
        patched.append("--disable-slash-commands")
    return patched


def codex_json_enabled(command: list[str]) -> bool:
    return "--json" in command


def codex_hooks_disabled(command: list[str]) -> bool:
    for index, item in enumerate(command):
        if item == "--disable" and index + 1 < len(command) and command[index + 1] == "hooks":
            return True
        if item == "--disable=hooks":
            return True
        if item == "-c" and index + 1 < len(command) and command[index + 1] == "features.hooks=false":
            return True
        if item == "-c=features.hooks=false":
            return True
    return False


def ensure_codex_hooks_disabled_command(command: list[str]) -> list[str]:
    exec_index = codex_subcommand_index(command)
    if exec_index is None or codex_hooks_disabled(command):
        return list(command)
    patched = list(command)
    patched[exec_index:exec_index] = ["--disable", "hooks"]
    return patched


def ensure_codex_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    if "--json" not in patched:
        patched.append("--json")
    if "-" not in patched:
        patched.append("-")
    return patched


def ensure_codex_exec_add_dir(command: list[str], directory: Path) -> list[str]:
    exec_index = codex_subcommand_index(command)
    if exec_index is None:
        return command

    directory_arg = str(directory)
    patched = list(command)
    index = exec_index + 1
    while index < len(patched):
        item = patched[index]
        if item == "--add-dir" and index + 1 < len(patched):
            if Path(patched[index + 1]).expanduser() == directory:
                return patched
            index += 2
            continue
        if item.startswith("--add-dir="):
            if Path(item.split("=", 1)[1]).expanduser() == directory:
                return patched
            index += 1
            continue
        index += 1

    insert_at = len(patched)
    for index in range(exec_index + 1, len(patched)):
        if patched[index] == "-":
            insert_at = index
            break
    patched[insert_at:insert_at] = ["--add-dir", directory_arg]
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


def safe_artifact_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    return token.strip("-")[:80] or "reviewer"


def reviewer_id_from_label(label: str) -> str:
    if label.startswith("alternative-reviewer:"):
        label = label.split(":", 1)[1]
    return safe_artifact_token(label)


def unique_child_path(directory: Path, name: str) -> Path:
    path = directory / safe_artifact_token(name)
    if not path.exists():
        return path
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    for index in range(2, 10000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}.{os.urandom(4).hex()}{suffix}"


def write_child_artifact(
    directory: Path,
    name: str,
    content_or_bytes: bytes | str | dict[str, Any] | list[Any],
    *,
    unique: bool = False,
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = unique_child_path(directory, name) if unique else directory / safe_artifact_token(name)
    if isinstance(content_or_bytes, bytes):
        path.write_bytes(content_or_bytes)
    elif isinstance(content_or_bytes, str):
        path.write_text(content_or_bytes, encoding="utf-8")
    else:
        path.write_text(json.dumps(content_or_bytes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def infer_scope_contract(review_packet: Path | None) -> Path | None:
    if review_packet is not None:
        candidates = [
            review_packet.parent / "scope.contract.json",
            review_packet.parent / "inputs" / "scope.contract.json",
            review_packet.parent.parent / "inputs" / "scope.contract.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
    env_value = os.environ.get("RVF_SCOPE_CONTRACT")
    if env_value and env_value.strip():
        candidate = Path(env_value).expanduser().resolve()
        if candidate.exists():
            return candidate
    return None


def load_scope_contract(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def tracker_scope_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    session_manifest_path = contract.get("session_manifest_path")
    if not isinstance(session_manifest_path, str) or not session_manifest_path:
        return {}
    try:
        manifest = json.loads(Path(session_manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    tracker = manifest.get("tracker")
    if not isinstance(tracker, dict):
        return {}
    scope = tracker.get("tracker_scope")
    return scope if isinstance(scope, dict) else {}


def lease_heartbeat_seconds() -> float:
    raw = os.environ.get(LEASE_HEARTBEAT_ENV, "").strip()
    if not raw:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS
    return value if value > 0 else DEFAULT_LEASE_HEARTBEAT_SECONDS


class TrackerLeaseRuntime:
    def __init__(
        self,
        *,
        repo: Path | None,
        scope_contract: dict[str, Any],
        reviewer_id: str,
        run_id: str,
    ) -> None:
        self.repo = repo
        self.scope_contract = scope_contract
        self.reviewer_id = reviewer_id
        self.run_id = run_id
        self.lease_id: str | None = None
        self._owns_lease = False
        self._participant_joined = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_handlers: dict[int, Any] = {}

    def acquire(self) -> None:
        if self.repo is None:
            return
        unit_ids = self.scope_contract.get("primary_units")
        if not isinstance(unit_ids, list) or not all(isinstance(item, str) and item for item in unit_ids):
            return
        if not unit_ids:
            return
        existing = self.scope_contract.get("tracker_lease_id")
        if isinstance(existing, str) and existing:
            self.lease_id = existing
            result = diff_tracker.lease_participant_join(
                repo=self.repo,
                lease_id=existing,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                owns_lease=False,
                log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
                if os.environ.get("CODEX_RVF_LOG_ROOT")
                else None,
            )
            if not result.get("joined"):
                raise RuntimeError(f"tracker lease participant join failed: {result.get('reason')}")
            self._participant_joined = True
            return
        tracker_scope = tracker_scope_from_contract(self.scope_contract)
        session_id = tracker_scope.get("source_session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = os.environ.get("CODEX_SESSION_ID") or "alternative-reviewer"
        result = diff_tracker.lease_acquire(
            repo=self.repo,
            session_id=session_id,
            run_id=str(self.scope_contract.get("run_id") or self.run_id),
            reviewer_id=self.reviewer_id,
            unit_ids=list(dict.fromkeys(unit_ids)),
            log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None,
        )
        if not result.get("acquired"):
            raise RuntimeError(f"tracker lease acquire failed: {result.get('reason')}")
        lease_id = result.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise RuntimeError("tracker lease acquire returned no lease_id")
        self.lease_id = lease_id
        self._owns_lease = True
        joined = diff_tracker.lease_participant_join(
            repo=self.repo,
            lease_id=lease_id,
            reviewer_id=self.reviewer_id,
            run_id=self.run_id,
            owns_lease=True,
            log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None,
        )
        if not joined.get("joined"):
            raise RuntimeError(f"tracker lease participant join failed: {joined.get('reason')}")
        self._participant_joined = True

    def start(self) -> None:
        if self.repo is None or self.lease_id is None:
            return
        self._install_signal_handlers()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._restore_signal_handlers()

    def release(self, reason: str) -> None:
        if self.repo is None or self.lease_id is None:
            return
        log_root_override = (
            Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None
        )
        active_participant_count = 0
        if self._participant_joined:
            result = diff_tracker.lease_participant_finish(
                repo=self.repo,
                lease_id=self.lease_id,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                reason=reason,
                log_root_override=log_root_override,
            )
            active_participant_count = int(result.get("active_participant_count") or 0)
            owning_participant_count = int(result.get("owning_participant_count") or 0)
        else:
            owning_participant_count = 1 if self._owns_lease else 0
        if self._owns_lease and owning_participant_count > 0 and active_participant_count == 0:
            diff_tracker.lease_release(
                repo=self.repo,
                lease_id=self.lease_id,
                reason=reason,
                log_root_override=log_root_override,
            )
        self.lease_id = None
        self._owns_lease = False
        self._participant_joined = False

    def _heartbeat_loop(self) -> None:
        assert self.repo is not None
        assert self.lease_id is not None
        interval = lease_heartbeat_seconds()
        while not self._stop.wait(interval):
            diff_tracker.lease_participant_refresh(
                repo=self.repo,
                lease_id=self.lease_id,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
                if os.environ.get("CODEX_RVF_LOG_ROOT")
                else None,
            )

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._old_handlers[sig] = signal.getsignal(sig)

            def _handler(signum: int, frame: object, *, _sig: int = sig) -> None:
                terminate_active_reviewer_process()
                self.stop()
                self.release(f"signal:{signal_name(signum)}")
                old = self._old_handlers.get(_sig)
                if callable(old):
                    old(signum, frame)
                raise SystemExit(128 + signum)

            signal.signal(sig, _handler)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._old_handlers.items():
            signal.signal(sig, handler)
        self._old_handlers.clear()


def build_prompt(
    prompt_file: Path,
    session_context: Path | None,
    review_packet: Path | None,
    repo: Path | None,
    scope_contract: Path | None = None,
    result_path: Path | None = None,
) -> str:
    parts = []
    env_lines = [
        "## Review session environment",
        "- The reviewer process receives short-lived `RVF_*` environment variables for this review session.",
        "- Use these variables in commands and notes instead of expanding absolute artifact paths.",
    ]
    if repo is not None:
        env_lines.append("- `RVF_REPO`: target repository. Use it as the repo path if `pwd` is not already the repo.")
    if scope_contract is not None:
        env_lines.append(
            "- `RVF_SCOPE_CONTRACT`: scope contract. Read it before reviewing and obey its scope boundaries; "
            "`primary_units` takes precedence over session manifest paths when present."
        )
    if session_context is not None:
        env_lines.extend(
            [
                "- `RVF_SCOPE_OF_WORK`: main-agent scope anchor. Read it before analyzing code.",
                "- `RVF_SESSION_CONTEXT`: alias for `RVF_SCOPE_OF_WORK`.",
            ]
        )
    if review_packet is not None:
        env_lines.append("- `RVF_REVIEW_PACKET`: self-contained review packet and fallback context.")
    if scope_contract is not None or session_context is not None or review_packet is not None:
        read_targets = []
        if scope_contract is not None:
            read_targets.append('"$RVF_SCOPE_CONTRACT"')
        if session_context is not None:
            read_targets.append('"$RVF_SCOPE_OF_WORK"')
        if review_packet is not None:
            read_targets.append('"$RVF_REVIEW_PACKET"')
        env_lines.append(f"- Read entry files with: `sed -n '1,220p' {' '.join(read_targets)}`")
    if repo is not None:
        env_lines.extend(
            [
                "- `RVF_COMMAND_LOCK`: repo-scoped command lock wrapper.",
                "- `RVF_WRITE_REVIEW_RESULT`: script for writing the canonical review result artifact.",
                "- `RVF_CHECK_REVIEW_RESULT`: script for validating the canonical artifact before final response.",
                '- Example: `python3 "$RVF_COMMAND_LOCK" --repo "$RVF_REPO" --name <stable-lock-name> -- <command ...>`',
                '- Result example: `python3 "$RVF_WRITE_REVIEW_RESULT" no-issues --out "$RVF_REVIEW_RESULT"`',
            ]
        )
    if result_path is not None:
        env_lines.extend(
            [
                "- `RVF_REVIEW_RESULT`: canonical reviewer result artifact. This protocol output is under the RVF run directory and is not a repo source edit.",
                '- Validate before your final message with: `python3 "$RVF_CHECK_REVIEW_RESULT" "$RVF_REVIEW_RESULT"`',
            ]
        )
    if session_context is not None:
        env_lines.append(
            "- Do not use the entire git diff as full review scope unless the main agent explicitly requested full diff review."
        )
    if scope_contract is not None:
        env_lines.append(
            "- Treat the session manifest as ownership evidence and tracker audit context, not as the final scope contract."
        )
    if len(env_lines) > 3:
        parts.append("\n".join(env_lines))
    parts.append(prompt_file.read_text(encoding="utf-8").strip())
    if review_packet is not None:
        parts.append(review_packet.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def scrub_env(env_unset: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    for name in CHILD_RVF_ENV_KEYS:
        env.pop(name, None)
    for name in env_unset:
        env.pop(name, None)
    env["RVF_SKILL_DIR"] = str(SKILL_DIR)
    env["RVF_COMMAND_LOCK"] = str(COMMAND_LOCK)
    env["RVF_WRITE_REVIEW_RESULT"] = str(WRITE_REVIEW_RESULT)
    env["RVF_CHECK_REVIEW_RESULT"] = str(CHECK_REVIEW_RESULT)
    return env


def check_review_result_artifact(path: Path, scope_contract: Path | None) -> tuple[dict[str, Any], str]:
    command = [sys.executable, str(CHECK_REVIEW_RESULT), str(path), "--json"]
    if scope_contract is not None:
        command.extend(["--scope-contract", str(scope_contract)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "kind": "invalid",
            "issue_count": 0,
            "request_count": 0,
            "request_types": [],
            "errors": [stdout or completed.stderr.strip() or "check_review_result.py returned invalid output"],
        }
    return payload, completed.stderr.strip()


def run_health(
    command: list[str],
    env: dict[str, str],
    timeout: int,
    *,
    emit_output: bool = True,
) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return fail(f"health command timed out after {timeout}s")
    if completed.returncode != 0:
        return fail(completed.stderr.strip() or completed.stdout.strip() or "health command failed")
    if emit_output:
        print(completed.stdout.strip())
    return 0


def _payload_length(payload: bytes | str | None) -> int:
    if payload is None:
        return 0
    return len(payload)


def _payload_delta(payload: bytes | str | None, start: int) -> str:
    if payload is None:
        return ""
    delta = payload[start:]
    if isinstance(delta, bytes):
        return delta.decode("utf-8", errors="replace")
    return delta


class ClaudeStreamActivityMonitor:
    def __init__(self) -> None:
        self.active_bash_tool_ids: set[str] = set()
        self.active_anonymous_bash_tools = 0
        self._pending_line = ""

    @property
    def waiting_on_long_command(self) -> bool:
        return bool(self.active_bash_tool_ids or self.active_anonymous_bash_tools > 0)

    def ingest(self, output: str) -> None:
        self._pending_line += output
        lines = self._pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        else:
            self._pending_line = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            self._ingest_line(line)

    def _ingest_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "result":
            self.active_bash_tool_ids.clear()
            self.active_anonymous_bash_tools = 0
            return
        message = payload.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "tool_use" and item.get("name") == "Bash":
                tool_id = item.get("id")
                if isinstance(tool_id, str) and tool_id:
                    self.active_bash_tool_ids.add(tool_id)
                else:
                    self.active_anonymous_bash_tools += 1
            elif item_type == "tool_result":
                tool_id = item.get("tool_use_id")
                if isinstance(tool_id, str) and tool_id:
                    self.active_bash_tool_ids.discard(tool_id)
                elif self.active_anonymous_bash_tools > 0:
                    self.active_anonymous_bash_tools -= 1


class ReviewerRunResult:
    def __init__(
        self,
        *,
        args: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
        pid: int | None,
        signal: int | None = None,
        terminated_signal: int | None = None,
        timeout_reason: str | None = None,
        probe_history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid
        self.signal = signal
        self.terminated_signal = terminated_signal
        self.timeout_reason = timeout_reason
        self.probe_history = probe_history or []


class CodexJsonOutputError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def signal_name(signal_number: int | None) -> str | None:
    if signal_number is None:
        return None
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return str(signal_number)


def subprocess_signal(returncode: int | None) -> int | None:
    if returncode is not None and returncode < 0:
        return -returncode
    return None


def _timeout_payload_delta(payload: bytes | str | None, start: int) -> str:
    return _payload_delta(payload, start)


def run_activity_probe(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    started_at: float,
) -> dict[str, Any]:
    probe_started_at = time.monotonic()
    record: dict[str, Any] = {
        "command": command,
        "started_after_seconds": round(probe_started_at - started_at, 3),
        "timeout_seconds": timeout_seconds,
        "status": "running",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        record.update(
            {
                "status": "completed" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "signal": signal_name(subprocess_signal(completed.returncode)),
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
        )
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "status": "timeout",
                "returncode": None,
                "signal": None,
                "stdout": _timeout_payload_delta(exc.stdout, 0),
                "stderr": _timeout_payload_delta(exc.stderr, 0),
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "error",
                "returncode": None,
                "signal": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
            }
        )
    ended_at = time.monotonic()
    record["ended_after_seconds"] = round(ended_at - started_at, 3)
    record["duration_seconds"] = round(ended_at - probe_started_at, 3)
    return record


def next_wait_seconds(
    *,
    activity_check_interval_seconds: float,
    remaining_idle_seconds: float,
    max_runtime_remaining_seconds: float | None,
    waiting_on_long_command: bool,
    probe_retry_remaining_seconds: float | None = None,
) -> float:
    wait_candidates = [activity_check_interval_seconds]
    if not waiting_on_long_command:
        wait_candidates.append(
            probe_retry_remaining_seconds
            if probe_retry_remaining_seconds is not None
            else remaining_idle_seconds
        )
    if max_runtime_remaining_seconds is not None:
        wait_candidates.append(max(0.0, max_runtime_remaining_seconds))
    return max(0.01, min(wait_candidates))


def set_active_reviewer_process(process: subprocess.Popen[str]) -> None:
    global _ACTIVE_REVIEWER_PROCESS
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        _ACTIVE_REVIEWER_PROCESS = process


def clear_active_reviewer_process(process: subprocess.Popen[str]) -> None:
    global _ACTIVE_REVIEWER_PROCESS
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        if _ACTIVE_REVIEWER_PROCESS is process:
            _ACTIVE_REVIEWER_PROCESS = None


def terminate_active_reviewer_process() -> int | None:
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        process = _ACTIVE_REVIEWER_PROCESS
    if process is None or process.poll() is not None:
        return None
    signal_number = terminate_process_group(process)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    return signal_number


def run_with_activity_timeout(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str],
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None,
    activity_probe_timeout_seconds: float,
    activity_probe_failure_threshold: int,
    max_runtime_seconds: float | None,
    output_format: str,
) -> ReviewerRunResult:
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
        start_new_session=True,
    )
    set_active_reviewer_process(process)
    started_at = time.monotonic()
    last_liveness_at = started_at
    last_stdout_len = 0
    last_stderr_len = 0
    next_probe_at = started_at
    consecutive_probe_failures = 0
    probe_history: list[dict[str, Any]] = []
    pending_input: str | None = input_text
    stream_monitor = (
        ClaudeStreamActivityMonitor()
        if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON
        else None
    )

    try:
        while True:
            now = time.monotonic()
            if max_runtime_seconds is not None and now - started_at >= max_runtime_seconds:
                return timeout_completed(
                    process,
                    command,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_check_interval_seconds=activity_check_interval_seconds,
                    activity_probe_command=activity_probe_command,
                    activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                    activity_probe_failure_threshold=activity_probe_failure_threshold,
                    max_runtime_seconds=max_runtime_seconds,
                    reason="max_runtime_exceeded",
                    probe_history=probe_history,
                )
            idle_for = now - last_liveness_at
            remaining_idle = max(0.0, idle_timeout_seconds - idle_for)
            max_runtime_remaining = (
                max_runtime_seconds - (now - started_at)
                if max_runtime_seconds is not None
                else None
            )
            probe_retry_remaining = None
            if (
                activity_probe_command is not None
                and remaining_idle <= 0.0
                and next_probe_at > now
            ):
                probe_retry_remaining = max(0.0, next_probe_at - now)
            wait_for = next_wait_seconds(
                activity_check_interval_seconds=activity_check_interval_seconds,
                remaining_idle_seconds=remaining_idle,
                max_runtime_remaining_seconds=max_runtime_remaining,
                waiting_on_long_command=(
                    stream_monitor.waiting_on_long_command
                    if stream_monitor is not None
                    else False
                ),
                probe_retry_remaining_seconds=probe_retry_remaining,
            )

            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=wait_for)
                return ReviewerRunResult(
                    args=command,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    pid=process.pid,
                    signal=subprocess_signal(process.returncode),
                    probe_history=probe_history,
                )
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                now = time.monotonic()
                if max_runtime_seconds is not None and now - started_at >= max_runtime_seconds:
                    return timeout_completed(
                        process,
                        command,
                        idle_timeout_seconds=idle_timeout_seconds,
                        activity_check_interval_seconds=activity_check_interval_seconds,
                        activity_probe_command=activity_probe_command,
                        activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                        activity_probe_failure_threshold=activity_probe_failure_threshold,
                        max_runtime_seconds=max_runtime_seconds,
                        reason="max_runtime_exceeded",
                        probe_history=probe_history,
                    )
                stdout_len = _payload_length(exc.stdout)
                stderr_len = _payload_length(exc.stderr)
                if stdout_len > last_stdout_len or stderr_len > last_stderr_len:
                    if stream_monitor is not None and stdout_len > last_stdout_len:
                        stream_monitor.ingest(_payload_delta(exc.stdout, last_stdout_len))
                    last_liveness_at = now
                    last_stdout_len = stdout_len
                    last_stderr_len = stderr_len
                    consecutive_probe_failures = 0
                    continue

                if now - last_liveness_at < idle_timeout_seconds:
                    continue
                if stream_monitor is not None and stream_monitor.waiting_on_long_command:
                    continue
                if activity_probe_command is not None:
                    if now < next_probe_at:
                        continue
                    probe_env = env.copy()
                    probe_env["RVF_REVIEWER_PID"] = str(process.pid)
                    probe = run_activity_probe(
                        activity_probe_command,
                        cwd=cwd,
                        env=probe_env,
                        timeout_seconds=activity_probe_timeout_seconds,
                        started_at=started_at,
                    )
                    probe_history.append(probe)
                    next_probe_at = time.monotonic() + activity_check_interval_seconds
                    if probe.get("status") == "completed" and probe.get("returncode") == 0:
                        last_liveness_at = time.monotonic()
                        consecutive_probe_failures = 0
                        continue
                    consecutive_probe_failures += 1
                    if consecutive_probe_failures < activity_probe_failure_threshold:
                        continue

                return timeout_completed(
                    process,
                    command,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_check_interval_seconds=activity_check_interval_seconds,
                    activity_probe_command=activity_probe_command,
                    activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                    activity_probe_failure_threshold=activity_probe_failure_threshold,
                    max_runtime_seconds=max_runtime_seconds,
                    reason=(
                        "no_observable_activity_probe_failed"
                        if activity_probe_command is not None
                        else "no_observable_activity"
                    ),
                    probe_history=probe_history,
                )
    except BaseException:
        if process.poll() is None:
            terminate_process_group(process)
        raise
    finally:
        clear_active_reviewer_process(process)


def timeout_completed(
    process: subprocess.Popen[str],
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None,
    activity_probe_timeout_seconds: float,
    activity_probe_failure_threshold: int,
    max_runtime_seconds: float | None,
    reason: str,
    probe_history: list[dict[str, Any]],
) -> ReviewerRunResult:
    terminated_signal = terminate_process_group(process)
    stdout, stderr = process.communicate()
    timeout_parts = [
        EXTERNAL_REVIEWER_TIMEOUT_FLAG,
        f"idle_timeout_seconds={idle_timeout_seconds:g}",
        f"activity_check_interval_seconds={activity_check_interval_seconds:g}",
    ]
    if activity_probe_command is not None:
        timeout_parts.extend(
            [
                f"activity_probe_timeout_seconds={activity_probe_timeout_seconds:g}",
                f"activity_probe_failure_threshold={activity_probe_failure_threshold}",
                f"activity_probe_failures={len([item for item in probe_history if item.get('status') != 'completed' or item.get('returncode') != 0])}",
            ]
        )
    if max_runtime_seconds is not None:
        timeout_parts.append(f"max_runtime_seconds={max_runtime_seconds:g}")
    timeout_parts.append(f"reason={reason}")
    timeout_line = " ".join(timeout_parts)
    stderr = (stderr or "").rstrip()
    stderr = f"{stderr}\n{timeout_line}" if stderr else timeout_line
    return ReviewerRunResult(
        args=command,
        returncode=EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE,
        stdout=stdout,
        stderr=stderr,
        pid=process.pid,
        signal=subprocess_signal(process.returncode),
        terminated_signal=terminated_signal,
        timeout_reason=reason,
        probe_history=probe_history,
    )


def terminate_process_group(process: subprocess.Popen[str]) -> int:
    try:
        signal_number = signal.SIGKILL
        os.killpg(os.getpgid(process.pid), signal_number)
        return signal_number
    except Exception:
        process.kill()
        return signal.SIGKILL


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


def text_parts_from_content(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        for key in ("output_text", "input_text"):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return parts


def message_text_from_payload(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content_text = "\n".join(text_parts_from_content(message.get("content"))).strip()
        if content_text:
            return content_text

    if payload.get("type") == "message":
        content_text = "\n".join(text_parts_from_content(payload.get("content"))).strip()
        if content_text:
            return content_text

    for key in ("text", "result", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def looks_like_codex_backend_challenge(output: str) -> bool:
    text = output.lstrip()[:8192].lower()
    if not text:
        return False
    htmlish = text.startswith("<!doctype html") or text.startswith("<html") or "<html" in text[:512]
    if not htmlish:
        return False
    challenge_markers = (
        "cloudflare",
        "cf-chl",
        "challenge-platform",
        "just a moment",
        "turnstile",
        "checking your browser",
    )
    return any(marker in text for marker in challenge_markers)


def extract_codex_json_result(output: str) -> str:
    """从 `codex exec --json` JSONL stdout 中提取最后一条 assistant 文本。"""

    result: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        record_type = record.get("type")
        payload = record.get("payload")
        if record_type in {"agent_message", "assistant_message"}:
            text = message_text_from_payload(record)
            if text:
                result = text
        elif record_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type in {"agent_message", "assistant_message"}:
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == "item.completed" and isinstance(record.get("item"), dict):
            item = record["item"]
            item_type = item.get("type")
            if item_type in {"agent_message", "assistant_message"}:
                text = message_text_from_payload(item)
                if text:
                    result = text
            elif item_type == "message" and item.get("role") == "assistant":
                text = message_text_from_payload(item)
                if text:
                    result = text
        elif record_type in {"result", "task_complete"}:
            text = message_text_from_payload(record)
            if text:
                result = text

    if result is not None:
        return result.strip()
    if looks_like_codex_backend_challenge(output):
        raise CodexJsonOutputError(
            "codex_backend_challenge",
            (
                f"{CODEX_BACKEND_CHALLENGE_FLAG} Codex CLI returned ChatGPT/Codex "
                "backend challenge HTML instead of valid `codex exec --json` output."
            ),
        )
    return output.strip()


def normalize_review_output(output: str) -> str:
    return output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run configured review-validate-fix alternative reviewer.")
    parser.add_argument("--repo", help="Target git repository.")
    parser.add_argument("--session-context", help="File containing the main-agent scope-of-work / Session context block.")
    parser.add_argument("--review-packet", help="Self-contained packet generated by build_review_packet.py, including Session Context.")
    parser.add_argument("--scope-contract", help="scope.contract.json path for this review run.")
    parser.add_argument("--prompt-file", default=str(DEFAULT_PROMPT), help="Review prompt file.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Alternative reviewer config JSON.")
    parser.add_argument("--output", help="Optional file to write reviewer stdout.")
    parser.add_argument("--check", action="store_true", help="Validate config and command availability only.")
    parser.add_argument("--preflight", action="store_true", help="Validate config, command availability, and configured health command when present.")
    parser.add_argument("--health", action="store_true", help="Run the configured health command.")
    parser.add_argument("--print-label", action="store_true", help="Print configured source label.")
    parser.add_argument("--dry-run", action="store_true", help="Print command and prompt length without invoking reviewer.")
    parser.add_argument("--rvf-run-id", help="Use an existing RVF run id instead of creating a new one.")
    parser.add_argument("--rvf-run-dir", help="Use this RVF run directory instead of resolving state/runs/<run_id>.")
    args = parser.parse_args()
    ledger = start_run(
        "reviewer",
        repo=args.repo,
        cwd=args.repo,
        run_id=args.rvf_run_id,
        run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
    )
    ledger.event(
        phase="review",
        event="started",
        status="started",
        reason_code="reviewer_started",
        repo=args.repo,
        cwd=args.repo,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        ledger.event(
            phase="review",
            event="config_missing",
            status="failed",
            reason_code="reviewer_config_missing",
            paths={"config": str(config_path)},
            error=f"missing config: {config_path}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_config_missing",
            message=f"缺少 alternative reviewer 配置: {config_path}",
            paths={"config": str(config_path)},
        )
        return fail(f"缺少 alternative reviewer 配置: {config_path}", 2)

    try:
        config = load_config(config_path)
        if config.get("enabled") is not True:
            ledger.event(
                phase="review",
                event="config_disabled",
                status="failed",
                reason_code="reviewer_config_disabled",
                paths={"config": str(config_path)},
            )
            ledger.summary(
                status="failed",
                reason_code="reviewer_config_disabled",
                message="alternative reviewer 未启用",
                paths={"config": str(config_path)},
            )
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
        activity_probe_command = optional_string_list(config, "activity_probe_command")
        activity_probe_timeout = positive_float(
            config,
            "activity_probe_timeout_seconds",
            DEFAULT_ACTIVITY_PROBE_TIMEOUT_SECONDS,
        )
        activity_probe_failure_threshold = positive_int(
            config,
            "activity_probe_failure_threshold",
            DEFAULT_ACTIVITY_PROBE_FAILURE_THRESHOLD,
        )
        max_runtime = optional_positive_float(
            config,
            "max_runtime_seconds",
            DEFAULT_MAX_RUNTIME_SECONDS,
        )
        health_timeout = int(positive_float(config, "health_timeout_seconds", 30.0))
        health_command = optional_string_list(config, "health_command")
        pre_run_health = config.get("pre_run_health", False)
        if not isinstance(pre_run_health, bool):
            raise ValueError("pre_run_health must be a boolean")
        if pre_run_health and health_command is None:
            raise ValueError("pre_run_health requires health_command")
        output_format = config.get("output_format")
        if output_format is None and is_claude_print_command(command):
            cli_output_format = claude_output_format_arg(command)
            if cli_output_format in {None, "stream-json"}:
                output_format = OUTPUT_FORMAT_CLAUDE_STREAM_JSON
                command = ensure_claude_stream_json_command(command)
            else:
                output_format = OUTPUT_FORMAT_TEXT
        elif output_format is None and is_codex_exec_command(command):
            output_format = OUTPUT_FORMAT_CODEX_JSON if codex_json_enabled(command) else OUTPUT_FORMAT_TEXT
        elif output_format is None:
            output_format = OUTPUT_FORMAT_TEXT
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}"
            )
        if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON and is_claude_print_command(command):
            command = ensure_claude_stream_json_command(command)
        if output_format == OUTPUT_FORMAT_CODEX_JSON and is_codex_exec_command(command):
            command = ensure_codex_hooks_disabled_command(command)
            command = ensure_codex_json_command(command)
        if is_codex_exec_command(command):
            command = ensure_codex_exec_add_dir(command, ledger.run_dir)
    except Exception as exc:
        ledger.event(
            phase="review",
            event="config_invalid",
            status="failed",
            reason_code="reviewer_config_invalid",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_config_invalid",
            message=f"alternative reviewer 配置无效: {exc}",
        )
        return fail(f"alternative reviewer 配置无效: {exc}", 2)

    if args.print_label:
        ledger.event(
            phase="review",
            event="print_label",
            status="completed",
            reason_code="print_label",
        )
        ledger.summary(status="completed", reason_code="print_label", message=label)
        print(label)
        return 0

    command_path = check_command(command)
    if command_path is None:
        ledger.event(
            phase="review",
            event="command_missing",
            status="failed",
            reason_code="reviewer_command_missing",
            command=command,
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_command_missing",
            message=f"找不到 alternative reviewer 命令: {command[0]}",
            command=command,
        )
        return fail(f"找不到 alternative reviewer 命令: {command[0]}", 2)

    env = scrub_env(env_unset)
    env.update(ledger.env())
    env[SUPPRESS_STOP_HOOK_ENV] = "1"
    env["RVF_RUN_DIR"] = str(ledger.run_dir)
    env["RVF_ARTIFACTS_DIR"] = str(ledger.artifacts_dir)

    if args.check:
        ledger.event(
            phase="review",
            event="check_completed",
            status="completed",
            reason_code="reviewer_check_completed",
            command_path=command_path,
        )
        ledger.summary(
            status="completed",
            reason_code="reviewer_check_completed",
            message=f"OK {label} {command_path}",
            command=command,
            command_path=command_path,
        )
        print(f"OK {label} {command_path}")
        return 0

    if args.preflight:
        print(f"OK {label} {command_path}")
        if health_command is None:
            ledger.event(
                phase="review",
                event="preflight_completed",
                status="completed",
                reason_code="reviewer_preflight_completed",
                command_path=command_path,
                health_configured=False,
            )
            ledger.summary(
                status="completed",
                reason_code="reviewer_preflight_completed",
                message="health command not configured",
                command=command,
                command_path=command_path,
                health_configured=False,
            )
            print("health command not configured")
            return 0
        returncode = run_health(health_command, env, health_timeout)
        ledger.event(
            phase="review",
            event="health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            command=health_command,
            returncode=returncode,
        )
        ledger.summary(
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            message="alternative reviewer health command completed",
            command=health_command,
            returncode=returncode,
        )
        return returncode

    if args.health:
        if health_command is None:
            return fail("health command not configured", 2)
        returncode = run_health(health_command, env, health_timeout)
        ledger.event(
            phase="review",
            event="health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            command=health_command,
            returncode=returncode,
        )
        ledger.summary(
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            message="alternative reviewer health command completed",
            command=health_command,
            returncode=returncode,
        )
        return returncode

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
    review_packet = Path(args.review_packet).expanduser().resolve() if args.review_packet else None
    scope_contract = Path(args.scope_contract).expanduser().resolve() if args.scope_contract else None
    reviewer_id = reviewer_id_from_label(label)
    reviewer_dir = ledger.artifacts_dir / "reviewers" / reviewer_id
    review_result_path = reviewer_dir / "review-result.json"

    try:
        if repo is None and review_packet is None:
            raise ValueError("缺少 --repo 或 --review-packet")
        if repo is not None:
            check_repo(repo)
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        if review_packet is not None and not review_packet.exists():
            raise ValueError(f"review packet file not found: {review_packet}")
        if scope_contract is None:
            scope_contract = infer_scope_contract(review_packet)
        if scope_contract is not None and not scope_contract.exists():
            raise ValueError(f"scope contract file not found: {scope_contract}")
        scope_contract_payload = load_scope_contract(scope_contract)
        prompt = build_prompt(prompt_file, session_context, review_packet, repo, scope_contract, review_result_path)
    except Exception as exc:
        ledger.event(
            phase="review",
            event="input_invalid",
            status="failed",
            reason_code="reviewer_input_invalid",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_input_invalid",
            message=str(exc),
        )
        return fail(str(exc), 2)

    if repo is not None:
        env["RVF_REPO"] = str(repo)
    if session_context is not None:
        env["RVF_SCOPE_OF_WORK"] = str(session_context)
        env["RVF_SESSION_CONTEXT"] = str(session_context)
    if review_packet is not None:
        env["RVF_REVIEW_PACKET"] = str(review_packet)
    if scope_contract is not None:
        env["RVF_SCOPE_CONTRACT"] = str(scope_contract)
    env["RVF_REVIEWER_ID"] = reviewer_id
    env["RVF_REVIEW_RESULT"] = str(review_result_path)

    if args.dry_run:
        cwd = str(repo) if repo is not None and allow_repo_cwd else str(review_packet.parent if review_packet is not None else SKILL_DIR)
        payload = {
            "label": label,
            "command": command,
            "cwd": cwd,
            "prompt_chars": len(prompt),
            "activity_probe_configured": activity_probe_command is not None,
            "scope_contract": str(scope_contract) if scope_contract is not None else None,
            "review_result": str(review_result_path),
        }
        ledger.event(
            phase="review",
            event="dry_run",
            status="completed",
            reason_code="reviewer_dry_run",
            cwd=cwd,
            prompt_chars=len(prompt),
        )
        ledger.summary(
            status="completed",
            reason_code="reviewer_dry_run",
            message="alternative reviewer dry run",
            **payload,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if pre_run_health and health_command is not None:
        returncode = run_health(health_command, env, health_timeout, emit_output=False)
        ledger.event(
            phase="review",
            event="pre_run_health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code=(
                "reviewer_pre_run_health_completed"
                if returncode == 0
                else "reviewer_pre_run_health_failed"
            ),
            command=health_command,
            returncode=returncode,
        )
        if returncode != 0:
            ledger.summary(
                status="failed",
                reason_code="reviewer_pre_run_health_failed",
                message="alternative reviewer pre-run health command failed",
                command=health_command,
                returncode=returncode,
            )
            return returncode

    cwd = repo if repo is not None and allow_repo_cwd else (review_packet.parent if review_packet is not None else SKILL_DIR)
    try:
        prompt_path = write_child_artifact(reviewer_dir, "reviewer.prompt.txt", prompt, unique=True)
    except OSError as exc:
        ledger.event(
            phase="review",
            event="artifact_write_failed",
            status="failed",
            reason_code="reviewer_artifact_write_failed",
            error=f"{type(exc).__name__}: {exc}",
            paths={"reviewer_dir": str(reviewer_dir)},
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_artifact_write_failed",
            message=f"failed to write reviewer artifact: {exc}",
            paths={"reviewer_dir": str(reviewer_dir)},
        )
        return fail(f"failed to write reviewer artifact: {exc}", 2)

    lease_runtime = TrackerLeaseRuntime(
        repo=repo,
        scope_contract=scope_contract_payload,
        reviewer_id=reviewer_id,
        run_id=ledger.run_id,
    )
    lease_release_reason = "failed"
    try:
        lease_runtime.acquire()
        lease_runtime.start()
        try:
            completed = run_with_activity_timeout(
                command,
                input_text=prompt,
                cwd=cwd,
                env=env,
                idle_timeout_seconds=idle_timeout,
                activity_check_interval_seconds=activity_check_interval,
                activity_probe_command=activity_probe_command,
                activity_probe_timeout_seconds=activity_probe_timeout,
                activity_probe_failure_threshold=activity_probe_failure_threshold,
                max_runtime_seconds=max_runtime,
                output_format=output_format,
            )
        except Exception as exc:
            completed = ReviewerRunResult(
                args=command,
                returncode=1,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                pid=None,
            )
        lease_release_reason = (
            "failed"
            if completed.returncode != 0
            or (
                output_format == OUTPUT_FORMAT_CODEX_JSON
                and looks_like_codex_backend_challenge(completed.stdout or "")
            )
            else "completed"
        )
    finally:
        lease_runtime.stop()
        lease_runtime.release(lease_release_reason)

    raw_stdout = completed.stdout or ""
    raw_stderr = completed.stderr or ""
    stdout = raw_stdout.strip()
    output_error_reason: str | None = None
    if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON:
        stdout = extract_claude_stream_result(stdout)
    elif output_format == OUTPUT_FORMAT_CODEX_JSON:
        try:
            stdout = extract_codex_json_result(stdout)
        except CodexJsonOutputError as exc:
            output_error_reason = exc.reason_code
            stdout = str(exc)
            if completed.returncode == 0:
                completed.returncode = 1
            raw_stderr = f"{raw_stderr.rstrip()}\n{exc}".strip()
    stdout = normalize_review_output(stdout)
    stderr = raw_stderr.strip()
    timed_out = (
        completed.returncode == EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE
        and EXTERNAL_REVIEWER_TIMEOUT_FLAG in stderr
    )
    if timed_out:
        stdout = EXTERNAL_REVIEWER_TIMEOUT_FLAG
    normalized_path = write_child_artifact(reviewer_dir, "reviewer.normalized.txt", stdout + ("\n" if stdout else ""), unique=True)
    stdout_path = write_child_artifact(reviewer_dir, "reviewer.stdout.txt", raw_stdout, unique=True)
    stderr_path = write_child_artifact(reviewer_dir, "reviewer.stderr.txt", raw_stderr, unique=True)
    review_result_summary: dict[str, Any] | None = None
    review_result_check_stderr = ""
    review_result_summary_path: str | None = None
    if completed.returncode == 0:
        review_result_summary, review_result_check_stderr = check_review_result_artifact(
            review_result_path,
            scope_contract,
        )
        review_result_summary_path = write_child_artifact(
            reviewer_dir,
            "review-result.summary.json",
            review_result_summary,
            unique=True,
        )
        if not review_result_summary.get("valid"):
            completed.returncode = 1
            errors = review_result_summary.get("errors")
            error_text = "; ".join(str(item) for item in errors) if isinstance(errors, list) else "invalid review result artifact"
            stderr = f"{stderr}\n{error_text}".strip()
    review_result_kind = review_result_summary.get("kind") if review_result_summary else None
    review_result_complete = review_result_kind in {"no_issues", "issues"}
    review_request_pending = review_result_kind == "request"
    probe_history_path = write_child_artifact(
        reviewer_dir,
        "reviewer.activity_probe_history.json",
        completed.probe_history,
        unique=True,
    )
    reviewer_summary_path = write_child_artifact(
        reviewer_dir,
        "reviewer.summary.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "reviewer_id": reviewer_id,
            "command": command,
            "cwd": str(cwd),
            "output_format": output_format,
            "scope_contract": str(scope_contract) if scope_contract is not None else None,
            "review_packet": str(review_packet) if review_packet is not None else None,
            "session_context": str(session_context) if session_context is not None else None,
            "paths": {
                "prompt": prompt_path,
                "stdout": stdout_path,
                "stderr": stderr_path,
                "normalized": normalized_path,
                "review_result": str(review_result_path),
                "review_result_summary": review_result_summary_path,
                "activity_probe_history": probe_history_path,
            },
            "review_result_summary": review_result_summary,
            "review_result_complete": review_result_complete,
            "review_request_pending": review_request_pending,
            "output_error_reason": output_error_reason,
        },
        unique=True,
    )
    if args.output:
        output_text = f"{EXTERNAL_REVIEWER_TIMEOUT_FLAG}\n" if timed_out else stdout + ("\n" if stdout else "")
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(stdout)

    paths = {
        "prompt": prompt_path,
        "stdout": stdout_path,
        "stderr": stderr_path,
        "normalized": normalized_path,
        "review_result": str(review_result_path),
        "review_result_summary": review_result_summary_path,
        "activity_probe_history": probe_history_path,
        "reviewer_summary": reviewer_summary_path,
        "reviewer_dir": str(reviewer_dir),
        "review_packet": str(review_packet) if review_packet is not None else None,
        "session_context": str(session_context) if session_context is not None else None,
        "scope_contract": str(scope_contract) if scope_contract is not None else None,
    }
    status = "completed" if completed.returncode == 0 else "failed"
    reason_code = "reviewer_completed"
    event_name = "completed"
    message = "alternative reviewer completed"
    if timed_out:
        reason_code = "reviewer_timeout"
        event_name = "failed"
        message = "alternative reviewer failed"
    elif review_result_summary is not None and not review_result_summary.get("valid"):
        reason_code = "reviewer_result_invalid"
        event_name = "failed"
        message = "alternative reviewer failed"
    elif review_request_pending:
        status = "pending"
        reason_code = "reviewer_request_pending"
        event_name = "request_pending"
        message = "alternative reviewer recorded a request"
    elif output_error_reason == "codex_backend_challenge":
        reason_code = "reviewer_codex_backend_challenge"
        event_name = "failed"
        message = "alternative reviewer failed before producing valid Codex JSON output"
    elif completed.returncode != 0:
        reason_code = "reviewer_failed"
        event_name = "failed"
        message = "alternative reviewer failed"
    ledger.event(
        phase="review",
        event=event_name,
        status=status,
        reason_code=reason_code,
        repo=str(repo) if repo is not None else None,
        cwd=str(cwd),
        paths={key: value for key, value in paths.items() if value},
        returncode=completed.returncode,
        pid=completed.pid,
        signal=signal_name(completed.signal),
        signal_number=completed.signal,
        terminated_signal=signal_name(completed.terminated_signal),
        terminated_signal_number=completed.terminated_signal,
        timed_out=timed_out,
        timeout_reason=completed.timeout_reason,
        review_result_valid=bool(review_result_summary and review_result_summary.get("valid")),
        review_result_kind=review_result_kind,
        review_result_complete=review_result_complete,
        review_request_pending=review_request_pending,
        review_result_check_stderr=review_result_check_stderr,
        output_error_reason=output_error_reason,
        activity_probe_configured=activity_probe_command is not None,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        activity_probe_history=completed.probe_history,
        output_format=output_format,
        reviewer_id=reviewer_id,
    )
    ledger.summary(
        status=status,
        reason_code=reason_code,
        message=message,
        repo=str(repo) if repo is not None else None,
        cwd=str(cwd),
        paths={key: value for key, value in paths.items() if value},
        returncode=completed.returncode,
        pid=completed.pid,
        signal=signal_name(completed.signal),
        signal_number=completed.signal,
        terminated_signal=signal_name(completed.terminated_signal),
        terminated_signal_number=completed.terminated_signal,
        timed_out=timed_out,
        timeout_reason=completed.timeout_reason,
        review_result_valid=bool(review_result_summary and review_result_summary.get("valid")),
        review_result_kind=review_result_kind,
        review_result_complete=review_result_complete,
        review_request_pending=review_request_pending,
        review_result_summary=review_result_summary,
        review_result_check_stderr=review_result_check_stderr,
        output_error_reason=output_error_reason,
        activity_probe_configured=activity_probe_command is not None,
        activity_probe_command=activity_probe_command,
        activity_probe_timeout_seconds=activity_probe_timeout,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        activity_probe_history=completed.probe_history,
        output_format=output_format,
        label=label,
        reviewer_id=reviewer_id,
    )

    if completed.returncode != 0:
        if stderr:
            print(stderr, file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
