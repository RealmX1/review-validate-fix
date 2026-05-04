#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


DEFAULT_KANBAN_VERSION = "0.1.67"
DEFAULT_START_CMD = "kanban --no-open"
DEFAULT_TASK_CMD = "kanban task"
DEFAULT_START_TIMEOUT_SECONDS = 90.0
DEFAULT_TMUX_SESSION = "cline-kanban-3484"
DEFAULT_RUNTIME_PORT = 3484
CLINE_KANBAN_TMUX_SESSION_NAME = "cline-kanban"

# 契约边界：这个 wrapper 只 shell 到 `kanban task create`、
# `kanban task start`、`kanban task trash`，以及 RVF 定制的
# `kanban task message` follow-up 用户消息注入命令。


class KanbanError(RuntimeError):
    pass


def split_command(value: str) -> list[str]:
    parts = shlex.split(value)
    if not parts:
        raise KanbanError("command must not be empty")
    return parts


def parse_runtime_port_value(value: str, *, source: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise KanbanError(f"invalid Cline Kanban runtime port from {source}: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise KanbanError(f"Cline Kanban runtime port from {source} out of range: {value!r}")
    return port


def command_runtime_port_spec(command: str, *, source: str) -> tuple[str, int | None]:
    parts = split_command(command)
    fixed_ports: set[int] = set()
    auto = False
    for index, part in enumerate(parts):
        value: str | None = None
        if part == "--port":
            if index + 1 >= len(parts):
                raise KanbanError(f"missing Cline Kanban --port value in {source}")
            value = parts[index + 1]
        elif part.startswith("--port="):
            value = part.split("=", 1)[1]
        elif part.startswith("KANBAN_RUNTIME_PORT="):
            value = part.split("=", 1)[1]
        if value is None:
            continue
        if value.strip().lower() == "auto":
            auto = True
            continue
        fixed_ports.add(parse_runtime_port_value(value, source=source))
    if auto and fixed_ports:
        raise KanbanError(f"conflicting Cline Kanban --port values in {source}")
    if auto:
        return ("auto", None)
    if len(fixed_ports) > 1:
        ports = ", ".join(str(port) for port in sorted(fixed_ports))
        raise KanbanError(f"conflicting Cline Kanban --port values in {source}: {ports}")
    if fixed_ports:
        return ("fixed", next(iter(fixed_ports)))
    return ("unset", None)


def env_runtime_port_spec(env: Mapping[str, str]) -> tuple[str, int | None]:
    value = (env.get("KANBAN_RUNTIME_PORT") or "").strip()
    if not value:
        return ("unset", None)
    if value.lower() == "auto":
        return ("auto", None)
    return ("fixed", parse_runtime_port_value(value, source="KANBAN_RUNTIME_PORT"))


def resolve_runtime_port(
    *,
    start_cmd: str | None = None,
    task_cmd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    specs: list[tuple[str, tuple[str, int | None]]] = []
    if start_cmd is not None:
        specs.append(("start command", command_runtime_port_spec(start_cmd, source="start command")))
    if task_cmd is not None:
        specs.append(("task command", command_runtime_port_spec(task_cmd, source="task command")))
    specs.append(("KANBAN_RUNTIME_PORT", env_runtime_port_spec(os.environ if env is None else env)))

    auto_sources = [source for source, (mode, _) in specs if mode == "auto"]
    if auto_sources:
        sources = ", ".join(auto_sources)
        raise KanbanError(
            "Cline Kanban --port auto is not supported by RVF because task CLI "
            "commands need a fixed KANBAN_RUNTIME_PORT for server ownership checks. "
            f"Use a fixed --port or omit it for {DEFAULT_RUNTIME_PORT}. Source(s): {sources}."
        )

    fixed: dict[int, list[str]] = {}
    for source, (_, port) in specs:
        if port is None:
            continue
        fixed.setdefault(port, []).append(source)
    if len(fixed) > 1:
        details = ", ".join(
            f"{port} from {'/'.join(sources)}"
            for port, sources in sorted(fixed.items())
        )
        raise KanbanError(f"conflicting Cline Kanban runtime ports: {details}")
    if fixed:
        return next(iter(fixed))
    return DEFAULT_RUNTIME_PORT


def runtime_env(port: int) -> dict[str, str]:
    env = os.environ.copy()
    env["KANBAN_RUNTIME_PORT"] = str(port)
    return env


def task_runtime_port(*, task_cmd: str, start_cmd: str | None = None) -> int:
    if start_cmd is None:
        start_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD")
    return resolve_runtime_port(start_cmd=start_cmd, task_cmd=task_cmd)


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise KanbanError(
            f"Cline Kanban command not found: {command[0]!r}. Install or upgrade a stable "
            f"`kanban` binary with `npm install -g kanban@{DEFAULT_KANBAN_VERSION}`, "
            "or set CODEX_RVF_CLINE_KANBAN_TASK_CMD/CODEX_RVF_CLINE_KANBAN_START_CMD "
            "to a stable local binary. RVF does not use npx for its default Kanban path."
        ) from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"{command[0]} failed"
        raise KanbanError(detail)
    return completed


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def listener_pids_for_port(port: int) -> list[int]:
    completed = run_command(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        check=False,
    )
    if completed.returncode != 0:
        return []
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            pids.append(int(text))
        except ValueError:
            continue
    return pids


def process_cwd(pid: int) -> Path | None:
    completed = run_command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], check=False)
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("n/"):
            return Path(line[1:])
    return None


def process_command(pid: int) -> str:
    completed = run_command(["ps", "-p", str(pid), "-o", "command="], check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def process_parent_pid(pid: int) -> int | None:
    completed = run_command(["ps", "-p", str(pid), "-o", "ppid="], check=False)
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    if not text:
        return None
    try:
        parent_pid = int(text)
    except ValueError:
        return None
    if parent_pid <= 0:
        return None
    return parent_pid


def process_ancestry(pid: int, *, max_depth: int = 32) -> list[int]:
    ancestry: list[int] = []
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current not in seen and len(ancestry) < max_depth:
        ancestry.append(current)
        seen.add(current)
        current = process_parent_pid(current)
    return ancestry


def tmux_sessions_for_pid(pid: int) -> list[str]:
    completed = run_command(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_pid}",
        ],
        check=False,
    )
    if completed.returncode != 0:
        return []
    candidate_pids = {str(ancestor_pid) for ancestor_pid in process_ancestry(pid)}
    sessions: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            session_name, pane_pid = line.split("\t", 1)
        except ValueError:
            continue
        if pane_pid.strip() in candidate_pids:
            sessions.append(session_name.strip())
    return sorted(session for session in sessions if session)


def is_cline_kanban_tmux_session(session_name: str) -> bool:
    return session_name == CLINE_KANBAN_TMUX_SESSION_NAME or session_name.startswith(
        f"{CLINE_KANBAN_TMUX_SESSION_NAME}-"
    )


def describe_listener(pid: int) -> str:
    cwd = process_cwd(pid)
    command = process_command(pid)
    tmux_sessions = tmux_sessions_for_pid(pid)
    cwd_text = str(cwd) if cwd is not None else "<unknown>"
    command_text = command or "<unknown>"
    tmux_text = ",".join(tmux_sessions) if tmux_sessions else "<none>"
    return f"pid={pid} cwd={cwd_text} tmux={tmux_text} command={command_text}"


def payload_workspace_path(payload: dict[str, Any]) -> Path | None:
    for key in ("workspacePath", "workspace_path", "projectPath", "project_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        value = workspace.get("path")
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    return None


def assert_server_belongs_to_repo(
    *,
    port: int,
    repo: Path,
    payload: dict[str, Any] | None = None,
) -> None:
    workspace_path = payload_workspace_path(payload) if payload is not None else None
    if workspace_path is not None:
        if not same_path(workspace_path, repo):
            raise KanbanError(
                f"Kanban CLI on 127.0.0.1:{port} returned workspace {workspace_path}, "
                f"but RVF expected {repo}."
            )

    pids = listener_pids_for_port(port)
    if not pids:
        return
    for pid in pids:
        if any(is_cline_kanban_tmux_session(session) for session in tmux_sessions_for_pid(pid)):
            return
    details = "; ".join(describe_listener(pid) for pid in pids)
    raise KanbanError(
        f"Kanban CLI reached a server on 127.0.0.1:{port}, but no listener pane belongs "
        f"to tmux session `{CLINE_KANBAN_TMUX_SESSION_NAME}` or "
        f"`{CLINE_KANBAN_TMUX_SESSION_NAME}-*`. Listener(s): {details}. Stop the "
        "foreign listener or restart Kanban from a correctly named tmux session before "
        "creating RVF tasks."
    )


def running_listener_error(*, port: int, last_error: str) -> KanbanError:
    pids = listener_pids_for_port(port)
    if not pids:
        return KanbanError(last_error or "Kanban server is unavailable")
    details = "; ".join(describe_listener(pid) for pid in pids)
    detail = last_error or "Kanban task list failed"
    return KanbanError(
        f"Kanban server is already listening on 127.0.0.1:{port}, but RVF could not "
        f"connect with task list and will not start another Kanban server. "
        f"Error: {detail}. Listener(s): {details}"
    )


def parse_json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KanbanError(f"Kanban command did not return JSON: {completed.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise KanbanError(f"Kanban command returned non-object JSON: {payload!r}")
    if payload.get("ok") is False:
        raise KanbanError(str(payload.get("error") or payload))
    return payload


def task_command(task_cmd: str, *args: str) -> list[str]:
    return [*split_command(task_cmd), *args]


def task_list(*, task_cmd: str, repo: Path, start_cmd: str | None = None) -> dict[str, Any]:
    port = task_runtime_port(task_cmd=task_cmd, start_cmd=start_cmd)
    completed = run_command(
        task_command(task_cmd, "list", "--project-path", str(repo)),
        cwd=repo,
        env=runtime_env(port),
    )
    return parse_json_stdout(completed)


def start_kanban_server(
    *,
    start_cmd: str,
    repo: Path,
    tmux_session: str,
    runtime_port: int,
) -> dict[str, Any]:
    shell_command = (
        f"cd {shlex.quote(str(repo))} && "
        f"export KANBAN_RUNTIME_PORT={shlex.quote(str(runtime_port))} && "
        f"exec {start_cmd}"
    )
    command = ["tmux", "new-session", "-d", "-s", tmux_session, shell_command]
    completed = run_command(command, cwd=repo, check=False, env=runtime_env(runtime_port))
    if completed.returncode != 0:
        already_exists = "duplicate session" in (completed.stderr + completed.stdout).lower()
        if not already_exists:
            raise KanbanError(completed.stderr.strip() or completed.stdout.strip() or "failed to start Kanban tmux session")
    return {
        "tmux_session": tmux_session,
        "start_cmd": start_cmd,
        "runtime_port": runtime_port,
        "command": command,
        "returncode": completed.returncode,
        "stderr": completed.stderr,
    }


def ensure_kanban(
    *,
    task_cmd: str,
    start_cmd: str,
    repo: Path,
    tmux_session: str,
    timeout_seconds: float,
    start_if_needed: bool,
) -> dict[str, Any]:
    runtime_port = resolve_runtime_port(start_cmd=start_cmd, task_cmd=task_cmd)
    task_env = runtime_env(runtime_port)
    first = run_command(
        task_command(task_cmd, "list", "--project-path", str(repo)),
        cwd=repo,
        check=False,
        env=task_env,
    )
    if first.returncode == 0:
        payload = parse_json_stdout(first)
        assert_server_belongs_to_repo(port=runtime_port, repo=repo, payload=payload)
        return {"started": False, "list": payload}
    first_error = first.stderr.strip() or first.stdout.strip() or "Kanban server is unavailable"
    if not start_if_needed:
        raise running_listener_error(port=runtime_port, last_error=first_error)
    if listener_pids_for_port(runtime_port):
        raise running_listener_error(port=runtime_port, last_error=first_error)

    launcher = start_kanban_server(
        start_cmd=start_cmd,
        repo=repo,
        tmux_session=tmux_session,
        runtime_port=runtime_port,
    )
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_error = first_error
    while time.monotonic() <= deadline:
        probe = run_command(
            task_command(task_cmd, "list", "--project-path", str(repo)),
            cwd=repo,
            check=False,
            env=task_env,
        )
        if probe.returncode == 0:
            payload = parse_json_stdout(probe)
            assert_server_belongs_to_repo(port=runtime_port, repo=repo, payload=payload)
            return {"started": True, "launcher": launcher, "list": payload}
        last_error = probe.stderr.strip() or probe.stdout.strip() or last_error
        time.sleep(1.0)
    raise KanbanError(f"timed out waiting for Cline Kanban server: {last_error}")


def normalize_task_id(payload: dict[str, Any]) -> str:
    task = payload.get("task")
    if isinstance(task, dict):
        value = task.get("id") or task.get("task_id") or task.get("taskId")
        if isinstance(value, str) and value.strip():
            return value
    for key in ("task_id", "taskId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise KanbanError(f"Kanban response did not include task id: {payload!r}")


def normalize_message_id(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        value = message.get("id") or message.get("message_id") or message.get("messageId")
        if isinstance(value, str) and value.strip():
            return value
    for key in ("message_id", "messageId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise KanbanError(f"Kanban response did not include message id: {payload!r}")


def create_task(
    *,
    task_cmd: str,
    start_cmd: str | None = None,
    repo: Path,
    prompt: str,
    base_ref: str,
    title: str | None,
    agent_id: str | None,
    start_in_plan_mode: bool,
    auto_review_enabled: bool,
    auto_review_mode: str,
) -> dict[str, Any]:
    port = task_runtime_port(task_cmd=task_cmd, start_cmd=start_cmd)
    command = task_command(
        task_cmd,
        "create",
        "--project-path",
        str(repo),
        "--base-ref",
        base_ref,
        "--prompt",
        prompt,
    )
    if title:
        command.extend(["--title", title])
    if agent_id:
        command.extend(["--agent-id", agent_id])
    if start_in_plan_mode:
        command.append("--start-in-plan-mode")
    if auto_review_enabled:
        command.extend(["--auto-review-enabled", "--auto-review-mode", auto_review_mode])
    payload = parse_json_stdout(run_command(command, cwd=repo, env=runtime_env(port)))
    payload["task_id"] = normalize_task_id(payload)
    return payload


def start_task(*, task_cmd: str, repo: Path, task_id: str, start_cmd: str | None = None) -> dict[str, Any]:
    port = task_runtime_port(task_cmd=task_cmd, start_cmd=start_cmd)
    payload = parse_json_stdout(
        run_command(
            task_command(task_cmd, "start", "--project-path", str(repo), "--task-id", task_id),
            cwd=repo,
            env=runtime_env(port),
        )
    )
    try:
        payload["task_id"] = normalize_task_id(payload)
    except KanbanError:
        payload["task_id"] = task_id
    return payload


def trash_task(*, task_cmd: str, repo: Path, task_id: str, start_cmd: str | None = None) -> dict[str, Any]:
    port = task_runtime_port(task_cmd=task_cmd, start_cmd=start_cmd)
    payload = parse_json_stdout(
        run_command(
            task_command(task_cmd, "trash", "--project-path", str(repo), "--task-id", task_id),
            cwd=repo,
            env=runtime_env(port),
        )
    )
    payload.setdefault("task_id", task_id)
    return payload


def send_task_message(
    *,
    task_cmd: str,
    start_cmd: str | None = None,
    repo: Path,
    task_id: str,
    prompt: str | None,
    prompt_file: Path | None,
    source: str,
    idempotency_key: str,
    attempt_id: str | None,
) -> dict[str, Any]:
    if prompt is None and prompt_file is None:
        raise KanbanError("message requires --prompt or --prompt-file")
    port = task_runtime_port(task_cmd=task_cmd, start_cmd=start_cmd)
    command = task_command(
        task_cmd,
        "message",
        "--project-path",
        str(repo),
        "--task-id",
        task_id,
    )
    if prompt_file is not None:
        command.extend(["--prompt-file", str(prompt_file)])
    else:
        command.extend(["--prompt", prompt or ""])
    command.extend(["--source", source, "--idempotency-key", idempotency_key])
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])
    payload = parse_json_stdout(run_command(command, cwd=repo, env=runtime_env(port)))
    try:
        payload["task_id"] = normalize_task_id(payload)
    except KanbanError:
        payload["task_id"] = task_id
    payload["message_id"] = normalize_message_id(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Cline Kanban CLI client for RVF.")
    parser.add_argument("action", choices=["ensure", "list", "create", "start", "trash", "message"])
    parser.add_argument("--task-cmd", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_TASK_CMD))
    parser.add_argument("--start-cmd", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD", DEFAULT_START_CMD))
    parser.add_argument("--start-timeout", type=float, default=float(os.environ.get("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT", DEFAULT_START_TIMEOUT_SECONDS)))
    parser.add_argument("--tmux-session", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", DEFAULT_TMUX_SESSION))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--start-if-needed", action="store_true")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--base-ref", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF"))
    parser.add_argument("--title")
    parser.add_argument("--agent-id")
    parser.add_argument("--task-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--source", default="review-validate-fix")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--start-in-plan-mode", action="store_true")
    parser.add_argument("--auto-review-enabled", action="store_true")
    parser.add_argument("--auto-review-mode", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", "commit"))
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.action == "ensure":
            payload = ensure_kanban(
                task_cmd=args.task_cmd,
                start_cmd=args.start_cmd,
                repo=repo,
                tmux_session=args.tmux_session,
                timeout_seconds=args.start_timeout,
                start_if_needed=args.start_if_needed,
            )
        elif args.action == "list":
            payload = task_list(task_cmd=args.task_cmd, start_cmd=args.start_cmd, repo=repo)
        elif args.action == "create":
            if args.prompt is None:
                raise KanbanError("--prompt is required for create")
            if not args.base_ref:
                raise KanbanError("--base-ref is required for create")
            payload = create_task(
                task_cmd=args.task_cmd,
                start_cmd=args.start_cmd,
                repo=repo,
                prompt=args.prompt,
                base_ref=args.base_ref,
                title=args.title,
                agent_id=args.agent_id,
                start_in_plan_mode=args.start_in_plan_mode,
                auto_review_enabled=args.auto_review_enabled,
                auto_review_mode=args.auto_review_mode,
            )
        elif args.action == "start":
            if not args.task_id:
                raise KanbanError("--task-id is required for start")
            payload = start_task(task_cmd=args.task_cmd, start_cmd=args.start_cmd, repo=repo, task_id=args.task_id)
        elif args.action == "trash":
            if not args.task_id:
                raise KanbanError("--task-id is required for trash")
            payload = trash_task(task_cmd=args.task_cmd, start_cmd=args.start_cmd, repo=repo, task_id=args.task_id)
        else:
            if not args.task_id:
                raise KanbanError("--task-id is required for message")
            if not args.idempotency_key:
                raise KanbanError("--idempotency-key is required for message")
            payload = send_task_message(
                task_cmd=args.task_cmd,
                start_cmd=args.start_cmd,
                repo=repo,
                task_id=args.task_id,
                prompt=args.prompt,
                prompt_file=Path(args.prompt_file).expanduser().resolve() if args.prompt_file else None,
                source=args.source,
                idempotency_key=args.idempotency_key,
                attempt_id=args.attempt_id,
            )
    except Exception as exc:
        print(f"cline-kanban error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
