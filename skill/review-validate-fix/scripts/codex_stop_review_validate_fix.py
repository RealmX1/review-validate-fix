#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = SKILL_DIR / "state" / "fork-experiment"
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
DEFAULT_RVF_MODE = "continuation"
DEFAULT_FORK_LAUNCH_MODE = "manual"
SUPPRESS_ENV_NAMES = (
    "CODEX_RVF_SUPPRESS",
    "CODEX_RVF_SUPPRESS_STOP_HOOK",
)
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


@dataclass(frozen=True)
class GateResult:
    status: str
    repo: str | None
    output: str


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_RVF_STATE_DIR", str(DEFAULT_STATE_DIR)))


def read_event() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return event


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def source_marks_subagent(source: Any) -> bool:
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


def session_meta_marks_subagent(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return False
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict) and source_marks_subagent(payload.get("source")):
                    return True
    except OSError:
        return False
    return False


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def text_from_message_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def latest_user_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue

                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        if text:
                            latest = text
    except OSError:
        return None
    return latest


def latest_user_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_user_message")
    if isinstance(direct, str) and direct:
        return direct

    for path in event_session_paths(event):
        message = latest_user_message(path)
        if message:
            return message
    return None


def session_id_from_path(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    return payload["id"]
    except OSError:
        return None
    return None


def session_id_from_event(event: dict[str, Any]) -> str | None:
    value = event.get("session_id")
    if isinstance(value, str) and value:
        return value

    for path in event_session_paths(event):
        session_id = session_id_from_path(path)
        if session_id:
            return session_id
    return None


def string_event_value(event: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def configured_reasoning_effort() -> str | None:
    env_value = os.environ.get("CODEX_RVF_FORK_REASONING_EFFORT")
    if env_value and env_value.strip():
        return env_value.strip()

    config_path = Path(os.environ.get("CODEX_RVF_CODEX_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        return None

    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        value = data.get("model_reasoning_effort")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    pattern = re.compile(r'^model_reasoning_effort\s*=\s*"([^"]+)"\s*$')
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    except OSError:
        return None
    return None


def reasoning_effort_for_fork(event: dict[str, Any]) -> str | None:
    return string_event_value(
        event,
        (
            "model_reasoning_effort",
            "reasoning_effort",
            "reasoningEffort",
        ),
    ) or configured_reasoning_effort()


def fork_experiment_prompt(parent_session_id: str, cwd: str | None) -> str:
    cwd_line = cwd or "<unknown cwd>"
    return (
        "Codex fork experiment sidecar session.\n\n"
        f"Parent session id: {parent_session_id}\n"
        f"Parent cwd: {cwd_line}\n\n"
        "请用中文简短回复：\n"
        "1. 你是否看起来是一个新 fork 出来的会话。\n"
        "2. 你能看到的当前工作目录是什么。\n"
        "3. 你是否看到了父会话的上下文。\n\n"
        "不要运行 $review-validate-fix，不要修改文件。"
    )


def fork_review_validate_fix_prompt(
    parent_session_id: str,
    parent_cwd: str | None,
    repo: str,
) -> str:
    cwd_line = parent_cwd or "<unknown cwd>"
    return (
        "$review-validate-fix\n\n"
        f"{RVF_FORK_MARKER}\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CWD: {cwd_line}\n"
        f"RVF_TARGET_REPO: {repo}\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后 fork 出来的 "
        "review-validate-fix 会话。请基于完整父会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}\n\n"
        "完成后按 skill 规范生成最终汇总和 <handoff-context>。不要在正文里提示"
        "用户复制 handoff；Stop hook 会在本会话结束时用 systemMessage 做程序化提示。"
    )


def parse_marker_value(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


def rvf_fork_context(latest_user: str | None) -> dict[str, str] | None:
    if not latest_user or RVF_FORK_MARKER not in latest_user:
        return None
    return {
        "parent_session_id": parse_marker_value(latest_user, "RVF_PARENT_SESSION_ID") or "",
        "parent_cwd": parse_marker_value(latest_user, "RVF_PARENT_CWD") or "",
        "target_repo": parse_marker_value(latest_user, "RVF_TARGET_REPO") or "",
    }


def handoff_advisory(event: dict[str, Any], context: dict[str, str]) -> dict[str, Any] | None:
    last_assistant_message = event.get("last_assistant_message")
    if (
        not isinstance(last_assistant_message, str)
        or "<handoff-context>" not in last_assistant_message
        or "</handoff-context>" not in last_assistant_message
    ):
        return None

    session_id = session_id_from_event(event) or "unknown-session"
    sdir = state_dir()
    marker_path = sdir / f"{session_id}.handoff-advised"
    if marker_path.exists():
        return None

    sdir.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "context": context,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    parent = context.get("parent_session_id") or "<unknown>"
    parent_cwd = context.get("parent_cwd") or "<unknown>"
    target_repo = context.get("target_repo") or "<unknown>"
    return {
        "continue": True,
        "systemMessage": (
            "review-validate-fix fork 已结束。请复制本 fork 会话最终回复中的 "
            "<handoff-context> 块，并粘贴回原始 chat session。"
            f" parent_session_id={parent}; parent_cwd={parent_cwd}; target_repo={target_repo}"
        ),
    }


def run_codex_fork(
    *,
    parent_session_id: str,
    cwd: str | None,
    prompt: str,
    log_prefix: str,
    mode_env_name: str = "CODEX_RVF_FORK_MODE",
    suppress_child_stop_hook: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    mode = os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE)

    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = sdir / f"{timestamp}.{log_prefix}.json"
    latest_path = sdir / "latest.json"
    prompt_path = sdir / f"{timestamp}.{log_prefix}.prompt.txt"
    launcher_path = sdir / f"{timestamp}.{log_prefix}.sh"

    if not parent_session_id:
        payload = {
            "continue": True,
            "systemMessage": (
                f"{log_prefix} skipped: Stop event did not expose a parent session id."
            ),
        }
        log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    prompt_path.write_text(prompt, encoding="utf-8")
    command = ["codex", "fork"]
    if model:
        command.extend(["-m", model])
    if reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
    command.append(parent_session_id)
    launcher = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cd {shlex.quote(cwd or str(Path.home()))}\n"
    )
    if suppress_child_stop_hook:
        launcher += "export CODEX_RVF_SUPPRESS_STOP_HOOK=1\n"
    launcher += " ".join(shlex.quote(part) for part in command)
    launcher += f" \"$(cat {shlex.quote(str(prompt_path))})\"\n"
    launcher_path.write_text(launcher, encoding="utf-8")
    launcher_path.chmod(0o755)
    shell_command = "bash " + shlex.quote(str(launcher_path))

    result: dict[str, Any] = {
        "timestamp": timestamp,
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_session_id": parent_session_id,
        "cwd": cwd,
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "launcher_path": str(launcher_path),
        "shell_command": shell_command,
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }

    if mode in {"manual", "prepare", "prepared", "log-only"}:
        result["status"] = "manual-prepared"
    elif mode == "dry-run":
        result["status"] = "dry-run"
    elif mode == "exec":
        completed = subprocess.run(
            shell_command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result.update(
            {
                "status": "exec-finished",
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
    else:
        result.update(
            {
                "status": "terminal-launch-disabled",
                "returncode": 0,
                "stdout": "",
                "stderr": (
                    "Terminal-based codex fork is disabled because Desktop "
                    "session ids are not reliably visible to the CLI."
                ),
            }
        )

    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    status = result.get("status", "unknown")
    if status == "manual-prepared":
        message = (
            f"{log_prefix} prepared: no Terminal was launched. "
            f"parent_session_id={parent_session_id}. launcher={launcher_path}. "
            f"prompt={prompt_path}. log={log_path}"
        )
    else:
        message = (
            f"{log_prefix} triggered: "
            f"{status}. parent_session_id={parent_session_id}. log={log_path}"
        )
    return {
        "continue": True,
        "systemMessage": message,
    }


def run_fork_experiment(event: dict[str, Any], latest_user: str) -> dict[str, Any]:
    session_id = session_id_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    prompt = fork_experiment_prompt(session_id or "", cwd)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    payload = run_codex_fork(
        parent_session_id=session_id or "",
        cwd=cwd,
        prompt=prompt,
        log_prefix="fork-experiment",
        mode_env_name="CODEX_RVF_FORK_EXPERIMENT_MODE",
        suppress_child_stop_hook=True,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    latest_path = state_dir() / "latest.json"
    try:
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        data["marker"] = os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER)
        data["latest_user_message"] = latest_user
        latest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return payload


def should_run_fork_experiment(event: dict[str, Any]) -> tuple[bool, str | None]:
    marker = os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER)
    latest_user = latest_user_message_from_event(event)
    if latest_user and marker in latest_user:
        return True, latest_user
    return False, latest_user


def rvf_mode() -> str:
    mode = os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE).strip().lower()
    if mode in {"continuation", "continue", "block"}:
        return "continuation"
    if mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    return "fork"


def fork_review_validate_fix(event: dict[str, Any], repo: str) -> dict[str, Any]:
    parent_session_id = session_id_from_event(event) or ""
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else repo
    prompt = fork_review_validate_fix_prompt(parent_session_id, cwd, repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=parent_session_id,
        cwd=repo,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def review_validate_fix_dispatch(event: dict[str, Any], repo: str) -> dict[str, Any] | None:
    mode = rvf_mode()
    if mode == "off":
        return {
            "continue": True,
            "systemMessage": f"review-validate-fix Stop hook 已跳过：CODEX_RVF_MODE=off。repo={repo}",
        }
    if mode == "continuation":
        return continuation(repo)
    return fork_review_validate_fix(event, repo)


def should_suppress(event: dict[str, Any]) -> bool:
    if any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES):
        return True

    if event.get("suppress_review_validate_fix") is True:
        return True
    if event.get("review_validate_fix_suppressed") is True:
        return True

    if source_marks_subagent(event.get("source")):
        return True

    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def run_gate(repo: str) -> GateResult:
    gate = Path(os.environ.get("CODEX_RVF_GATE", str(DEFAULT_GATE)))
    try:
        completed = subprocess.run(
            ["bash", str(gate), repo],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return GateResult("ERROR", None, str(exc))

    output = completed.stdout.strip()
    first_line = output.splitlines()[0] if output else ""
    parts = first_line.split(maxsplit=1)
    status = parts[0] if parts else "ERROR"
    resolved_repo = parts[1] if len(parts) > 1 else None
    return GateResult(status, resolved_repo, output)


def parse_trusted_projects(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []

    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        projects = data.get("projects", {})
        if isinstance(projects, dict):
            return [
                str(path)
                for path, settings in projects.items()
                if isinstance(settings, dict)
                and settings.get("trust_level") == "trusted"
            ]
    except Exception:
        pass

    trusted: list[str] = []
    current_path: str | None = None
    current_trusted = False
    section_re = re.compile(r'^\[projects\."(.*)"\]\s*$')

    def flush() -> None:
        if current_path and current_trusted:
            trusted.append(current_path)

    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = section_re.match(stripped)
        if match:
            flush()
            current_path = match.group(1).replace(r"\"", '"')
            current_trusted = False
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            flush()
            current_path = None
            current_trusted = False
            continue
        if current_path and stripped == 'trust_level = "trusted"':
            current_trusted = True
    flush()
    return trusted


def trusted_dirty_repos() -> tuple[list[str], list[str]]:
    config_path = Path(os.environ.get("CODEX_RVF_CONFIG", str(DEFAULT_CONFIG)))
    dirty: list[str] = []
    errors: list[str] = []

    for project in parse_trusted_projects(config_path):
        result = run_gate(project)
        if result.status == "DIRTY" and result.repo:
            if result.repo not in dirty:
                dirty.append(result.repo)
        elif result.status == "ERROR":
            errors.append(f"{project}: {result.output}")

    return dirty, errors


def continuation(repo: str) -> dict[str, Any]:
    reason = (
        "$review-validate-fix\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后自动提交的 "
        "continuation prompt。请基于完整会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}"
    )
    return {"decision": "block", "reason": reason}


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    if event.get("stop_hook_active") is True:
        return 0

    latest_user = latest_user_message_from_event(event)
    fork_context = rvf_fork_context(latest_user)
    if fork_context is not None:
        advisory = handoff_advisory(event, fork_context)
        if advisory is not None:
            emit(advisory)
        return 0

    should_experiment, latest_user = should_run_fork_experiment(event)
    if should_experiment and latest_user is not None:
        emit(run_fork_experiment(event, latest_user))
        return 0

    if should_suppress(event):
        return 0

    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        cwd_result = run_gate(cwd)
        if cwd_result.status == "DIRTY" and cwd_result.repo:
            payload = review_validate_fix_dispatch(event, cwd_result.repo)
            if payload is not None:
                emit(payload)
            return 0
        if cwd_result.status == "CLEAN":
            return 0

    dirty_repos, _errors = trusted_dirty_repos()
    if len(dirty_repos) == 1:
        payload = review_validate_fix_dispatch(event, dirty_repos[0])
        if payload is not None:
            emit(payload)
        return 0
    if len(dirty_repos) > 1:
        joined = ", ".join(dirty_repos)
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Stop hook 已跳过：发现多个 dirty trusted repo，"
                    f"为避免审错仓库未自动触发。候选: {joined}"
                ),
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
