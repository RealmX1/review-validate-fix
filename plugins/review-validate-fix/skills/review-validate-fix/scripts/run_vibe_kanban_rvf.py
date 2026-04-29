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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, start_run
from vibe_kanban_mcp_client import DEFAULT_MCP_CMD, update_issue, update_local_workspace


DEFAULT_CODEX_EXEC_ARGS = "exec --json --dangerously-bypass-approvals-and-sandbox"
SCRIPT_DIR = Path(__file__).resolve().parent
HEADLESS_MARKER = "RVF_HEADLESS_REVIEW_VALIDATE_FIX"
SUPPRESS_STOP_HOOK_MARKER = "CODEX_RVF_SUPPRESS_STOP_HOOK=1"


def split_args(value: str) -> list[str]:
    args = shlex.split(value)
    if not args:
        raise ValueError("CODEX_RVF_CODEX_EXEC_ARGS must not be empty")
    return args


def build_codex_command(args: argparse.Namespace, final_message_path: Path) -> list[str]:
    command = [args.codex_bin]
    command.extend(split_args(args.codex_exec_args))
    if args.model:
        command.extend(["-m", args.model])
    if args.reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(args.reasoning_effort)}"])
    command.extend(["-C", str(args.repo), "--output-last-message", str(final_message_path), "-"])
    return command


def issue_description(
    *,
    status: str,
    repo: Path,
    parent_session_id: str,
    parent_transcript_path: Path | None,
    run_dir: Path,
    final_message_path: Path | None = None,
    returncode: int | None = None,
    error: str | None = None,
) -> str:
    transcript = str(parent_transcript_path) if parent_transcript_path is not None else "<unknown>"
    lines = [
        f"status: {status}",
        f"target repo: {repo}",
        f"parent session id: {parent_session_id}",
        f"parent transcript path: {transcript}",
        f"run_dir: {run_dir}",
        f"events.jsonl: {run_dir / 'events.jsonl'}",
        f"summary.json: {run_dir / 'summary.json'}",
        f"review-env.sh: {run_dir / 'artifacts' / 'review-env.sh'}",
        f"review-agent-context.md: {run_dir / 'artifacts' / 'review-agent-context.md'}",
    ]
    if final_message_path is not None:
        lines.append(f"final message: {final_message_path}")
    if returncode is not None:
        lines.append(f"returncode: {returncode}")
    if error:
        lines.append(f"error: {error}")
    return "\n".join(lines)


def quoted_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def build_child_env(
    *,
    base_env: Mapping[str, str],
    ledger_env: Mapping[str, str],
    parent_session_id: str,
    parent_transcript_path: Path | None,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("CODEX_RVF_") and not key.startswith("RVF_")
    }
    env.update(ledger_env)
    env["CODEX_RVF_SUPPRESS_STOP_HOOK"] = "1"
    env["CODEX_RVF_SUPPRESS"] = "1"
    if parent_session_id:
        env["CODEX_RVF_PARENT_SESSION_ID"] = parent_session_id
    if parent_transcript_path is not None:
        env["CODEX_RVF_PARENT_TRANSCRIPT_PATH"] = str(parent_transcript_path)
    return env


def build_headless_prompt(
    *,
    original_prompt: str,
    repo: Path,
    run_id: str,
    run_dir: Path,
    prompt_path: Path,
    parent_session_id: str,
    parent_transcript_path: Path | None,
    startup_prepare: dict[str, Any] | None = None,
) -> str:
    artifacts_dir = run_dir / "artifacts"
    scope_path = artifacts_dir / "headless-scope-of-work.md"
    prepare_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "prepare_review_run.py"),
        "--repo",
        str(repo),
        "--session-context",
        str(scope_path),
        "--rvf-run-id",
        run_id,
        "--rvf-run-dir",
        str(run_dir),
    ]
    transcript_display = "<unknown>"
    if parent_transcript_path is not None:
        transcript_display = str(parent_transcript_path)
        prepare_cmd.extend(["--transcript", str(parent_transcript_path)])

    frozen_lines: list[str] = []
    if startup_prepare:
        frozen_lines = [
            "Frozen startup artifacts captured before the headless runner was queued:",
            f"- scope-of-work: {startup_prepare.get('scope_of_work_file') or '<unavailable>'}",
            f"- session manifest: {startup_prepare.get('session_manifest_file') or '<unavailable>'}",
            f"- review packet: {startup_prepare.get('review_packet') or '<unavailable>'}",
            f"- workspace snapshot: {startup_prepare.get('before_workspace_snapshot') or '<unavailable>'}",
            f"- review-env.sh: {startup_prepare.get('review_env_file') or '<unavailable>'}",
            f"- review-agent-context.md: {startup_prepare.get('review_agent_context_file') or '<unavailable>'}",
            "",
            "必须优先读取并复用这些冻结 artifacts；不要因 runner 排队后 worktree 变化而重新定义 review scope。",
            "只有当这些 artifacts 缺失或不可读时，才运行下面的 prepare 命令并明确说明 fallback 原因。",
            "",
        ]
    if frozen_lines:
        preparation_steps = (
            "开始 `$review-validate-fix` 流程前必须完成以下准备：\n"
            f"1. 在目标仓库 `{repo}` 中工作。\n"
            "2. 读取上方 frozen startup artifacts 中的 `review-env.sh` 和 "
            "`review-agent-context.md`，并以其中的 review packet、session manifest、"
            "workspace snapshot 作为本轮唯一启动 scope anchor。\n"
            "3. 不要用 runner 启动后的实时 worktree 重新生成默认 scope；如 frozen artifacts "
            "缺失或不可读，请 fail-close，或在明确记录 fallback 原因后运行下面的准备命令：\n\n"
        )
    else:
        preparation_steps = (
            "开始 `$review-validate-fix` 流程前必须完成以下准备：\n"
            f"1. 在目标仓库 `{repo}` 中工作。\n"
            f"2. 优先读取父 transcript `{transcript_display}` 来恢复本 turn 的 session-owned scope；"
            "如果 transcript 缺失或不足以可靠判断 scope，不要编造 scope，也不要降级为 whole diff review，"
            "请 fail-close 并说明缺少父会话上下文。\n"
            f"3. 将主会话 scope-of-work/session context 写入 `{scope_path}`，内容必须说明本 turn 的用户意图、"
            "实际改动文件、每个文件的具体编辑面、已跑验证和不确定点。\n"
            "4. 复用当前 RunLedger，不要创建新的 run id/run dir。准备命令应使用：\n\n"
        )

    return (
        "$review-validate-fix\n\n"
        f"{HEADLESS_MARKER}\n"
        f"RVF_TARGET_REPO: {repo}\n"
        f"RVF_RUN_ID: {run_id}\n"
        f"RVF_RUN_DIR: {run_dir}\n"
        f"RVF_ARTIFACTS_DIR: {artifacts_dir}\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {transcript_display}\n"
        f"RVF_ORIGINAL_FORK_PROMPT: {prompt_path}\n\n"
        "Stop hook child-session metadata:\n"
        f"{SUPPRESS_STOP_HOOK_MARKER}\n"
        "当前 headless RVF 子进程结束时必须跳过 review-validate-fix Stop hook。\n\n"
        "Existing RunLedger artifacts:\n"
        f"- events.jsonl: {run_dir / 'events.jsonl'}\n"
        f"- summary.json: {run_dir / 'summary.json'}\n"
        f"- original fork prompt: {prompt_path}\n"
        f"- review-env.sh target: {artifacts_dir / 'review-env.sh'}\n"
        f"- review-agent-context.md target: {artifacts_dir / 'review-agent-context.md'}\n"
        f"- handoff.md target: {artifacts_dir / 'handoff.md'}\n\n"
        + ("\n".join(frozen_lines) + "\n" if frozen_lines else "") +
        "这是 Vibe-Kanban 管理的 headless RVF 子进程，运行在 `codex exec` 中，"
        "不是 Codex GUI fork。不要假设你拥有父 GUI 会话的内存上下文；下面的原始 fork "
        "prompt 只能作为元数据。\n\n"
        f"{preparation_steps}"
        "```sh\n"
        f"{quoted_command(prepare_cmd)}\n"
        "```\n\n"
        "后续 reviewer/validate/fix 交接必须使用 "
        "`review-env.sh` 和 `review-agent-context.md`，不要手写新的 export block，也不要把 "
        "`git diff HEAD` 当成默认 review scope。Handoff 默认开启时，必须持续维护 "
        f"`{artifacts_dir / 'handoff.md'}`，最终回复只输出 "
        "`RVF_HANDOFF_FILE: <handoff.md 绝对路径>` 作为第一行，随后只追加 "
        "1-3 句极短中文说明 reviewers 和 validate/fixers 做了什么。\n\n"
        "原始 fork prompt 如下，仅作兼容元数据：\n\n"
        "```text\n"
        f"{original_prompt.rstrip()}\n"
        "```\n"
    )


def safe_update_issue(
    *,
    ledger: RunLedger,
    mcp_cmd: str,
    backend_url: str | None,
    project_id: str | None,
    issue_id: str | None,
    title: str | None,
    description: str,
    status: str,
) -> dict[str, Any] | None:
    if not project_id or not issue_id:
        return None
    try:
        payload = update_issue(
            mcp_cmd=mcp_cmd,
            backend_url=backend_url,
            project_id=project_id,
            issue_id=issue_id,
            title=title,
            description=description,
            status=status,
        )
        ledger.artifact(f"vibe-kanban-issue-{status}.json", payload, unique=True)
        return payload
    except Exception as exc:
        ledger.event(
            phase="fork",
            event="vibe_kanban_issue_update_failed",
            status="warn",
            reason_code="vibe_kanban_issue_update_failed",
            level="warn",
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


def safe_update_workspace(
    *,
    ledger: RunLedger,
    backend_url: str | None,
    workspace_id: str | None,
    title: str | None,
    description: str,
    status: str,
) -> dict[str, Any] | None:
    if not backend_url or not workspace_id:
        return None
    try:
        payload = update_local_workspace(
            backend_url=backend_url,
            workspace_id=workspace_id,
            title=title,
            description=description,
            status=status,
        )
        ledger.artifact(f"vibe-kanban-workspace-{status}.json", payload, unique=True)
        return payload
    except Exception as exc:
        ledger.event(
            phase="fork",
            event="vibe_kanban_workspace_update_failed",
            status="warn",
            reason_code="vibe_kanban_workspace_update_failed",
            level="warn",
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


def safe_update_management_record(
    *,
    ledger: RunLedger,
    mcp_cmd: str,
    backend_url: str | None,
    project_id: str | None,
    issue_id: str | None,
    workspace_id: str | None,
    title: str | None,
    description: str,
    status: str,
) -> None:
    safe_update_workspace(
        ledger=ledger,
        backend_url=backend_url,
        workspace_id=workspace_id,
        title=title,
        description=description,
        status=status,
    )
    safe_update_issue(
        ledger=ledger,
        mcp_cmd=mcp_cmd,
        backend_url=backend_url,
        project_id=project_id,
        issue_id=issue_id,
        title=title,
        description=description,
        status=status,
    )


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    ledger = start_run(
        "vibe-kanban-runner",
        repo=repo,
        cwd=repo,
        run_id=args.run_id,
        run_dir=run_dir,
    )
    final_message_path = ledger.artifact_path("codex-exec.final-message.md")
    stdout_path = ledger.artifact_path("codex-exec.stdout.jsonl")
    stderr_path = ledger.artifact_path("codex-exec.stderr.txt")
    command_path = ledger.artifact_path("codex-exec.command.json")
    parent_transcript_path = (
        Path(args.parent_transcript_path).expanduser().resolve()
        if args.parent_transcript_path
        else None
    )

    original_prompt = prompt_path.read_text(encoding="utf-8")
    startup_prepare = None
    if args.startup_prepare_metadata:
        startup_prepare_path = Path(args.startup_prepare_metadata).expanduser().resolve()
        startup_prepare_payload = json.loads(startup_prepare_path.read_text(encoding="utf-8"))
        if isinstance(startup_prepare_payload, dict):
            startup_prepare = startup_prepare_payload
    prompt = build_headless_prompt(
        original_prompt=original_prompt,
        repo=repo,
        run_id=args.run_id,
        run_dir=run_dir,
        prompt_path=prompt_path,
        parent_session_id=args.parent_session_id,
        parent_transcript_path=parent_transcript_path,
        startup_prepare=startup_prepare,
    )
    headless_prompt_path = ledger.artifact("codex-exec.prompt.md", prompt)
    command = build_codex_command(args, final_message_path)
    ledger.artifact(
        "codex-exec.command.json",
        {
            "command": command,
            "repo": str(repo),
            "prompt_file": str(prompt_path),
            "headless_prompt_file": headless_prompt_path,
            "parent_transcript_path": str(parent_transcript_path) if parent_transcript_path is not None else None,
            "run_dir": str(run_dir),
            "startup_prepare_metadata": args.startup_prepare_metadata,
        },
    )
    ledger.event(
        phase="fork",
        event="codex_exec_started",
        status="started",
        reason_code="codex_exec_started",
        parent_thread_id=args.parent_session_id,
        paths={
            "prompt": str(prompt_path),
            "headless_prompt": headless_prompt_path,
            "command": str(command_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "final_message": str(final_message_path),
        },
    )
    safe_update_management_record(
        ledger=ledger,
        mcp_cmd=args.mcp_cmd,
        backend_url=args.backend_url,
        project_id=args.vibe_project_id,
        issue_id=args.vibe_issue_id,
        workspace_id=args.vibe_workspace_id,
        title=args.issue_title,
        description=issue_description(
            status="running",
            repo=repo,
            parent_session_id=args.parent_session_id,
            parent_transcript_path=parent_transcript_path,
            run_dir=run_dir,
            final_message_path=final_message_path,
        ),
        status="running",
    )

    env = build_child_env(
        base_env=os.environ,
        ledger_env=ledger.env(),
        parent_session_id=args.parent_session_id,
        parent_transcript_path=parent_transcript_path,
    )

    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr_handle:
        completed = subprocess.Popen(
            command,
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env=env,
        )
        completed.communicate(prompt)
    duration_ms = int((time.monotonic() - started) * 1000)

    if completed.returncode == 0:
        status = "completed"
        reason_code = "codex_exec_completed"
        message = "Vibe-Kanban managed RVF codex exec completed."
        event = "codex_exec_completed"
    elif completed.returncode < 0:
        status = "cancelled"
        reason_code = "codex_exec_cancelled"
        message = "Vibe-Kanban managed RVF codex exec was cancelled."
        event = "codex_exec_cancelled"
    else:
        status = "failed"
        reason_code = "codex_exec_failed"
        message = "Vibe-Kanban managed RVF codex exec failed."
        event = "codex_exec_failed"

    ledger.event(
        phase="fork",
        event=event,
        status=status,
        reason_code=reason_code,
        duration_ms=duration_ms,
        parent_thread_id=args.parent_session_id,
        paths={
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "final_message": str(final_message_path),
            "headless_prompt": headless_prompt_path,
        },
        returncode=completed.returncode,
    )
    ledger.summary(
        status=f"vibe-kanban-rvf-{status}",
        reason_code=reason_code,
        message=message,
        repo=str(repo),
        cwd=str(repo),
        parent_thread_id=args.parent_session_id,
        parent_transcript_path=str(parent_transcript_path) if parent_transcript_path is not None else None,
        issue_title=args.issue_title,
        vibe_project_id=args.vibe_project_id,
        vibe_issue_id=args.vibe_issue_id,
        vibe_workspace_id=args.vibe_workspace_id,
        vibe_backend_url=args.backend_url,
        returncode=completed.returncode,
        paths={
            "prompt": str(prompt_path),
            "headless_prompt": headless_prompt_path,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "final_message": str(final_message_path),
        },
    )
    safe_update_management_record(
        ledger=ledger,
        mcp_cmd=args.mcp_cmd,
        backend_url=args.backend_url,
        project_id=args.vibe_project_id,
        issue_id=args.vibe_issue_id,
        workspace_id=args.vibe_workspace_id,
        title=args.issue_title,
        description=issue_description(
            status=status,
            repo=repo,
            parent_session_id=args.parent_session_id,
            parent_transcript_path=parent_transcript_path,
            run_dir=run_dir,
            final_message_path=final_message_path,
            returncode=completed.returncode,
        ),
        status=status,
    )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Vibe-Kanban issue 管理下运行 headless RVF。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--parent-session-id", required=True)
    parser.add_argument("--parent-transcript-path")
    parser.add_argument("--vibe-project-id")
    parser.add_argument("--vibe-issue-id")
    parser.add_argument("--vibe-workspace-id")
    parser.add_argument("--issue-title")
    parser.add_argument("--startup-prepare-metadata")
    parser.add_argument("--mcp-cmd", default=os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_MCP_CMD))
    parser.add_argument("--backend-url", default=os.environ.get("CODEX_RVF_VK_BACKEND_URL") or os.environ.get("VIBE_BACKEND_URL"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_RVF_CODEX_BIN", "codex"))
    parser.add_argument(
        "--codex-exec-args",
        default=os.environ.get("CODEX_RVF_CODEX_EXEC_ARGS", DEFAULT_CODEX_EXEC_ARGS),
    )
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        run_dir = Path(args.run_dir).expanduser()
        ledger = start_run(
            "vibe-kanban-runner",
            repo=args.repo,
            cwd=args.repo,
            run_id=args.run_id,
            run_dir=run_dir,
        )
        ledger.event(
            phase="fork",
            event="runner_failed",
            status="failed",
            reason_code="vibe_kanban_runner_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="vibe-kanban-rvf-failed",
            reason_code="vibe_kanban_runner_failed",
            message=f"Vibe-Kanban RVF runner failed: {type(exc).__name__}: {exc}",
        )
        safe_update_management_record(
            ledger=ledger,
            mcp_cmd=args.mcp_cmd,
            backend_url=args.backend_url,
            project_id=args.vibe_project_id,
            issue_id=args.vibe_issue_id,
            workspace_id=args.vibe_workspace_id,
            title=args.issue_title,
            description=issue_description(
                status="failed",
                repo=Path(args.repo).expanduser(),
                parent_session_id=args.parent_session_id,
                parent_transcript_path=(
                    Path(args.parent_transcript_path).expanduser()
                    if args.parent_transcript_path
                    else None
                ),
                run_dir=run_dir,
                error=f"{type(exc).__name__}: {exc}",
            ),
            status="failed",
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
