#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import RunLedger, log_root  # noqa: E402
from cline_kanban_client import DEFAULT_TASK_CMD, trash_task


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_run(args: argparse.Namespace) -> tuple[str, Path, dict[str, Any]]:
    summary_path: Path | None = None
    run_dir: Path | None = None
    if args.summary:
        summary_path = Path(args.summary).expanduser().resolve()
        run_dir = summary_path.parent
    elif args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        summary_path = run_dir / "summary.json"
    elif args.run_id:
        run_dir = log_root() / "runs" / args.run_id
        summary_path = run_dir / "summary.json"

    summary = read_json_object(summary_path) if summary_path is not None else {}
    run_id = args.run_id or str(summary.get("run_id") or "").strip()
    if not run_id:
        raise SystemExit("--run-id, --run-dir, or --summary must identify an RVF run_id")
    if run_dir is None:
        run_dir = log_root() / "runs" / run_id
    return run_id, run_dir, summary


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def normalize_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def ps_processes() -> list[tuple[int, str]]:
    completed = subprocess.run(
        ["ps", "axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    processes: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        pid = normalize_pid(pid_text)
        if pid is not None:
            processes.append((pid, command.strip()))
    return processes


def command_matches_run(run_id: str, command: str) -> bool:
    if run_id not in command:
        return False
    return (
        "cline_kanban_client.py" in command
        or "apply_worktree_bootstrap.py" in command
        or "codex" in command
        or "review-validate-fix" in command
    )


def discover_run_processes(run_id: str, summary: dict[str, Any]) -> dict[int, str]:
    own_pids = {os.getpid(), os.getppid()}
    candidates: dict[int, str] = {}
    processes = ps_processes()
    process_commands = {pid: command for pid, command in processes}
    runner_pid = normalize_pid(summary.get("runner_pid"))
    if (
        runner_pid
        and runner_pid not in own_pids
        and command_matches_run(run_id, process_commands.get(runner_pid, ""))
    ):
        candidates[runner_pid] = "summary.runner_pid"
    for pid, command in processes:
        if pid in own_pids:
            continue
        if "cancel_rvf_run.py" in command:
            continue
        if command_matches_run(run_id, command):
            candidates.setdefault(pid, command)
    return candidates


def terminate_process_group(pid: int) -> dict[str, Any] | None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        return {"pid": pid, "signal": "SIGTERM", "kind": "process_group", "error": f"PermissionError: {exc}"}
    if pgid in {0, os.getpgrp()}:
        return None
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        return {"pid": pid, "pgid": pgid, "signal": "SIGTERM", "kind": "process_group", "error": f"PermissionError: {exc}"}
    return {"pid": pid, "pgid": pgid, "signal": "SIGTERM", "kind": "process_group"}


def terminate_process(pid: int, sig: signal.Signals) -> dict[str, Any] | None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        return {"pid": pid, "signal": sig.name, "kind": "process", "error": f"PermissionError: {exc}"}
    return {"pid": pid, "signal": sig.name, "kind": "process"}


def wait_for_exit(pids: list[int], timeout_seconds: float) -> list[int]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    remaining = [pid for pid in pids if pid_is_alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if pid_is_alive(pid)]
    return remaining


def cancellation_description(
    *,
    summary: dict[str, Any],
    run_id: str,
    run_dir: Path,
    cancelled_pids: list[int],
    still_running_pids: list[int],
) -> str:
    lines = [
        "status: cancelled",
        f"run_id: {run_id}",
        f"target repo: {summary.get('repo') or summary.get('cwd') or '<unknown>'}",
        f"parent session id: {summary.get('parent_thread_id') or '<unknown>'}",
        f"parent transcript path: {summary.get('parent_transcript_path') or summary.get('parent_thread_path') or '<unknown>'}",
        f"run_dir: {run_dir}",
        f"events.jsonl: {run_dir / 'events.jsonl'}",
        f"summary.json: {run_dir / 'summary.json'}",
        "reason: user_cancelled",
        f"cancelled_pids: {cancelled_pids}",
    ]
    if still_running_pids:
        lines.append(f"still_running_pids: {still_running_pids}")
    return "\n".join(lines)


def update_management_record(
    *,
    ledger: RunLedger,
    summary: dict[str, Any],
    run_id: str,
    run_dir: Path,
    args: argparse.Namespace,
    cancelled_pids: list[int],
    still_running_pids: list[int],
) -> None:
    del cancelled_pids, still_running_pids
    task_id = summary.get("cline_kanban_task_id")
    repo = summary.get("repo") or summary.get("cwd") or summary.get("workspace_path")
    if isinstance(task_id, str) and task_id.strip() and isinstance(repo, str) and repo.strip():
        try:
            payload = trash_task(
                task_cmd=args.task_cmd,
                repo=Path(repo).expanduser().resolve(),
                task_id=task_id,
            )
            ledger.artifact("cline-kanban-task-cancelled.json", payload, unique=True)
        except Exception as exc:
            ledger.event(
                phase="fork",
                event="cline_kanban_task_trash_failed",
                status="warn",
                reason_code="cline_kanban_task_trash_failed",
                level="warn",
                error=f"{type(exc).__name__}: {exc}",
            )


def cancel_run(args: argparse.Namespace) -> dict[str, Any]:
    run_id, run_dir, summary = resolve_run(args)
    repo = summary.get("repo") or summary.get("cwd")
    ledger = RunLedger(
        component="cline-kanban",
        repo=repo if isinstance(repo, str) else None,
        cwd=repo if isinstance(repo, str) else None,
        run_id=run_id,
        run_dir=run_dir,
    )
    processes = discover_run_processes(run_id, summary)
    pids = sorted(processes)
    signals_sent: list[dict[str, Any]] = []
    still_running: list[int] = pids

    ledger.event(
        phase="fork",
        event="run_cancel_requested",
        status="cancelled",
        reason_code="user_cancelled",
        candidate_pids=pids,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        runner_pid = normalize_pid(summary.get("runner_pid"))
        if runner_pid in processes:
            group_signal = terminate_process_group(runner_pid)
            if group_signal:
                signals_sent.append(group_signal)
        for pid in pids:
            signal_result = terminate_process(pid, signal.SIGTERM)
            if signal_result:
                signals_sent.append(signal_result)
        still_running = wait_for_exit(pids, args.force_after)
        for pid in still_running:
            signal_result = terminate_process(pid, signal.SIGKILL)
            if signal_result:
                signals_sent.append(signal_result)
        still_running = wait_for_exit(still_running, 1.0)

        cancelled_pids = [pid for pid in pids if pid not in still_running]
        ledger.event(
            phase="fork",
            event="run_cancelled",
            status="cancelled" if not still_running else "warn",
            reason_code="user_cancelled",
            cancelled_pids=cancelled_pids,
            still_running_pids=still_running,
            signals_sent=signals_sent,
        )
        ledger.summary(
            status="cline-kanban-rvf-cancelled",
            reason_code="user_cancelled",
            message="RVF run cancelled by user request.",
            repo=repo if isinstance(repo, str) else None,
            cwd=repo if isinstance(repo, str) else None,
            cancelled_pids=cancelled_pids,
            still_running_pids=still_running,
            signals_sent=signals_sent,
        )
        update_management_record(
            ledger=ledger,
            summary=summary,
            run_id=run_id,
            run_dir=run_dir,
            args=args,
            cancelled_pids=cancelled_pids,
            still_running_pids=still_running,
        )
        try:
            from rvf_run_finalize import finalize_run

            finalize_run(
                run_dir=run_dir,
                event=None,
                decision_kind="cancelled",
            )
        except Exception as exc:
            ledger.event(
                phase="fork",
                event="finalize_failed",
                status="warn",
                reason_code="finalize_on_cancel_failed",
                level="warn",
                error=f"{type(exc).__name__}: {exc}",
            )
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": "dry-run" if args.dry_run else "cancelled",
        "candidate_pids": pids,
        "signals_sent": signals_sent,
        "still_running_pids": still_running,
        "summary_path": str(run_dir / "summary.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="取消 Cline Kanban RVF run，并把状态标为 cancelled。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id")
    group.add_argument("--run-dir")
    group.add_argument("--summary")
    parser.add_argument("--task-cmd", default=os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_TASK_CMD))
    parser.add_argument("--force-after", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = cancel_run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
