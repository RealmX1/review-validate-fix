#!/usr/bin/env python3
"""RVF finalize 后的 ``$rvf-analyze`` 后台线程派发。

这个模块只处理 finalize 已经生成 deterministic analysis scaffold 之后的收尾：
把 analyze 的 LLM 补全步骤通过
``rvf_analyze_thread.launch_detached_analyze_thread`` 派进一个 detached
tmux 线程后台运行。它不直接运行 ``rvf_analyze.py``，也不在当前会话内同步触发
LLM skill —— 原会话/task finalize 完即可 idle，无需等待 analyze 完成。

detached analyze 线程靠注入的 ``CODEX_RVF_SUPPRESS_STOP_HOOK`` /
``CODEX_RVF_ANALYZE_THREAD`` env 守卫在自己那次 Stop 时自抑制，无需再 arm 任何
task 级 quiet marker 来挡父会话——父会话下一轮真实改动的 Stop 应正常触发 RVF。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


RVF_ANALYZE_FOLLOWUP_MARKER = "RVF_KANBAN_ANALYZE_TRIGGER"

from rvf_analyze_thread import launch_detached_analyze_thread


# 与 codex_stop_review_validate_fix.SESSION_PATH_KEYS 保持一致，便于 advisory 在
# 没有显式 session_id 字段时回退到 transcript 文件解析。Inline 一份是为了避免
# advisory 反向 import codex_stop_review_validate_fix 造成循环（主 hook 已经
# import 本模块）。
_SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


def _session_id_from_transcript(path: Path) -> str | None:
    """从 Codex 风格 jsonl 头部 session_meta 中解析 session id。

    与 codex_stop_review_validate_fix.session_id_from_path 等价：最多扫描前 20
    行，找到 ``type == "session_meta"`` 即返回 ``payload.id``。Claude Code
    transcript 通常没有 session_meta 头，此函数会返回 None，由调用方决定后续
    行为（fail-open）。

    除 ``OSError`` 外，还会吞掉 ``UnicodeDecodeError``：dispatcher 的
    session_manifest_failed 测试会传入 ``\\xff`` 二进制 transcript 作为
    fixture，advisory 在 dispatcher quiet-gate 解析 session_id 时不应抛 utf-8
    异常打断后续守护流程。
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                try:
                    line = handle.readline()
                except UnicodeDecodeError:
                    return None
                if not line:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    value = payload.get("id")
                    if isinstance(value, str) and value:
                        return value
                return None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def parent_session_id_from_event(event: dict[str, Any]) -> str | None:
    """从 Stop hook event 中提取 parent session id，覆盖 codex 与 claude-code 命名。

    解析顺序与 codex_stop_review_validate_fix.session_hook_id_from_event 保持
    对称：先尝试 event 字段（claude-code Stop hook 通常带 session_id；codex 各
    种 thread/parent/conversation 别名也覆盖），再回退到 transcript 路径里的
    ``session_meta.id``，让 advisory 写 marker 时与主 hook 读 marker 时使用同
    一份 key，避免 key 不一致导致的 marker miss。
    """
    candidates = (
        "session_id",
        "sessionId",
        "thread_id",
        "threadId",
        "parent_session_id",
        "parent_thread_id",
        "parentSessionId",
        "parentThreadId",
        "conversation_id",
        "conversationId",
    )
    for key in candidates:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # transcript fallback —— 与主 hook 的 session_id_from_event /
    # parent_thread_id_from_event 行为一致：扫 event 中所有 session path 字段，
    # 第一个能解析出 session_meta.id 的 transcript 文件即返回。
    for key in _SESSION_PATH_KEYS:
        raw = event.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            expanded = Path(raw).expanduser()
        except (OSError, ValueError):
            continue
        session_id = _session_id_from_transcript(expanded)
        if session_id:
            return session_id
    return None


def _event_or_env_text(
    event: dict[str, Any],
    env_keys: tuple[str, ...],
    event_keys: tuple[str, ...],
) -> str | None:
    for key in env_keys:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    for key in event_keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def current_kanban_task_id(event: dict[str, Any]) -> str | None:
    return _event_or_env_text(
        event,
        ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"),
        ("kanban_task_id", "kanbanTaskId", "task_id", "taskId"),
    )


def current_kanban_attempt_id(event: dict[str, Any]) -> str | None:
    return _event_or_env_text(
        event,
        ("KANBAN_ATTEMPT_ID", "CLINE_KANBAN_ATTEMPT_ID"),
        ("kanban_attempt_id", "kanbanAttemptId", "attempt_id", "attemptId"),
    )


def _analysis_payload(finalize_record: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(finalize_record, dict):
        return None
    analysis = finalize_record.get("analysis")
    if not isinstance(analysis, dict):
        return None
    run_dir = finalize_record.get("run_dir")
    summary_md_path = analysis.get("summary_md_path")
    causality_json_path = analysis.get("causality_json_path")
    if not all(
        isinstance(value, str) and value
        for value in (run_dir, summary_md_path, causality_json_path)
    ):
        return None
    return {
        "run_dir": str(Path(run_dir).expanduser().resolve()),
        "summary_md_path": summary_md_path,
        "causality_json_path": causality_json_path,
    }


def rvf_analyze_trigger(run_dir: str) -> str:
    return f"$rvf-analyze {run_dir}"


def rvf_analyze_followup_prompt(analysis: dict[str, str]) -> str:
    trigger = rvf_analyze_trigger(analysis["run_dir"])
    return (
        f"{trigger}\n\n"
        f"{RVF_ANALYZE_FOLLOWUP_MARKER}\n"
        f"RVF_ANALYZE_RUN_DIR: {analysis['run_dir']}\n"
        f"RVF_ANALYZE_SUMMARY_MD: {analysis['summary_md_path']}\n"
        f"RVF_ANALYZE_CAUSALITY_JSON: {analysis['causality_json_path']}\n\n"
        "这是 RVF finalize 在生成 deterministic analysis scaffold 后注入的 "
        "$rvf-analyze follow-up。只复盘这个已 finalize 的 run：补全 "
        "`artifacts/analysis/summary.md`，并在 "
        "`artifacts/analysis/causality.json` 中填写 issue 到 patch call_id "
        "的候选归因。不要启动新的 `$review-validate-fix`，不要修改被审查源码，"
        "不要生成新的 handoff。"
    )


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_summary(ledger: Any, fields: dict[str, Any]) -> None:
    summary_path = getattr(ledger, "summary_path", None)
    if summary_path is None:
        return
    path = Path(summary_path)
    payload = _read_json_dict(path)
    payload.update(fields)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _append_system_message(payload: dict[str, Any], note: str) -> None:
    message = payload.get("systemMessage")
    if isinstance(message, str) and note not in message:
        payload["systemMessage"] = f"{message}; {note}"


def surface_rvf_analyze_advisory(
    *,
    event: dict[str, Any],
    ledger: Any,
    payload: dict[str, Any],
    finalize_record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """finalize handoff 完成后，把 ``$rvf-analyze`` 派进 detached tmux 后台线程。

    不再向当前会话/Kanban task 注入 follow-up 用户消息：直接调
    ``launch_detached_analyze_thread`` 在 ``rvf-analyze-<run_name>`` tmux session
    里 detached 跑 analyze agent，原会话 finalize 完即可 idle。手动 ``$rvf-analyze``
    路径不受影响。

    detached 线程靠注入的 ``CODEX_RVF_SUPPRESS_STOP_HOOK`` /
    ``CODEX_RVF_ANALYZE_THREAD`` env 守卫在自己那次 Stop 自抑制，不再 arm 任何
    task 级 quiet marker——父会话下一轮真实改动会正常触发 RVF。launch 失败按
    fail-open 处理（``thread-launch-failed``），不阻断 handoff，用户可手动
    ``$rvf-analyze`` 收尾。
    """
    analysis = _analysis_payload(finalize_record)
    if analysis is None:
        return None

    trigger = rvf_analyze_trigger(analysis["run_dir"])
    base_fields: dict[str, Any] = {
        "rvf_analyze_run_dir": analysis["run_dir"],
        "rvf_analyze_summary_md_path": analysis["summary_md_path"],
        "rvf_analyze_causality_json_path": analysis["causality_json_path"],
        "rvf_analyze_trigger": trigger,
    }
    task_id = current_kanban_task_id(event)
    attempt_id = current_kanban_attempt_id(event)

    try:
        thread_info = launch_detached_analyze_thread(
            event=event,
            ledger=ledger,
            analysis=analysis,
            finalize_record=finalize_record,
        )
    except Exception as exc:  # noqa: BLE001 - 启动失败绝不阻断 finalize/handoff。
        thread_info = {
            "launch_status": "launch_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    launch_status = thread_info.get("launch_status")
    launched = launch_status in ("launched", "already_running")

    thread_fields: dict[str, Any] = {
        "rvf_analyze_thread_launch_status": launch_status,
        "rvf_analyze_thread_session": thread_info.get("tmux_session"),
        "rvf_analyze_thread_host": thread_info.get("host"),
        "rvf_analyze_thread_status_path": thread_info.get("status_path"),
        "rvf_analyze_thread_log_path": thread_info.get("log_path"),
        "rvf_analyze_thread_prompt_path": thread_info.get("prompt_path"),
        "rvf_analyze_thread_command": thread_info.get("command"),
        "rvf_analyze_thread_returncode": thread_info.get("returncode"),
    }
    if task_id:
        thread_fields["rvf_analyze_kanban_task_id"] = task_id
    if attempt_id:
        thread_fields["rvf_analyze_kanban_attempt_id"] = attempt_id

    if launched:
        fields = {
            **base_fields,
            "rvf_analyze_status": "thread-launched",
            **thread_fields,
        }
        ledger.event(
            phase="analysis",
            event="rvf_analyze_thread_launched",
            status="completed",
            reason_code="rvf_analyze_thread_launched",
            **fields,
        )
        _merge_summary(ledger, fields)
        _append_system_message(payload, "rvf_analyze=thread_launched")
        return fields

    error = thread_info.get("error")
    fields = {
        **base_fields,
        "rvf_analyze_status": "thread-launch-failed",
        "rvf_analyze_error": error,
        **thread_fields,
    }
    ledger.event(
        phase="analysis",
        event="rvf_analyze_thread_launch_failed",
        status="warning",
        reason_code="rvf_analyze_thread_launch_failed",
        level="warn",
        error=error,
        **fields,
    )
    _merge_summary(ledger, fields)
    _append_system_message(payload, f"rvf_analyze=manual_required; trigger={trigger}")
    return fields
