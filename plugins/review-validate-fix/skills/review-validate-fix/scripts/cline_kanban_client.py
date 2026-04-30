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
from typing import Any


DEFAULT_KANBAN_VERSION = "0.1.66"
DEFAULT_START_CMD = f"npx -y kanban@{DEFAULT_KANBAN_VERSION} --no-open"
DEFAULT_TASK_CMD = f"npx -y kanban@{DEFAULT_KANBAN_VERSION} task"
DEFAULT_START_TIMEOUT_SECONDS = 90.0
DEFAULT_TMUX_SESSION = "rvf-cline-kanban"

# Contract surface: this wrapper shells out to `kanban task create`,
# `kanban task start`, and `kanban task trash`.


class KanbanError(RuntimeError):
    pass


def split_command(value: str) -> list[str]:
    parts = shlex.split(value)
    if not parts:
        raise KanbanError("command must not be empty")
    return parts


def run_command(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"{command[0]} failed"
        raise KanbanError(detail)
    return completed


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


def task_list(*, task_cmd: str, repo: Path) -> dict[str, Any]:
    completed = run_command(
        task_command(task_cmd, "list", "--project-path", str(repo)),
        cwd=repo,
    )
    return parse_json_stdout(completed)


def start_kanban_server(
    *,
    start_cmd: str,
    repo: Path,
    tmux_session: str,
) -> dict[str, Any]:
    shell_command = f"cd {shlex.quote(str(repo))} && exec {start_cmd}"
    command = ["tmux", "new-session", "-d", "-s", tmux_session, shell_command]
    completed = run_command(command, cwd=repo, check=False)
    if completed.returncode != 0:
        already_exists = "duplicate session" in (completed.stderr + completed.stdout).lower()
        if not already_exists:
            raise KanbanError(completed.stderr.strip() or completed.stdout.strip() or "failed to start Kanban tmux session")
    return {
        "tmux_session": tmux_session,
        "start_cmd": start_cmd,
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
    first = run_command(
        task_command(task_cmd, "list", "--project-path", str(repo)),
        cwd=repo,
        check=False,
    )
    if first.returncode == 0:
        payload = parse_json_stdout(first)
        return {"started": False, "list": payload}
    if not start_if_needed:
        raise KanbanError(first.stderr.strip() or first.stdout.strip() or "Kanban server is unavailable")

    launcher = start_kanban_server(start_cmd=start_cmd, repo=repo, tmux_session=tmux_session)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_error = first.stderr.strip() or first.stdout.strip()
    while time.monotonic() <= deadline:
        probe = run_command(
            task_command(task_cmd, "list", "--project-path", str(repo)),
            cwd=repo,
            check=False,
        )
        if probe.returncode == 0:
            payload = parse_json_stdout(probe)
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


def create_task(
    *,
    task_cmd: str,
    repo: Path,
    prompt: str,
    base_ref: str,
    title: str | None,
    agent_id: str | None,
    start_in_plan_mode: bool,
    auto_review_enabled: bool,
    auto_review_mode: str,
) -> dict[str, Any]:
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
    payload = parse_json_stdout(run_command(command, cwd=repo))
    payload["task_id"] = normalize_task_id(payload)
    return payload


def start_task(*, task_cmd: str, repo: Path, task_id: str) -> dict[str, Any]:
    payload = parse_json_stdout(
        run_command(
            task_command(task_cmd, "start", "--project-path", str(repo), "--task-id", task_id),
            cwd=repo,
        )
    )
    try:
        payload["task_id"] = normalize_task_id(payload)
    except KanbanError:
        payload["task_id"] = task_id
    return payload


def trash_task(*, task_cmd: str, repo: Path, task_id: str) -> dict[str, Any]:
    payload = parse_json_stdout(
        run_command(
            task_command(task_cmd, "trash", "--project-path", str(repo), "--task-id", task_id),
            cwd=repo,
        )
    )
    payload.setdefault("task_id", task_id)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Cline Kanban CLI client for RVF.")
    parser.add_argument("action", choices=["ensure", "list", "create", "start", "trash"])
    parser.add_argument("--task-cmd", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_TASK_CMD))
    parser.add_argument("--start-cmd", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD", DEFAULT_START_CMD))
    parser.add_argument("--start-timeout", type=float, default=float(os.environ.get("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT", DEFAULT_START_TIMEOUT_SECONDS)))
    parser.add_argument("--tmux-session", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", DEFAULT_TMUX_SESSION))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--start-if-needed", action="store_true")
    parser.add_argument("--prompt")
    parser.add_argument("--base-ref", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF"))
    parser.add_argument("--title")
    parser.add_argument("--agent-id")
    parser.add_argument("--task-id")
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
            payload = task_list(task_cmd=args.task_cmd, repo=repo)
        elif args.action == "create":
            if args.prompt is None:
                raise KanbanError("--prompt is required for create")
            if not args.base_ref:
                raise KanbanError("--base-ref is required for create")
            payload = create_task(
                task_cmd=args.task_cmd,
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
            payload = start_task(task_cmd=args.task_cmd, repo=repo, task_id=args.task_id)
        else:
            if not args.task_id:
                raise KanbanError("--task-id is required for trash")
            payload = trash_task(task_cmd=args.task_cmd, repo=repo, task_id=args.task_id)
    except Exception as exc:
        print(f"cline-kanban error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
