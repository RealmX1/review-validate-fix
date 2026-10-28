"""原生错误事实优先；模型正文不是故障分类输入。"""
from __future__ import annotations

import json

from .harness_cli_adapters import looks_like_usage_limit, kimi_retry_event_exhausted_provider_usage, oh_my_pi_record_error_text


def classify_role_failure(stdout: str, stderr: str, returncode: int, output_format: str) -> dict | None:
    native_errors = []
    if output_format != "text":
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload", event)
            if not isinstance(payload, dict):
                continue
            if (payload.get("type") in {"error", "turn.failed", "thread.error"} or payload.get("is_error") is True
                    or payload.get("stopReason") == "error" or kimi_retry_event_exhausted_provider_usage(payload)
                    or oh_my_pi_record_error_text(payload)):
                native_errors.append(payload)
    if not native_errors and returncode == 0:
        return None
    # stderr 只有在进程失败时才可作为兜底，绝不扫描正常评审正文。
    error_text = json.dumps(native_errors, ensure_ascii=False) if native_errors else stderr
    lower = error_text.lower()
    if any(word in lower for word in ("authentication", "failed to authenticate", "oauth session expired", "not logged in", "please run /login", "unauthorized", "invalid api key", "invalid_api_key", '"code": 401', "http 401", "login required")):
        category = "authentication"
    elif looks_like_usage_limit(error_text) or any(str(item.get("error", {}).get("code")) == "429" for item in native_errors if isinstance(item.get("error"), dict)):
        category = "usage_limit"
    elif any(word in lower for word in ("unknown option", "unknown argument", "invalid configuration", "model not found", "unrecognized arguments")):
        category = "configuration"
    elif any(word in lower for word in ("permission denied", "operation not permitted", "sandbox", "loopback", "eacces")):
        category = "deterministic"
    else:
        category = "transient"
    return {"category": category, "source": "native_error_event" if native_errors else "failed_process_stderr",
            "native_errors": native_errors, "retry_not_before_seconds": 300 if category in {"authentication", "usage_limit"} else 0,
            "maximum_same_configuration_retries": 1 if category in {"transient", "authentication", "usage_limit"} else 0}


def native_invocation_facts(stdout: str) -> dict:
    sessions, models, usages = [], [], []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"system", "init", "thread.started", "turn.completed", "result", "session_start", "session", "message_end"}:
            continue
        for key in ("session_id", "sessionId", "thread_id"):
            if isinstance(event.get(key), str):
                sessions.append(event[key])
        if isinstance(event.get("model"), str):
            models.append(event["model"])
        if isinstance(event.get("usage"), dict):
            usages.append(event["usage"])
    return {"native_session_ids": sorted(set(sessions)) or None, "actual_models": sorted(set(models)) or None,
            "native_usage": usages or None}
