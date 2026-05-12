#!/usr/bin/env python3
"""RVF finalize 后的 ``$rvf-analyze`` follow-up 提示/注入。

这个模块只处理 finalize 已经生成 deterministic analysis scaffold 之后的
用户可见提醒或 Cline Kanban 原生 task 内 follow-up 注入；它不直接运行
``rvf_analyze.py``，也不触发 LLM skill。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CLINE_KANBAN_CLIENT = SKILL_DIR / "scripts" / "cline_kanban_client.py"
DEFAULT_CLINE_KANBAN_TASK_CMD = "kanban task"
RVF_ANALYZE_FOLLOWUP_MARKER = "RVF_KANBAN_ANALYZE_TRIGGER"

from post_analyze_quiet import write_post_analyze_quiet_marker


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


def _arm_post_analyze_quiet_marker_safe(
    *,
    event: dict[str, Any],
    ledger: Any,
    finalize_record: dict[str, Any] | None,
    analysis: dict[str, str],
    task_id: str | None,
    attempt_id: str | None,
) -> dict[str, Any]:
    """写一次性 quiet marker。返回 ledger event 用 metadata；失败也不抛。"""
    session_id = parent_session_id_from_event(event)
    armed_run_id = getattr(ledger, "run_id", None) or "unknown"
    handoff_path = None
    if isinstance(finalize_record, dict):
        candidate = finalize_record.get("handoff_path") or finalize_record.get("handoff")
        if isinstance(candidate, str) and candidate:
            handoff_path = candidate
    try:
        marker_path = write_post_analyze_quiet_marker(
            task_id=task_id,
            session_id=session_id,
            armed_run_id=armed_run_id,
            armed_handoff_path=handoff_path,
            analyze_run_dir=analysis["run_dir"],
            analyze_summary_md=analysis["summary_md_path"],
            analyze_causality_json=analysis["causality_json_path"],
            kanban_attempt_id=attempt_id,
        )
    except Exception as exc:  # noqa: BLE001 — marker 失败绝不阻断 advisory 主路径。
        return {
            "post_analyze_quiet_marker_status": "write-failed",
            "post_analyze_quiet_marker_error": f"{type(exc).__name__}: {exc}",
        }
    if marker_path is None:
        return {"post_analyze_quiet_marker_status": "no-key-available"}
    return {
        "post_analyze_quiet_marker_status": "armed",
        "post_analyze_quiet_marker_path": str(marker_path),
    }


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


def current_kanban_project_path(event: dict[str, Any], fallback: str | None) -> str | None:
    value = _event_or_env_text(
        event,
        ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH"),
        ("kanban_project_path", "kanbanProjectPath", "project_path", "projectPath"),
    )
    return value or fallback


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


def _run_kanban_message(
    *,
    ledger: Any,
    analysis: dict[str, str],
    task_id: str,
    project_path: str,
    attempt_id: str | None,
) -> dict[str, Any]:
    client = Path(os.environ.get("CODEX_RVF_CLINE_KANBAN_CLIENT", str(DEFAULT_CLINE_KANBAN_CLIENT))).expanduser()
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    prompt = rvf_analyze_followup_prompt(analysis)
    prompt_path = ledger.artifact("rvf-analyze-followup.prompt.md", prompt)
    if not prompt_path:
        raise RuntimeError("failed to write rvf-analyze follow-up prompt artifact")
    idempotency_key = f"rvf-analyze:{Path(analysis['run_dir']).name}"
    command = [
        sys.executable,
        str(client),
        "message",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
        "--prompt-file",
        prompt_path,
        "--source",
        "rvf-analyze",
        "--idempotency-key",
        idempotency_key,
    ]
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    command_path = ledger.artifact(
        "rvf-analyze-followup-message.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Cline Kanban task message failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Cline Kanban task message JSON: {completed.stdout!r}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"invalid Cline Kanban task message payload: {result!r}")
    message_id = str(result.get("message_id") or result.get("messageId") or "").strip()
    if not message_id:
        raise RuntimeError(f"Cline Kanban task message response did not include message_id: {result!r}")
    result["message_id"] = message_id
    result.setdefault("task_id", task_id)
    if attempt_id:
        result.setdefault("attempt_id", attempt_id)
    result["prompt_path"] = prompt_path
    result["command_artifact_path"] = command_path
    result["project_path"] = project_path
    result["task_cmd"] = task_cmd
    return result


def surface_rvf_analyze_advisory(
    *,
    event: dict[str, Any],
    ledger: Any,
    payload: dict[str, Any],
    finalize_record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """在 Stop hook handoff 完成 payload 上追加 ``$rvf-analyze`` 后续动作。

    Cline Kanban task 内直接注入真实 follow-up 用户消息；非 Kanban native
    session 只在 hook payload 与 summary 里提示用户手动触发。
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
    fallback_project = finalize_record.get("repo") if isinstance(finalize_record, dict) else None
    if not isinstance(fallback_project, str) or not fallback_project:
        fallback_project = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    project_path = current_kanban_project_path(event, fallback_project)

    if task_id and project_path:
        attempt_id = current_kanban_attempt_id(event)
        try:
            message_payload = _run_kanban_message(
                ledger=ledger,
                analysis=analysis,
                task_id=task_id,
                project_path=project_path,
                attempt_id=attempt_id,
            )
        except Exception as exc:  # noqa: BLE001 - advisory must not fail finalize/handoff.
            error = f"{type(exc).__name__}: {exc}"
            marker_info = _arm_post_analyze_quiet_marker_safe(
                event=event,
                ledger=ledger,
                finalize_record=finalize_record,
                analysis=analysis,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            fields = {
                **base_fields,
                "rvf_analyze_status": "kanban-injection-failed",
                "rvf_analyze_error": error,
                "rvf_analyze_kanban_task_id": task_id,
                "rvf_analyze_kanban_attempt_id": attempt_id,
                **marker_info,
            }
            ledger.event(
                phase="analysis",
                event="rvf_analyze_followup_failed",
                status="warning",
                reason_code="rvf_analyze_followup_failed",
                level="warn",
                error=error,
                **fields,
            )
            _merge_summary(ledger, fields)
            _append_system_message(payload, f"rvf_analyze=manual_required; trigger={trigger}")
            return fields

        marker_info = _arm_post_analyze_quiet_marker_safe(
            event=event,
            ledger=ledger,
            finalize_record=finalize_record,
            analysis=analysis,
            task_id=message_payload.get("task_id") or task_id,
            attempt_id=message_payload.get("attempt_id") or attempt_id,
        )
        fields = {
            **base_fields,
            "rvf_analyze_status": "kanban-injected",
            "rvf_analyze_kanban_task_id": message_payload.get("task_id"),
            "rvf_analyze_kanban_attempt_id": message_payload.get("attempt_id"),
            "rvf_analyze_kanban_message_id": message_payload.get("message_id"),
            "rvf_analyze_kanban_turn_id": message_payload.get("turn_id") or message_payload.get("turnId"),
            "rvf_analyze_kanban_checkpoint_id": (
                message_payload.get("checkpoint_id") or message_payload.get("checkpointId")
            ),
            "rvf_analyze_followup_prompt_path": message_payload.get("prompt_path"),
            "rvf_analyze_followup_command_path": message_payload.get("command_artifact_path"),
            **marker_info,
        }
        ledger.event(
            phase="analysis",
            event="rvf_analyze_followup_injected",
            status="completed",
            reason_code="rvf_analyze_followup_injected",
            **fields,
        )
        _merge_summary(ledger, fields)
        _append_system_message(payload, "rvf_analyze=kanban_injected")
        return fields

    marker_info = _arm_post_analyze_quiet_marker_safe(
        event=event,
        ledger=ledger,
        finalize_record=finalize_record,
        analysis=analysis,
        task_id=task_id,
        attempt_id=None,
    )
    fields = {
        **base_fields,
        "rvf_analyze_status": "manual-required",
        **marker_info,
    }
    ledger.event(
        phase="analysis",
        event="rvf_analyze_manual_advisory",
        status="completed",
        reason_code="rvf_analyze_manual_required",
        **fields,
    )
    _merge_summary(ledger, fields)
    _append_system_message(payload, f"rvf_analyze=manual_required; trigger={trigger}")
    return fields
