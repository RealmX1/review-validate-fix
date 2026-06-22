#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rvf_logging import RunLedger, rvf_state_fields, safe_token


HANDOFF_FILE_MARKER = "RVF_HANDOFF_FILE"
HANDOFF_FILE_RE = re.compile(
    rf"^\s*{re.escape(HANDOFF_FILE_MARKER)}\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
MARKDOWN_SUFFIXES = {".md", ".markdown"}

# 系统通知（OS notification）相关常量。
NOTIFY_TITLE = "RVF"
# terminal-notifier 是 OS 通知的硬依赖；可用此环境变量覆盖二进制路径，
# 测试用它注入假 notifier，同时绕过 darwin 平台门控以便跨平台 CI 也能验证命令构建。
TERMINAL_NOTIFIER_BIN_ENV = "CODEX_RVF_TERMINAL_NOTIFIER_BIN"
# Phase B（cline-kanban 内带按钮通知）外部触发命令；未配置时该路径恒为 no-op。
KANBAN_NOTIFY_CMD_ENV = "CODEX_RVF_KANBAN_NOTIFY_CMD"
# cline-kanban runtime 默认端口（与 cline_kanban_client.DEFAULT_RUNTIME_PORT 对齐的兜底）。
DEFAULT_KANBAN_PORT = 3484


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return None


def handoff_path_from_text(text: str | None) -> Path | None:
    if not isinstance(text, str):
        return None
    matches = HANDOFF_FILE_RE.findall(text)
    if not matches:
        return None
    raw = matches[-1].strip().strip("`\"'")
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    return Path(raw).expanduser() if raw else None


def latest_assistant_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue
                if record.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                message = _message_text(payload.get("content"))
                if message:
                    latest = message
    except (OSError, UnicodeDecodeError):
        return None
    return latest


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def handoff_path_from_event(event: dict[str, Any]) -> Path | None:
    direct = handoff_path_from_text(event.get("last_assistant_message"))
    if direct is not None:
        return direct
    for path in event_session_paths(event):
        candidate = handoff_path_from_text(latest_assistant_message(path))
        if candidate is not None:
            return candidate
    return None


def assistant_text_from_event(event: dict[str, Any]) -> str | None:
    """返回包含 RVF_HANDOFF_FILE marker 的那条 assistant 消息全文。

    与 ``handoff_path_from_event`` 同源（优先 event 内联消息，其次 transcript
    最后一条 assistant 消息），但返回原始文本而非路径，供剪贴板尾段切片使用。
    """
    direct = event.get("last_assistant_message")
    if isinstance(direct, str) and HANDOFF_FILE_RE.search(direct):
        return direct
    for path in event_session_paths(event):
        text = latest_assistant_message(path)
        if isinstance(text, str) and HANDOFF_FILE_RE.search(text):
            return text
    return None


def handoff_marker_tail(text: str | None) -> str | None:
    """从最后一个 ``RVF_HANDOFF_FILE:`` 出现处切到文末。

    得到「marker 行 + 1-3 句中文概括」，作为 Phase B 按钮的剪贴板文本。
    """
    if not isinstance(text, str):
        return None
    matches = list(HANDOFF_FILE_RE.finditer(text))
    if not matches:
        return None
    tail = text[matches[-1].start():].strip()
    return tail or None


def _notification_summary(marker_tail: str | None) -> str | None:
    """剔除 marker 行，剩下的 1-3 句概括作为 OS 通知正文。"""
    if not marker_tail:
        return None
    body_lines = [line for line in marker_tail.splitlines() if not HANDOFF_FILE_RE.match(line)]
    body = "\n".join(body_lines).strip()
    return body or None


def validate_handoff_path(path: Path) -> tuple[bool, str]:
    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
        return False, "not_markdown"
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False, "unresolvable"
    if not resolved.is_file():
        return False, "missing"
    return True, "ok"


def _handoff_path_digest(handoff_path: Path) -> str:
    return hashlib.sha256(str(handoff_path).encode("utf-8")).hexdigest()[:12]


def _kanban_context_for_handoff(handoff_path: Path) -> dict[str, Any] | None:
    origin_path = handoff_path.parent / "origin.json"
    if not origin_path.is_file():
        return None
    try:
        data = json.loads(origin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("source_kind") != "cline-kanban-task":
        return None
    task_id = data.get("kanban_task_id")
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    context: dict[str, Any] = {"kanban_task_id": task_id.strip()}
    for key in ("kanban_attempt_id", "kanban_task_title", "kanban_task_title_source"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            context[key] = value.strip()
    return context


def _write_json_marker(path: Path, payload: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return False, {
            "kind": "log_unavailable",
            "operation": "handoff_marker",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return True, None


# --- cline-kanban task URL 解析 --------------------------------------------


def _kebab_workspace_base(repo_path: str) -> str:
    """复刻 cline-kanban ``toWorkspaceIdBase``：basename 小写 + 非字母数字折叠为 ``-``。

    仅作 workspace index 缺失时的兜底；collision suffix 无法在此重建，因此该兜底
    只在「同 basename 仓库唯一」时与真实 workspaceId 一致。
    """
    name = Path(repo_path).name
    kebab = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return kebab or "workspace"


def _cline_workspace_index_path() -> Path:
    # cline-kanban runtime home：homedir()/.cline/kanban（见 workspace-state.ts）。
    return Path.home() / ".cline" / "kanban" / "workspaces" / "index.json"


def workspace_id_for_repo(repo_path: str) -> str:
    """父仓库路径 → cline-kanban workspaceId；index 缺失则 kebab basename 兜底。"""
    try:
        data = json.loads(_cline_workspace_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        mapping = data.get("repoPathToId")
        if isinstance(mapping, dict):
            workspace_id = mapping.get(repo_path)
            if isinstance(workspace_id, str) and workspace_id.strip():
                return workspace_id.strip()
    return _kebab_workspace_base(repo_path)


def _kanban_runtime_port() -> int:
    try:
        from cline_kanban_client import DEFAULT_RUNTIME_PORT, resolve_runtime_port
    except Exception:
        return DEFAULT_KANBAN_PORT
    try:
        return resolve_runtime_port(env=os.environ)
    except Exception:
        return DEFAULT_RUNTIME_PORT


def resolve_kanban_task_url(project_path: str | None, task_id: str | None) -> str | None:
    if not (project_path and task_id):
        return None
    workspace_id = workspace_id_for_repo(project_path)
    port = _kanban_runtime_port()
    return (
        f"http://127.0.0.1:{port}/"
        f"{quote(workspace_id, safe='')}?task={quote(task_id, safe='')}"
    )


def _parent_repo_from_cwd(cwd: str | os.PathLike[str] | None) -> str | None:
    """从 worktree/仓库 cwd 推出父仓库路径（git common-dir 的父目录）。"""
    if not cwd:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    common = completed.stdout.strip()
    if not common:
        return None
    path = Path(common)
    if path.name == ".git":
        return str(path.parent)
    return None


def _project_path_for_run(handoff_path: Path, cwd: str | os.PathLike[str] | None) -> str | None:
    """优先 origin.json 显式字段；否则用 git common-dir 从 cwd 推父仓库路径。"""
    origin_path = handoff_path.parent / "origin.json"
    try:
        data = json.loads(origin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        for key in ("kanban_project_path", "project_path", "repo"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return str(Path(value.strip()).expanduser())
    return _parent_repo_from_cwd(cwd)


# --- OS 通知 + Phase B 触发桩 ----------------------------------------------


def _terminal_notifier_bin() -> str | None:
    override = os.environ.get(TERMINAL_NOTIFIER_BIN_ENV)
    if override and override.strip():
        return override.strip()
    return shutil.which("terminal-notifier")


def notify_handoff_ready(
    *,
    handoff_path: Path,
    summary_text: str | None,
    task_url: str | None,
    group_ref: str | None,
) -> dict[str, Any]:
    """发一条 OS 系统通知（terminal-notifier）；kanban 来源带 ``-open <taskUrl>``。

    terminal-notifier 是硬依赖：缺失时返回显式 reason，由上层透出而非静默。
    显式 ``CODEX_RVF_TERMINAL_NOTIFIER_BIN`` 覆盖会绕过 darwin 门控（测试/自定义）。
    """
    override = os.environ.get(TERMINAL_NOTIFIER_BIN_ENV)
    if sys.platform != "darwin" and not (override and override.strip()):
        return {"notified": False, "reason": "unsupported-platform", "platform": sys.platform}
    notifier = _terminal_notifier_bin()
    if not notifier:
        return {"notified": False, "reason": "terminal-notifier-missing"}
    message = summary_text or f"Handoff 就绪：{handoff_path.name}"
    command = [notifier, "-title", NOTIFY_TITLE, "-message", message]
    if group_ref:
        command += ["-group", f"rvf-{group_ref}"]
    if task_url:
        command += ["-open", task_url]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "notified": False,
            "reason": "timeout",
            "command": command,
            "task_url": task_url,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "notified": False,
            "reason": "exec_failed",
            "command": command,
            "task_url": task_url,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "notified": completed.returncode == 0,
        "reason": "notified" if completed.returncode == 0 else "command_failed",
        "command": command,
        "returncode": completed.returncode,
        "task_url": task_url,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def notify_kanban_followup_stranded(
    *,
    task_id: str | None,
    task_title: str | None,
    task_url: str | None,
    reason: str,
) -> dict[str, Any]:
    """RVF self-rising follow-up review 未确认/滞留时的 OS 系统通知。

    用于两处：S1a（首次 ``dispatched-unconfirmed`` 立即提醒）与 S1b（任意会话 Stop 的
    跨 task stranded-sweep 升级提醒）。复用 ``notify_handoff_ready`` 的
    subprocess + ``-open <taskUrl>`` + ``timeout=10`` 形状；故意**不**改 notify_handoff_ready
    （其 blast radius 高），以零回归方式并存。terminal-notifier 缺失 / 非 darwin 时返回
    显式 reason，由上层透出而非静默假装已通知。``-group`` 按 task 合并，避免同一 task 的
    多次升级在通知中心堆叠刷屏。
    """
    override = os.environ.get(TERMINAL_NOTIFIER_BIN_ENV)
    if sys.platform != "darwin" and not (override and override.strip()):
        return {"notified": False, "reason": "unsupported-platform", "platform": sys.platform}
    notifier = _terminal_notifier_bin()
    if not notifier:
        return {"notified": False, "reason": "terminal-notifier-missing"}
    title_text = task_title.strip() if isinstance(task_title, str) else ""
    label = f"task {task_id} «{title_text}»" if title_text else f"task {task_id}"
    message = (
        f"RVF review 已排队但未确认运行（{reason}）：{label}。"
        "打开该 task，让排队中的 $review-validate-fix 被消费。"
    )
    command = [notifier, "-title", NOTIFY_TITLE, "-message", message]
    if task_id:
        command += ["-group", f"rvf-followup-{safe_token(str(task_id))}"]
    if task_url:
        command += ["-open", task_url]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "notified": False,
            "reason": "timeout",
            "command": command,
            "task_url": task_url,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "notified": False,
            "reason": "exec_failed",
            "command": command,
            "task_url": task_url,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "notified": completed.returncode == 0,
        "reason": "notified" if completed.returncode == 0 else "command_failed",
        "command": command,
        "returncode": completed.returncode,
        "task_url": task_url,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def maybe_trigger_kanban_notification(
    *,
    task_url: str | None,
    copy_text: str | None,
    kanban_context: dict[str, Any] | None,
    project_path: str | None,
) -> dict[str, Any]:
    """Phase B 触发桩：把一条带按钮的通知推进 cline-kanban UI。

    ``CODEX_RVF_KANBAN_NOTIFY_CMD`` 未配置时恒为 no-op（不报错）；配置后把 task
    上下文经 env + stdin(JSON) 交给该命令（Phase B 的 ``kanban task notify`` CLI）。
    与 cline-kanban 具体 flag 解耦，避免在尚未落地的 Phase B 上硬编码契约。
    """
    command = os.environ.get(KANBAN_NOTIFY_CMD_ENV)
    if not (command and command.strip()):
        return {"triggered": False, "reason": "kanban-notify-not-configured"}
    if not kanban_context:
        return {"triggered": False, "reason": "no-kanban-context"}
    task_id = kanban_context.get("kanban_task_id")
    workspace_id = workspace_id_for_repo(project_path) if project_path else None
    context_payload = {
        "task_id": task_id,
        "task_url": task_url,
        "copy_text": copy_text,
        "project_path": project_path,
        "workspace_id": workspace_id,
        "title": NOTIFY_TITLE,
    }
    env = {**os.environ}
    env.update(
        {
            "RVF_KANBAN_TASK_ID": task_id or "",
            "RVF_KANBAN_TASK_URL": task_url or "",
            "RVF_KANBAN_COPY_TEXT": copy_text or "",
            "RVF_KANBAN_PROJECT_PATH": project_path or "",
            "RVF_KANBAN_WORKSPACE_ID": workspace_id or "",
        }
    )
    try:
        completed = subprocess.run(
            shlex.split(command),
            input=json.dumps(context_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {
            "triggered": False,
            "reason": "exec_failed",
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "triggered": completed.returncode == 0,
        "reason": "triggered" if completed.returncode == 0 else "command_failed",
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _notified_marker_path(ledger: RunLedger, session_id: str, handoff_path: Path) -> Path:
    return (
        ledger.root
        / "handoff-notified"
        / f"{safe_token(session_id)}.{_handoff_path_digest(handoff_path)}.json"
    )


def handoff_completion_payload(
    event: dict[str, Any],
    ledger: RunLedger,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    handoff_path = handoff_path_from_event(event)
    if handoff_path is None:
        return None

    valid, reason = validate_handoff_path(handoff_path)
    if not valid:
        ledger.event(
            phase="handoff",
            event="handoff_file_marker_invalid",
            status="skipped",
            reason_code=f"handoff_file_{reason}",
            session_id=str(event.get("session_id") or "unknown-session"),
            paths={"handoff": str(handoff_path)},
        )
        return None

    resolved = handoff_path.expanduser().resolve()
    try:
        previous_summary = json.loads(ledger.summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_summary = {}
    if not isinstance(previous_summary, dict):
        previous_summary = {}
    previous_state_fields = {
        "backend": previous_summary.get("rvf_backend")
        if isinstance(previous_summary.get("rvf_backend"), str)
        else None,
        "backend_raw": previous_summary.get("rvf_backend_raw")
        if isinstance(previous_summary.get("rvf_backend_raw"), str)
        else None,
        "scope_contract_path": previous_summary.get("rvf_scope_contract_path"),
        "scope_of_work_path": previous_summary.get("rvf_scope_of_work_path"),
        "review_packet_path": previous_summary.get("rvf_review_packet_path"),
        "session_manifest_path": previous_summary.get("rvf_session_manifest_path"),
    }
    session_id = str(event.get("session_id") or "unknown-session")
    marker_path = _notified_marker_path(ledger, session_id, resolved)
    already_notified = marker_path.exists()

    kanban_context = _kanban_context_for_handoff(resolved)
    project_path = _project_path_for_run(resolved, cwd) if kanban_context else None
    task_url = (
        resolve_kanban_task_url(project_path, kanban_context["kanban_task_id"])
        if kanban_context
        else None
    )
    marker_tail = handoff_marker_tail(assistant_text_from_event(event))

    marker_written = False
    marker_error: dict[str, str] | None = None
    if already_notified:
        notify_result: dict[str, Any] = {"notified": False, "reason": "already_notified"}
        kanban_trigger: dict[str, Any] = {"triggered": False, "reason": "already_notified"}
    else:
        notify_result = notify_handoff_ready(
            handoff_path=resolved,
            summary_text=_notification_summary(marker_tail),
            task_url=task_url,
            group_ref=ledger.run_id or session_id,
        )
        kanban_trigger = maybe_trigger_kanban_notification(
            task_url=task_url,
            copy_text=marker_tail,
            kanban_context=kanban_context,
            project_path=project_path,
        )
        marker_payload: dict[str, Any] = {
            "session_id": session_id,
            "handoff_path": str(resolved),
            "notify_result": notify_result,
            "kanban_trigger": kanban_trigger,
        }
        if kanban_context:
            marker_payload["kanban"] = kanban_context
        if task_url:
            marker_payload["task_url"] = task_url
        marker_written, marker_error = _write_json_marker(marker_path, marker_payload)
        if marker_error is not None:
            ledger._diagnose("handoff_marker", OSError(marker_error["error"]))

    notify_reason = notify_result.get("reason")
    # handoff 文件本身已就绪即视为完成；通知是 best-effort。但 terminal-notifier
    # 缺失 / 命令失败属 warning（硬依赖缺失要显式可见），unsupported-platform 不算失败。
    notified_ok = (
        bool(notify_result.get("notified"))
        or already_notified
        or notify_reason == "unsupported-platform"
    )
    status = "completed" if notified_ok else "warning"
    ledger.event(
        phase="handoff",
        event="handoff_file_ready",
        status=status,
        reason_code="handoff_file_ready",
        level="info" if status == "completed" else "warn",
        session_id=session_id,
        paths={
            "handoff": str(resolved),
            "marker": str(marker_path),
        },
        handoff_notify_result=notify_result,
        handoff_task_url=task_url,
        kanban_trigger=kanban_trigger,
        marker_written=marker_written,
        marker_error=marker_error,
        already_notified=already_notified,
        **rvf_state_fields(
            phase="complete",
            **previous_state_fields,
            handoff_path=resolved,
            completion_gate="handoff_file_ready",
        ),
    )
    message = f"review-validate-fix run 已结束。Handoff: {resolved}"
    if notify_result.get("notified"):
        message += "。已发送系统通知。"
        if task_url:
            message += "点击通知可打开对应 Kanban task。"
    elif already_notified:
        message += "。此前已通知过该 handoff。"
    elif notify_reason == "unsupported-platform":
        message += "。当前平台不支持系统通知，已跳过。"
    elif notify_reason == "terminal-notifier-missing":
        message += "。未找到 terminal-notifier，无法发送系统通知；请运行 `brew install terminal-notifier`。"
    else:
        message += f"。系统通知失败（{notify_reason}），详情见 summary。"
    return ledger.hook_payload(
        status="handoff-advisory",
        reason_code="handoff_file_ready",
        message=message,
        detail=str(resolved),
        handoff_path=str(resolved),
        handoff_notify_result=notify_result,
        handoff_task_url=task_url,
        kanban_trigger=kanban_trigger,
        marker_path=str(marker_path),
        marker_written=marker_written,
        marker_error=marker_error,
        already_notified=already_notified,
        **rvf_state_fields(
            phase="complete",
            **previous_state_fields,
            handoff_path=resolved,
            completion_gate="handoff_file_ready",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send an OS notification for an RVF handoff markdown file."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    notify_parser = subparsers.add_parser(
        "notify", help="Send an OS notification announcing a ready handoff file."
    )
    notify_parser.add_argument("path", help="Path to handoff.md.")
    notify_parser.add_argument("--task-url", default=None, help="Kanban task URL for click-to-open.")
    notify_parser.add_argument("--summary", default=None, help="Short notification body text.")
    notify_parser.add_argument("--group", default=None, help="terminal-notifier -group ref.")
    args = parser.parse_args(argv)

    if args.command == "notify":
        handoff_path = Path(args.path).expanduser()
        valid, reason = validate_handoff_path(handoff_path)
        if not valid:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "handoff_path": str(handoff_path),
                        "notified": False,
                        "reason": f"handoff_file_{reason}",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        resolved = handoff_path.resolve()
        result = notify_handoff_ready(
            handoff_path=resolved,
            summary_text=args.summary,
            task_url=args.task_url,
            group_ref=args.group,
        )
        print(json.dumps({"valid": True, "handoff_path": str(resolved), **result}, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
