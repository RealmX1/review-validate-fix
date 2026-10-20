"""仅 UserMessage 的独立 user turn 可作原生回退分界；hook_prompt 的 role=user 不算。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

CODEX_CLI_VERSIONS_WITH_VERIFIED_NATIVE_REWIND_BOUNDARY = frozenset({"0.163.0"})


def read_independent_user_turn_boundary_evidence(*, receipt: Mapping[str, Any]) -> dict[str, Any]:
    failure = {"independent_user_turn_verified": False, "reason": "缺少原生独立用户 turn 的正向证据"}
    try:
        records = [json.loads(line) for line in Path(receipt["transcript_path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, KeyError):
        return failure
    session = None
    native_cli_version = None
    native_originator = None
    native_session_source = None
    current_turn = None
    current_turn_is_user_initiated = False
    found = None
    later_turn = False
    boundary_turn_user_message_count = 0
    for record in records:
        if not isinstance(record, dict):
            return failure
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return failure
        if record.get("type") == "session_meta":
            session = payload.get("id")
            native_cli_version = payload.get("cli_version")
            native_originator = payload.get("originator")
            native_session_source = payload.get("source")
        if record.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "task_started":
            current_turn = payload.get("turn_id")
            attribution = payload.get("turn_attribution") or {}
            if not isinstance(attribution, dict):
                return failure
            current_turn_is_user_initiated = attribution.get("turn_trigger") == "user"
            # EXPERIMENT_NODE: 补齐冻结输入证明后，评估同一已执行 run 跨新用户 turn 的重试。
            if found is not None and current_turn_is_user_initiated and current_turn != receipt.get("native_turn_id"):
                later_turn = True
        if kind in ("thread_rolled_back", "thread_reverted", "conversation_reverted") and found is not None:
            return {**failure, "reason": "分界之后发生原生会话回退"}
        item = payload.get("item") or {}
        if not isinstance(item, dict):
            return failure
        if kind == "item_completed" and item.get("type") == "UserMessage":
            if payload.get("turn_id") == receipt.get("native_turn_id"):
                boundary_turn_user_message_count += 1
            if item.get("id") != receipt.get("native_user_message_id"):
                continue
            if found is not None:
                return {**failure, "reason": "原生消息 ID 不是唯一分界"}
            if not current_turn_is_user_initiated or current_turn != receipt.get("native_turn_id") or payload.get("turn_id") != current_turn:
                return failure
            content = item.get("content", [])
            if not isinstance(content, list) or not content or any(not isinstance(part, dict) or part.get("type") not in ("text", "input_text") or not isinstance(part.get("text"), str) for part in content):
                return failure
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if text.lstrip().startswith(("<hook_prompt", "<task-notification", "<environment_context>")):
                return failure
            found = {"independent_user_turn_verified": True, "message_text": text, "native_message_received_at": record.get("timestamp")}
    if session != receipt.get("session_id") or later_turn:
        return {**failure, "reason": "原生会话身份已改变或分界之后已进入另一工作 turn"}
    if found is not None and boundary_turn_user_message_count != 1:
        return {**failure, "reason": "决策点被并入已有 user turn，没有独立分界"}
    # 本机 TUI 的隔离 app-server 路径记录 source=vscode；它不等同于 VS Code 扩展宿主。
    if native_cli_version not in CODEX_CLI_VERSIONS_WITH_VERIFIED_NATIVE_REWIND_BOUNDARY or native_originator != "codex-tui" or native_session_source not in {"cli", "vscode"}:
        return {**failure, "capability_unverified": True, "reason": "此 Codex 版本或宿主形态的独立消息与原生回退能力尚未取证"}
    return found or failure
