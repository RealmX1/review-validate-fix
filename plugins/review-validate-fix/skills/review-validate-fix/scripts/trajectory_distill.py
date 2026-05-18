#!/usr/bin/env python3
"""把原始 Codex rollout JSONL 与 reviewer stream-json 蒸馏成统一的 trajectory schema。

输出 schema (`trajectory.jsonl` 每行):
{
  "schema_version": 1,
  "ts": "ISO8601-UTC",
  "source": "codex" | "reviewer:<id>",
  "kind": "tool_call" | "tool_result" | "message" | "reasoning" | "phase_marker",
  "call_id": "...",
  "summary": "短文本",
  "raw_ref": {"file": "rollout.jsonl", "line": 12, "byte_range": [a, b]},
  "artifact_refs": [{"path": "...", "lines": [42, 58], "op": "edit|create|delete"}]
}

只做确定性蒸馏（无 LLM），为后续 `/rvf-analyze` 复盘 agent 提供基础。

Host 耦合说明:
- ``distill_codex_jsonl`` / ``_distill_codex_record`` / ``_codex_parse_apply_patch_fallback``
  / ``_patch_lines_for_op`` / ``_patch_op_name`` 解析 **Codex** rollout schema
  (``event_msg`` / ``response_item.function_call`` /
  ``response_item.custom_tool_call`` / ``apply_patch`` custom-format patch 等)。
  Codex 把 ``apply_patch`` 调用走 ``custom_tool_call`` (``payload.input``);
  ``exec_command`` 等仍走 ``function_call`` (``payload.arguments``); 两条路径
  都识别。同理 output 既可能是 ``function_call_output`` 也可能是
  ``custom_tool_call_output``。
- ``distill_claude_jsonl`` / ``_distill_claude_record`` /
  ``_claude_tool_call_artifact_refs`` / ``_claude_message_text_blocks`` /
  ``_extract_apply_patch_from_bash`` 解析 **Claude Code** transcript NDJSON
  schema (``user`` / ``assistant`` / ``summary`` / ``system`` 等顶层 record;
  ``message.content`` 列表内含 ``text`` / ``thinking`` / ``tool_use`` /
  ``tool_result`` 等 block)。Claude ``Edit`` / ``Write`` / ``NotebookEdit``
  没有行号信息，``artifact_refs.lines`` 写 None；``Bash`` 工具中检测
  ``apply_patch`` heredoc 后复用 Codex patch 解析。
- ``detect_transcript_format`` 按 transcript 文件首条非空 record 的 ``type``
  字段探测 host；上层 ``trajectory_capture.capture_run`` 据此分派到对应
  distiller。两条解析栈互不交叉——新增 host 时再加新的平行 ``_<host>_*``
  实现。
- ``distill_reviewer_stream`` 解析 Claude Code stream-json (子进程 reviewer
  的 stdout NDJSON)，与主轨迹解析无关。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_manifest import parse_apply_patch  # noqa: E402

SCHEMA_VERSION = 1
SUMMARY_MAX_BYTES = 2048

HOST_CODEX = "codex"
"""Codex rollout JSONL schema (``session_meta`` / ``turn_context`` / ``event_msg``
/ ``response_item`` record types)。由 ``distill_codex_jsonl`` /
``_distill_codex_record`` 等解析。"""

HOST_CLAUDE = "claude_code"
"""Claude Code transcript NDJSON schema (``user`` / ``assistant`` / ``summary``
record types，无 ``payload`` 包裹)。由 ``distill_claude_jsonl`` /
``_distill_claude_record`` 等解析。"""

HOST_KIND = HOST_CODEX
"""向后兼容别名。新代码请直接使用 ``HOST_CODEX`` / ``HOST_CLAUDE`` 二者之一。
本常量保留是为了让早期写死 ``HOST_KIND="codex"`` 字面量的历史 import 点继续工作；
所有新增 manifest / summary 写入路径都应当从 ``detect_transcript_format`` 结果决定。"""

_FORMAT_DETECT_MAX_LINES = 32
_CODEX_RECORD_TYPES = frozenset(
    {"session_meta", "turn_context", "event_msg", "response_item"}
)
_CLAUDE_RECORD_TYPES = frozenset({"user", "assistant", "summary", "system"})


def detect_transcript_format(path: Path) -> str | None:
    """探测 transcript 文件的 schema host：``HOST_CODEX`` / ``HOST_CLAUDE`` / None。

    读首 ``_FORMAT_DETECT_MAX_LINES`` 行非空 JSON-decodable record；命中 Codex
    record types (``session_meta`` / ``turn_context`` / ``event_msg`` /
    ``response_item``) 立即返回 ``HOST_CODEX``，命中 Claude record types
    (``user`` / ``assistant`` / ``summary`` / ``system``) 立即返回 ``HOST_CLAUDE``。
    全部读完仍未命中（空文件 / 异常 schema / 截断）→ 返回 None；调用方应当
    fallback 到 ``HOST_CODEX`` 以保证既有 Codex-only 用例无回归。
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            scanned = 0
            for raw in handle:
                if scanned >= _FORMAT_DETECT_MAX_LINES:
                    break
                scanned += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                if rtype in _CODEX_RECORD_TYPES:
                    return HOST_CODEX
                if rtype in _CLAUDE_RECORD_TYPES:
                    return HOST_CLAUDE
    except OSError:
        return None
    return None


def read_codex_originator(rollout_path: Path) -> str | None:
    """从 rollout JSONL 首个 ``session_meta`` record 读 ``originator`` 字段。

    Codex CLI / Codex Desktop 在 rollout 起始 session_meta 中写入 ``originator``
    字符串（例如 ``"Codex Desktop"`` / ``"codex_cli_rs"`` 等）。我们把这个值
    promote 到 manifest 的 ``host_originator`` 字段，作为 host kind 之外的更
    细粒度信号。读取失败 / 字段缺失返回 None，不抛异常。
    """
    if not rollout_path.exists():
        return None
    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    originator = payload.get("originator")
                    if isinstance(originator, str) and originator:
                        return originator
                return None
    except OSError:
        return None
    return None


def _truncate(text: str, limit: int = SUMMARY_MAX_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    truncated = encoded[: limit - 12].decode("utf-8", "ignore")
    return truncated + "…[truncated]"


def _norm_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _safe_json_loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _byte_range_for_line(byte_offsets: list[int], line_index: int) -> list[int] | None:
    """line_index 是 0-based。byte_offsets[i] 是第 i 行起始字节，长度 N+1。"""
    if not byte_offsets or line_index < 0 or line_index + 1 >= len(byte_offsets):
        return None
    return [byte_offsets[line_index], byte_offsets[line_index + 1]]


def _read_jsonl_with_offsets(path: Path) -> tuple[list[dict[str, Any]], list[int]]:
    records: list[dict[str, Any]] = []
    offsets: list[int] = [0]
    if not path.exists():
        return records, offsets
    data = path.read_bytes()
    pos = 0
    line_no = 0
    for raw in data.splitlines(keepends=True):
        line_no += 1
        offsets.append(pos + len(raw))
        try:
            text = raw.rstrip(b"\n\r").decode("utf-8")
            if text.strip():
                obj = json.loads(text)
                if isinstance(obj, dict):
                    obj["_line_number"] = line_no
                    records.append(obj)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        pos += len(raw)
    return records, offsets


def _distill_codex_record(
    record: dict[str, Any],
    *,
    repo: Path | None,
    rollout_filename: str,
    byte_offsets: list[int],
) -> dict[str, Any] | None:
    line_number = record.get("_line_number") or 0
    raw_ref = {
        "file": rollout_filename,
        "line": line_number,
        "byte_range": _byte_range_for_line(byte_offsets, line_number - 1),
    }
    ts = record.get("timestamp")
    record_type = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else None

    base = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts,
        "source": "codex",
        "raw_ref": raw_ref,
    }

    if record_type == "session_meta" and payload:
        return {
            **base,
            "kind": "phase_marker",
            "call_id": None,
            "marker": "session_meta",
            "summary": _truncate(
                f"session_meta id={payload.get('id')} cwd={payload.get('cwd')}"
            ),
            "artifact_refs": [],
        }

    if record_type == "turn_context" and payload:
        return {
            **base,
            "kind": "phase_marker",
            "call_id": None,
            "marker": "turn_context",
            "summary": _truncate(json.dumps(payload, ensure_ascii=False)),
            "artifact_refs": [],
        }

    if record_type == "event_msg" and payload:
        sub = payload.get("type")
        if sub in {"task_started", "task_complete"}:
            return {
                **base,
                "kind": "phase_marker",
                "call_id": None,
                "marker": sub,
                "summary": _truncate(
                    f"{sub} turn={payload.get('turn_id', '')}".strip()
                ),
                "artifact_refs": [],
            }
        if sub == "agent_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return {
                **base,
                "kind": "message",
                "role": "assistant",
                "call_id": None,
                "summary": _truncate(message),
                "artifact_refs": [],
            }
        if sub == "user_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return {
                **base,
                "kind": "message",
                "role": "user",
                "call_id": None,
                "summary": _truncate(message),
                "artifact_refs": [],
            }
        if sub == "exec_command_end":
            return {
                **base,
                "kind": "tool_result",
                "tool": "exec_command",
                "call_id": payload.get("call_id"),
                "summary": _truncate(
                    f"exec_command_end exit={payload.get('exit_code')} duration_ms={payload.get('duration_ms')}"
                ),
                "artifact_refs": [],
            }
        return None

    if record_type == "response_item" and payload:
        sub = payload.get("type")
        if sub in ("function_call", "custom_tool_call"):
            tool = payload.get("name")
            # function_call carries arguments string; custom_tool_call uses input.
            # apply_patch on current Codex versions arrives as custom_tool_call.
            arguments = (
                payload.get("arguments") if sub == "function_call" else payload.get("input")
            )
            artifact_refs: list[dict[str, Any]] = []
            summary_parts: list[str] = [f"call {tool}"]
            if tool == "apply_patch":
                patch_text = arguments if isinstance(arguments, str) else None
                if patch_text:
                    if repo is not None:
                        ops, _ = parse_apply_patch(repo, patch_text, line_number)
                    else:
                        ops, _ = _codex_parse_apply_patch_fallback(patch_text, line_number)
                    for op in ops:
                        artifact_refs.append(
                            {
                                "path": op.get("path"),
                                "lines": _patch_lines_for_op(patch_text, op.get("path")),
                                "op": _patch_op_name(op.get("operation")),
                            }
                        )
                    summary_parts.append(
                        f"{len(ops)} hunk(s)"
                    )
            elif tool == "exec_command":
                args = _safe_json_loads(arguments) or {}
                cmd = args.get("cmd") if isinstance(args, dict) else None
                if isinstance(cmd, str):
                    summary_parts.append(_truncate(cmd, 256))
            else:
                if isinstance(arguments, str):
                    summary_parts.append(_truncate(arguments, 256))
            return {
                **base,
                "kind": "tool_call",
                "tool": tool,
                "call_id": payload.get("call_id"),
                "summary": _truncate(" ".join(summary_parts)),
                "artifact_refs": artifact_refs,
            }
        if sub in ("function_call_output", "custom_tool_call_output"):
            output_text = payload.get("output")
            if isinstance(output_text, dict):
                output_text = output_text.get("output") or output_text.get("text") or json.dumps(output_text, ensure_ascii=False)
            if not isinstance(output_text, str):
                output_text = ""
            return {
                **base,
                "kind": "tool_result",
                "tool": None,
                "call_id": payload.get("call_id"),
                "summary": _truncate(output_text),
                "artifact_refs": [],
            }
        if sub == "message":
            role = payload.get("role") or "assistant"
            text = _norm_text(payload.get("content")) or ""
            return {
                **base,
                "kind": "message",
                "role": role,
                "call_id": None,
                "summary": _truncate(text),
                "artifact_refs": [],
            }
        if sub == "reasoning":
            text = _norm_text(payload.get("summary"))
            if text is None:
                # keep an indicator without leaking encrypted_content blob
                text = "<encrypted reasoning>"
            return {
                **base,
                "kind": "reasoning",
                "call_id": None,
                "summary": _truncate(text),
                "artifact_refs": [],
            }
        return None

    return None


def _codex_parse_apply_patch_fallback(patch_text: str, line_number: int) -> tuple[list[dict[str, Any]], set[str]]:
    """Repo-less Codex apply_patch parser; mirrors session_manifest.parse_apply_patch
    shape but skips path normalization. Codex-specific patch text format
    (``*** Add File: ...`` / ``*** Update File: ...``).
    """
    operations: list[dict[str, Any]] = []
    paths: set[str] = set()
    for raw_line in patch_text.splitlines():
        for prefix, op in (
            ("*** Add File: ", "add"),
            ("*** Delete File: ", "delete"),
            ("*** Update File: ", "update"),
        ):
            if raw_line.startswith(prefix):
                rel = raw_line.removeprefix(prefix).strip()
                if rel:
                    operations.append({"operation": op, "path": rel, "line_number": line_number})
                    paths.add(rel)
                break
    return operations, paths


def _patch_lines_for_op(patch_text: str, path: str | None) -> list[int]:
    """从 apply_patch 文本中提取该 path 下首段 hunk 的 @@ -X,Y +A,B @@ 中的 A 与 A+B-1。"""
    if not path:
        return []
    in_path = False
    for raw_line in patch_text.splitlines():
        if raw_line.startswith("*** ") and "File:" in raw_line:
            in_path = raw_line.endswith(": " + path) or raw_line.endswith(":" + path) or raw_line.endswith(path)
            continue
        if not in_path:
            continue
        if raw_line.startswith("@@"):
            try:
                # @@ -a,b +c,d @@
                parts = raw_line.split(" ")
                plus = next(p for p in parts if p.startswith("+"))
                plus = plus.lstrip("+")
                if "," in plus:
                    start_str, length_str = plus.split(",", 1)
                    start = int(start_str)
                    length = int(length_str)
                    return [start, start + max(length, 1) - 1]
                return [int(plus), int(plus)]
            except (StopIteration, ValueError):
                continue
    return []


def _patch_op_name(operation: str | None) -> str:
    if operation == "add":
        return "create"
    if operation == "delete":
        return "delete"
    return "edit"


_HEREDOC_RE = re.compile(
    r"<<\s*['\"]?(?P<token>[A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\n(?P<body>.*?)\n(?P=token)\s*$",
    re.DOTALL | re.MULTILINE,
)


def _extract_apply_patch_from_bash(command: str) -> str | None:
    """从 Bash ``apply_patch`` 调用中抽出 patch 文本。

    支持两种形式：
    - heredoc: ``apply_patch <<'EOF'\n*** Begin Patch...\nEOF``
    - 内联 stdin: ``apply_patch '*** Begin Patch...\n*** End Patch'``
    无 ``apply_patch`` 关键字 → 返回 None。
    """
    if "apply_patch" not in command:
        return None
    match = _HEREDOC_RE.search(command)
    if match:
        body = match.group("body")
        if "*** Begin Patch" in body or "*** Add File:" in body or "*** Update File:" in body or "*** Delete File:" in body:
            return body
    if "*** Begin Patch" in command:
        start = command.find("*** Begin Patch")
        end = command.rfind("*** End Patch")
        if end > start:
            return command[start:end + len("*** End Patch")]
    return None


def _claude_tool_call_artifact_refs(
    tool_name: str | None,
    tool_input: Any,
    *,
    repo: Path | None,
    line_number: int,
) -> list[dict[str, Any]]:
    """根据 Claude tool_use block 推断 ``artifact_refs``。

    Claude 的 Edit / Write / NotebookEdit input 不含行号信息（与 Codex
    apply_patch 不同），``lines`` 字段写 None；``Bash`` 检测 apply_patch
    heredoc 后复用 Codex patch 解析。其他 tool 不产 artifact_refs。
    """
    if not isinstance(tool_input, dict):
        return []
    refs: list[dict[str, Any]] = []
    if tool_name in {"Edit", "MultiEdit"}:
        path_value = tool_input.get("file_path")
        if isinstance(path_value, str) and path_value:
            refs.append({"path": path_value, "lines": None, "op": "edit"})
    elif tool_name == "Write":
        path_value = tool_input.get("file_path")
        if isinstance(path_value, str) and path_value:
            refs.append({"path": path_value, "lines": None, "op": "create"})
    elif tool_name == "NotebookEdit":
        path_value = tool_input.get("notebook_path")
        if isinstance(path_value, str) and path_value:
            refs.append({"path": path_value, "lines": None, "op": "edit"})
    elif tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            patch_text = _extract_apply_patch_from_bash(command)
            if patch_text:
                if repo is not None:
                    ops, _ = parse_apply_patch(repo, patch_text, line_number)
                else:
                    ops, _ = _codex_parse_apply_patch_fallback(patch_text, line_number)
                for op in ops:
                    refs.append(
                        {
                            "path": op.get("path"),
                            "lines": _patch_lines_for_op(patch_text, op.get("path")),
                            "op": _patch_op_name(op.get("operation")),
                        }
                    )
    return refs


def _claude_message_text_blocks(content: Any) -> tuple[list[str], list[str]]:
    """把 Claude message ``content`` 拆成 (text_parts, thinking_parts)。

    string content → 全部归 text。list content → 按 ``type`` 字段分流：
    ``text`` → text_parts；``thinking`` → thinking_parts；其他 (``tool_use``
    / ``tool_result`` / ``image``) 由调用方单独成 record。
    """
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    if isinstance(content, str):
        if content:
            text_parts.append(content)
        return text_parts, thinking_parts
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                value = block.get("text")
                if isinstance(value, str) and value:
                    text_parts.append(value)
            elif bt == "thinking":
                value = block.get("thinking")
                if isinstance(value, str) and value:
                    thinking_parts.append(value)
    return text_parts, thinking_parts


def _distill_claude_record(
    record: dict[str, Any],
    *,
    repo: Path | None,
    rollout_filename: str,
    byte_offsets: list[int],
) -> list[dict[str, Any]]:
    """把单条 Claude Code NDJSON record 蒸馏成 0..N 条统一 trajectory record。

    一条 ``assistant`` record 可能同时含 ``text`` / ``thinking`` / 多条
    ``tool_use`` block，每个 block 都展开成独立 trajectory record；同样
    ``user`` record 的 ``tool_result`` block 单独成 record。其余 record type
    （``permission-mode`` / ``file-history-snapshot`` / ``ai-title`` / ``attachment``
    / ``agent-name`` / ``last-prompt`` / ``queue-operation`` / ``stop-hook-feedback``
    等）当前不抽取，返回空列表。
    """
    line_number = record.get("_line_number") or 0
    raw_ref = {
        "file": rollout_filename,
        "line": line_number,
        "byte_range": _byte_range_for_line(byte_offsets, line_number - 1),
    }
    ts = record.get("timestamp")
    record_type = record.get("type")
    base = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts,
        "source": "claude_code",
        "raw_ref": raw_ref,
    }
    out: list[dict[str, Any]] = []

    if record_type == "summary":
        summary_text = record.get("summary")
        if not isinstance(summary_text, str):
            summary_text = ""
        out.append(
            {
                **base,
                "kind": "phase_marker",
                "call_id": None,
                "marker": "summary",
                "summary": _truncate(summary_text or "<empty summary>"),
                "artifact_refs": [],
            }
        )
        return out

    if record_type == "system":
        subtype = record.get("subtype") or "system"
        out.append(
            {
                **base,
                "kind": "phase_marker",
                "call_id": None,
                "marker": f"system:{subtype}",
                "summary": _truncate(json.dumps(record, ensure_ascii=False)),
                "artifact_refs": [],
            }
        )
        return out

    if record_type not in ("user", "assistant"):
        return out

    message = record.get("message")
    if not isinstance(message, dict):
        return out
    role = message.get("role") or record_type
    content = message.get("content")

    text_parts, thinking_parts = _claude_message_text_blocks(content)

    if thinking_parts and record_type == "assistant":
        out.append(
            {
                **base,
                "kind": "reasoning",
                "call_id": None,
                "summary": _truncate("\n\n".join(thinking_parts)),
                "artifact_refs": [],
            }
        )

    if text_parts:
        out.append(
            {
                **base,
                "kind": "message",
                "role": role,
                "call_id": None,
                "summary": _truncate("\n\n".join(text_parts)),
                "artifact_refs": [],
            }
        )

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                tool_name = block.get("name")
                tool_input = block.get("input")
                summary_parts = [f"call {tool_name or '<unknown>'}"]
                if isinstance(tool_input, dict):
                    primitive = json.dumps(tool_input, ensure_ascii=False)
                    summary_parts.append(_truncate(primitive, 256))
                out.append(
                    {
                        **base,
                        "kind": "tool_call",
                        "tool": tool_name,
                        "call_id": block.get("id"),
                        "summary": _truncate(" ".join(summary_parts)),
                        "artifact_refs": _claude_tool_call_artifact_refs(
                            tool_name,
                            tool_input,
                            repo=repo,
                            line_number=line_number,
                        ),
                    }
                )
            elif bt == "tool_result":
                result_content = block.get("content")
                if isinstance(result_content, list):
                    parts: list[str] = []
                    for sub in result_content:
                        if isinstance(sub, dict):
                            text = sub.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                    result_text = "\n".join(parts)
                elif isinstance(result_content, str):
                    result_text = result_content
                else:
                    result_text = ""
                out.append(
                    {
                        **base,
                        "kind": "tool_result",
                        "tool": None,
                        "call_id": block.get("tool_use_id"),
                        "summary": _truncate(result_text),
                        "artifact_refs": [],
                    }
                )
    return out


def distill_claude_jsonl(
    *,
    rollout_path: Path,
    rollout_filename: str,
    repo: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Claude Code transcript NDJSON → 统一 trajectory schema。

    与 ``distill_codex_jsonl`` 同签名同返回类型。一条 Claude record 可能
    展开成多条 trajectory record（assistant 内含多个 tool_use 等），故内部
    用 ``_distill_claude_record`` 返回 list 而非单条。
    """
    records, offsets = _read_jsonl_with_offsets(rollout_path)
    distilled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for record in records:
        outs = _distill_claude_record(
            record,
            repo=repo,
            rollout_filename=rollout_filename,
            byte_offsets=offsets,
        )
        for out in outs:
            kind = out.get("kind") or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
            distilled.append(out)
    index = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": rollout_filename,
        "record_count": len(distilled),
        "kind_counts": counts,
    }
    return distilled, index


def distill_codex_jsonl(
    *,
    rollout_path: Path,
    rollout_filename: str,
    repo: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records, offsets = _read_jsonl_with_offsets(rollout_path)
    distilled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for record in records:
        out = _distill_codex_record(
            record,
            repo=repo,
            rollout_filename=rollout_filename,
            byte_offsets=offsets,
        )
        if out is None:
            continue
        kind = out.get("kind") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
        distilled.append(out)
    index = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": rollout_filename,
        "record_count": len(distilled),
        "kind_counts": counts,
    }
    return distilled, index


def distill_reviewer_stream(
    *,
    stdout_path: Path,
    reviewer_id: str,
) -> list[dict[str, Any]]:
    """解析 Claude Code stream-json (or 类似 NDJSON) reviewer stdout。"""
    if not stdout_path.exists():
        return []
    distilled: list[dict[str, Any]] = []
    raw_filename = stdout_path.name
    line_no = 0
    for raw in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line_no += 1
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        kind = "message"
        summary = json.dumps(payload, ensure_ascii=False)
        ptype = payload.get("type")
        ts = payload.get("timestamp")
        artifact_refs: list[dict[str, Any]] = []
        if ptype == "system":
            kind = "phase_marker"
            summary = f"reviewer system: {payload.get('subtype') or payload.get('event') or ''}"
        elif ptype == "assistant":
            content = payload.get("message", {}).get("content") if isinstance(payload.get("message"), dict) else None
            text = _norm_text(content) or json.dumps(payload, ensure_ascii=False)
            summary = text
        elif ptype == "user":
            content = payload.get("message", {}).get("content") if isinstance(payload.get("message"), dict) else None
            text = _norm_text(content) or json.dumps(payload, ensure_ascii=False)
            summary = text
            kind = "message"
        elif ptype == "result":
            kind = "phase_marker"
            text = payload.get("result")
            summary = f"reviewer result: {text if isinstance(text, str) else ''}"
        elif ptype == "tool_use":
            kind = "tool_call"
        elif ptype == "tool_result":
            kind = "tool_result"
        distilled.append(
            {
                "schema_version": SCHEMA_VERSION,
                "ts": ts,
                "source": f"reviewer:{reviewer_id}",
                "kind": kind,
                "call_id": payload.get("id") or payload.get("tool_use_id"),
                "summary": _truncate(summary),
                "raw_ref": {"file": raw_filename, "line": line_no, "byte_range": None},
                "artifact_refs": artifact_refs,
            }
        )
    return distilled


def write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Distill a Codex rollout JSONL into RVF trajectory.jsonl.")
    parser.add_argument("--rollout", required=True, help="Path to a Codex rollout JSONL file.")
    parser.add_argument("--output", required=True, help="Path for distilled trajectory.jsonl.")
    parser.add_argument("--repo", help="Optional repo root for path normalization in apply_patch refs.")
    parser.add_argument("--filename", help="Logical rollout filename to embed in raw_ref.file.")
    args = parser.parse_args()
    rollout = Path(args.rollout).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    rollout_filename = args.filename or rollout.name
    distilled, index = distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename=rollout_filename,
        repo=repo,
    )
    output_path = Path(args.output).expanduser().resolve()
    write_jsonl(distilled, output_path)
    index_path = output_path.with_name(output_path.stem + ".index.json")
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
