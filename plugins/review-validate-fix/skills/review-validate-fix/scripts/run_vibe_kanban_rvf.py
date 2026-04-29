#!/usr/bin/env python3
from __future__ import annotations

import argparse
import codecs
from collections import deque
import json
import os
import select
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, start_run
from vibe_kanban_mcp_client import DEFAULT_MCP_CMD, update_issue, update_local_workspace, upsert_workspace_notes


DEFAULT_CODEX_EXEC_ARGS = "exec --json --dangerously-bypass-approvals-and-sandbox"
SCRIPT_DIR = Path(__file__).resolve().parent
HEADLESS_MARKER = "RVF_HEADLESS_REVIEW_VALIDATE_FIX"
SUPPRESS_STOP_HOOK_MARKER = "CODEX_RVF_SUPPRESS_STOP_HOOK=1"
DEFAULT_PROGRESS_UPDATE_INTERVAL_SECONDS = 30.0
MAX_PROGRESS_EVENTS = 8
MAX_PROGRESS_TEXT_CHARS = 180


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


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_timestamp_after(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(0.0, seconds)))


def elapsed_seconds(started_monotonic: float) -> int:
    return max(0, int(time.monotonic() - started_monotonic))


def format_elapsed(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def clip_text(value: str, limit: int = MAX_PROGRESS_TEXT_CHARS) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def first_text_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            found = first_text_value(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ("message", "content", "text", "summary", "output", "error", "details"):
            found = first_text_value(value.get(key))
            if found:
                return found
        item = value.get("item")
        if isinstance(item, dict):
            found = first_text_value(item)
            if found:
                return found
        payload = value.get("payload")
        if isinstance(payload, dict):
            return first_text_value(payload)
    return None


def short_command(command: Any) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    text = command.strip()
    for prefix in ('/bin/zsh -lc "', "zsh -lc '", "bash -lc '", "sh -lc '"):
        if text.startswith(prefix) and text.endswith(prefix[-1]):
            return text[len(prefix) : -1]
    return text


def summarize_codex_item_event(payload: dict[str, Any]) -> str | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    event_type = str(payload.get("type") or "item")
    item_type = str(item.get("type") or "item")
    status = item.get("status")
    status_text = f" {status}" if isinstance(status, str) and status.strip() else ""
    if item_type == "agent_message":
        text = first_text_value(item)
        if text:
            return clip_text(f"assistant: {text}")
    if item_type == "command_execution":
        command = short_command(item.get("command")) or "<unknown command>"
        exit_code = item.get("exit_code")
        if event_type.endswith(".started"):
            return clip_text(f"command started: {command}")
        if exit_code is not None:
            return clip_text(f"command exited {exit_code}: {command}")
        return clip_text(f"command{status_text}: {command}")
    if item_type == "collab_tool_call":
        tool = item.get("tool") or "collab"
        receivers = item.get("receiver_thread_ids")
        receiver_count = len(receivers) if isinstance(receivers, list) else 0
        if event_type.endswith(".started"):
            return clip_text(f"{tool} started: {receiver_count} agent(s)")
        return clip_text(f"{tool}{status_text}: {receiver_count} agent(s)")
    item_id = item.get("id")
    suffix = f" {item_id}" if isinstance(item_id, str) and item_id.strip() else ""
    return clip_text(f"{event_type}: {item_type}{status_text}{suffix}")


def event_label(payload: dict[str, Any]) -> str:
    for key in ("event", "type", "status", "name", "role"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return event_label(nested)
    return "codex stdout event"


def infer_phase(text: str) -> str | None:
    lowered = text.lower()
    phase_markers = [
        ("handoff", ("handoff", "rvf_handoff_file")),
        ("validate/fix", ("validate/fix", "validate then fix", "fixer", "修复")),
        ("validate", ("validate", "validation", "验证")),
        ("review", ("review", "reviewer", "审查", "检查")),
        ("prepare", ("prepare", "preparing", "scope-of-work", "review packet", "准备")),
        ("finalizing", ("final", "completed", "complete", "done", "总结")),
    ]
    for phase, markers in phase_markers:
        if any(marker in lowered for marker in markers):
            return phase
    return None


class ProgressState:
    def __init__(self, *, started_monotonic: float) -> None:
        self.started_monotonic = started_monotonic
        self.last_update_timestamp: str | None = None
        self.next_update_timestamp = "<unknown>"
        self.current_phase = "running"
        self.current_activity = "codex exec started"
        self.recent_events: deque[str] = deque(maxlen=MAX_PROGRESS_EVENTS)
        self.stdout_lines = 0
        self.malformed_lines = 0
        self.last_raw_event: str | None = None

    def record_line(self, line: str) -> bool:
        self.stdout_lines += 1
        text = line.strip()
        if not text:
            return False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.malformed_lines += 1
            summary = f"malformed stdout: {clip_text(text)}"
            self.current_activity = summary
            self.recent_events.append(summary)
            self.last_raw_event = text
            return False
        if isinstance(payload, dict):
            summary = self._summarize_payload(payload)
        else:
            summary = clip_text(str(payload))
        old_phase = self.current_phase
        phase = infer_phase(summary)
        if phase:
            self.current_phase = phase
        self.current_activity = summary
        self.recent_events.append(summary)
        self.last_raw_event = text
        important = self.current_phase != old_phase
        lowered = summary.lower()
        return important or any(marker in lowered for marker in ("error", "failed", "cancelled", "handoff", "final"))

    def _summarize_payload(self, payload: dict[str, Any]) -> str:
        item_summary = summarize_codex_item_event(payload)
        if item_summary:
            return item_summary
        label = event_label(payload)
        nested = payload.get("payload")
        payload_dict = nested if isinstance(nested, dict) else payload
        name = payload_dict.get("name")
        role = payload_dict.get("role")
        text = first_text_value(payload)
        parts = [str(label)]
        if isinstance(role, str) and role.strip() and role not in parts:
            parts.append(role.strip())
        if isinstance(name, str) and name.strip() and name not in parts:
            parts.append(name.strip())
        if text and text not in parts:
            parts.append(text)
        return clip_text(": ".join(parts))

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.current_phase,
            "elapsed": format_elapsed(elapsed_seconds(self.started_monotonic)),
            "last_update": self.last_update_timestamp,
            "next_update": self.next_update_timestamp,
            "current_activity": self.current_activity,
            "recent_events": list(self.recent_events),
            "stdout_lines": self.stdout_lines,
            "malformed_stdout_lines": self.malformed_lines,
        }


def issue_description(
    *,
    status: str,
    repo: Path,
    parent_session_id: str,
    parent_transcript_path: Path | None,
    run_dir: Path,
    final_message_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    progress: ProgressState | None = None,
    returncode: int | None = None,
    error: str | None = None,
) -> str:
    transcript = str(parent_transcript_path) if parent_transcript_path is not None else "<unknown>"
    snapshot = progress.snapshot() if progress is not None else {}
    lines = [
        f"status: {status}",
        f"phase: {snapshot.get('phase', status)}",
        f"elapsed: {snapshot.get('elapsed', '<unknown>')}",
        f"last update: {snapshot.get('last_update') or utc_timestamp()}",
        f"next update: {snapshot.get('next_update') or '<unknown>'}",
        f"current activity: {snapshot.get('current_activity') or '<none recorded>'}",
        "recent events:",
    ]
    recent_events = snapshot.get("recent_events")
    if isinstance(recent_events, list) and recent_events:
        lines.extend(f"- {event}" for event in recent_events[-MAX_PROGRESS_EVENTS:])
    else:
        lines.append("- <none recorded>")
    lines.extend([
        f"target repo: {repo}",
        f"parent session id: {parent_session_id}",
        f"parent transcript path: {transcript}",
        f"run_dir: {run_dir}",
        f"events.jsonl: {run_dir / 'events.jsonl'}",
        f"summary.json: {run_dir / 'summary.json'}",
        f"stdout: {stdout_path or run_dir / 'artifacts' / 'codex-exec.stdout.jsonl'}",
        f"stderr: {stderr_path or run_dir / 'artifacts' / 'codex-exec.stderr.txt'}",
        f"review-env.sh: {run_dir / 'artifacts' / 'review-env.sh'}",
        f"review-agent-context.md: {run_dir / 'artifacts' / 'review-agent-context.md'}",
        f"handoff.md: {run_dir / 'artifacts' / 'handoff.md'}",
    ])
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


def safe_update_progress_record(
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
) -> bool:
    sent = False
    errors: list[str] = []
    if backend_url and workspace_id:
        try:
            payload = upsert_workspace_notes(
                backend_url=backend_url,
                workspace_id=workspace_id,
                description=description,
            )
            ledger.artifact(f"vibe-kanban-workspace-progress-{status}.json", payload or {}, unique=True)
            sent = True
        except Exception as exc:
            errors.append(f"workspace={type(exc).__name__}: {exc}")
    if project_id and issue_id:
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
            ledger.artifact(f"vibe-kanban-issue-progress-{status}.json", payload, unique=True)
            sent = True
        except Exception as exc:
            errors.append(f"issue={type(exc).__name__}: {exc}")
    if errors:
        ledger.event(
            phase="fork",
            event="vibe_kanban_progress_update_failed",
            status="warn",
            reason_code="vibe_kanban_progress_update_failed",
            level="warn",
            error="; ".join(errors),
        )
    if sent:
        ledger.event(
            phase="fork",
            event="vibe_kanban_progress_update_sent",
            status="running",
            reason_code="vibe_kanban_progress_update_sent",
        )
    return sent


def parse_progress_interval(value: str | None) -> float:
    if value is None or not value.strip():
        return DEFAULT_PROGRESS_UPDATE_INTERVAL_SECONDS
    try:
        return max(0.0, float(value))
    except ValueError:
        return DEFAULT_PROGRESS_UPDATE_INTERVAL_SECONDS


def write_prompt(process: subprocess.Popen[bytes], prompt: str) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
    except BrokenPipeError:
        return


def run_codex_exec_streaming(
    *,
    command: list[str],
    repo: Path,
    prompt: str,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    progress: ProgressState,
    progress_interval_seconds: float,
    progress_update: Any,
) -> subprocess.Popen[bytes]:
    last_progress_update = time.monotonic()
    pending_text = ""
    decoder = codecs.getincrementaldecoder("utf-8")()

    def handle_stdout_text(text: str, *, final: bool = False) -> bool:
        nonlocal pending_text
        important_seen = False
        pending_text += text
        lines = pending_text.splitlines(keepends=True)
        if lines and not final and not lines[-1].endswith(("\n", "\r")):
            pending_text = lines.pop()
        else:
            pending_text = ""
        for line in lines:
            stdout_handle.write(line)
            important_seen = progress.record_line(line) or important_seen
        if lines:
            stdout_handle.flush()
        return important_seen

    def read_available_stdout() -> bool:
        if process.stdout is None:
            return False
        important_seen = False
        while True:
            try:
                chunk = os.read(process.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            important_seen = handle_stdout_text(decoder.decode(chunk), final=False) or important_seen
        return important_seen

    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            bufsize=0,
            env=dict(env),
        )
        prompt_writer = threading.Thread(target=write_prompt, args=(process, prompt), daemon=True)
        prompt_writer.start()
        if process.stdout is None:
            process.wait()
            prompt_writer.join(timeout=1.0)
            return process
        os.set_blocking(process.stdout.fileno(), False)

        while True:
            ready, _, _ = select.select([process.stdout.fileno()], [], [], 1.0)
            if ready:
                important = read_available_stdout()
                if progress_interval_seconds > 0 and (
                    important or time.monotonic() - last_progress_update >= progress_interval_seconds
                ):
                    progress_update()
                    last_progress_update = time.monotonic()
                if process.poll() is not None:
                    break
            elif process.poll() is not None:
                break
            elif progress_interval_seconds > 0 and time.monotonic() - last_progress_update >= progress_interval_seconds:
                progress_update()
                last_progress_update = time.monotonic()

        process.wait()
        read_available_stdout()
        final_text = decoder.decode(b"", final=True)
        if final_text or pending_text:
            handle_stdout_text(final_text, final=True)
        prompt_writer.join(timeout=1.0)
        return process


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
    started = time.monotonic()
    progress = ProgressState(started_monotonic=started)
    progress_interval_seconds = parse_progress_interval(str(args.progress_update_interval_seconds))
    progress.last_update_timestamp = utc_timestamp()
    progress.next_update_timestamp = (
        utc_timestamp_after(progress_interval_seconds)
        if progress_interval_seconds > 0
        else "disabled"
    )

    def build_description(status: str, *, returncode: int | None = None, error: str | None = None) -> str:
        return issue_description(
            status=status,
            repo=repo,
            parent_session_id=args.parent_session_id,
            parent_transcript_path=parent_transcript_path,
            run_dir=run_dir,
            final_message_path=final_message_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            progress=progress,
            returncode=returncode,
            error=error,
        )

    def send_progress_update() -> None:
        progress.last_update_timestamp = utc_timestamp()
        progress.next_update_timestamp = (
            utc_timestamp_after(progress_interval_seconds)
            if progress_interval_seconds > 0
            else "disabled"
        )
        safe_update_progress_record(
            ledger=ledger,
            mcp_cmd=args.mcp_cmd,
            backend_url=args.backend_url,
            project_id=args.vibe_project_id,
            issue_id=args.vibe_issue_id,
            workspace_id=args.vibe_workspace_id,
            title=args.issue_title,
            description=build_description("running"),
            status="running",
        )

    safe_update_management_record(
        ledger=ledger,
        mcp_cmd=args.mcp_cmd,
        backend_url=args.backend_url,
        project_id=args.vibe_project_id,
        issue_id=args.vibe_issue_id,
        workspace_id=args.vibe_workspace_id,
        title=args.issue_title,
        description=build_description("running"),
        status="running",
    )

    env = build_child_env(
        base_env=os.environ,
        ledger_env=ledger.env(),
        parent_session_id=args.parent_session_id,
        parent_transcript_path=parent_transcript_path,
    )

    completed = run_codex_exec_streaming(
        command=command,
        repo=repo,
        prompt=prompt,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        progress=progress,
        progress_interval_seconds=progress_interval_seconds,
        progress_update=send_progress_update,
    )
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
    progress.last_update_timestamp = utc_timestamp()
    progress.next_update_timestamp = "none (terminal)"
    safe_update_management_record(
        ledger=ledger,
        mcp_cmd=args.mcp_cmd,
        backend_url=args.backend_url,
        project_id=args.vibe_project_id,
        issue_id=args.vibe_issue_id,
        workspace_id=args.vibe_workspace_id,
        title=args.issue_title,
        description=build_description(status, returncode=completed.returncode),
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
    parser.add_argument(
        "--progress-update-interval-seconds",
        type=float,
        default=parse_progress_interval(os.environ.get("CODEX_RVF_VK_PROGRESS_INTERVAL_SECONDS")),
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
