"""Claude Code host transcript adapter。

解析 **Claude Code** transcript NDJSON schema (``user`` / ``assistant`` /
``summary`` / ``system`` 等顶层 record；``message.content`` 列表内含 ``text`` /
``thinking`` / ``tool_use`` / ``tool_result`` block)，归一成
``core.transcript.models``。Claude ``Edit`` / ``Write`` / ``NotebookEdit`` 没有
行号信息，``artifact_refs.lines`` 写 None；``Bash`` 工具中检测 ``apply_patch``
heredoc 后复用 ``core.transcript.patch_parsing`` 的 host-中性解析 helper。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import（由 facade 预置 scripts_dir）

from core.transcript.io import (  # noqa: E402
    SCHEMA_VERSION,
    _byte_range_for_line,
    _read_jsonl_with_offsets,
    _truncate,
)
from core.transcript.models import TranscriptRecord  # noqa: E402
from core.transcript.patch_parsing import (  # noqa: E402
    apply_patch_hunk_line_range_for_path,
    apply_patch_operation_to_artifact_verb,
    extract_apply_patch_text_from_bash_command,
    parse_apply_patch_operations_without_repo,
)
from core.session_scope_allocation.session_change_manifest import parse_apply_patch  # noqa: E402


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
            patch_text = extract_apply_patch_text_from_bash_command(command)
            if patch_text:
                if repo is not None:
                    ops, _ = parse_apply_patch(repo, patch_text, line_number)
                else:
                    ops, _ = parse_apply_patch_operations_without_repo(patch_text, line_number)
                for op in ops:
                    refs.append(
                        {
                            "path": op.get("path"),
                            "lines": apply_patch_hunk_line_range_for_path(patch_text, op.get("path")),
                            "op": apply_patch_operation_to_artifact_verb(op.get("operation")),
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
) -> list[TranscriptRecord]:
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

    def _rec(**kwargs: Any) -> TranscriptRecord:
        return TranscriptRecord(
            schema_version=SCHEMA_VERSION,
            ts=ts,
            source="claude_code",
            raw_ref=raw_ref,
            **kwargs,
        )

    out: list[TranscriptRecord] = []

    if record_type == "summary":
        summary_text = record.get("summary")
        if not isinstance(summary_text, str):
            summary_text = ""
        out.append(
            _rec(
                kind="phase_marker",
                call_id=None,
                marker="summary",
                summary=_truncate(summary_text or "<empty summary>"),
                artifact_refs=[],
            )
        )
        return out

    if record_type == "system":
        subtype = record.get("subtype") or "system"
        out.append(
            _rec(
                kind="phase_marker",
                call_id=None,
                marker=f"system:{subtype}",
                summary=_truncate(json.dumps(record, ensure_ascii=False)),
                artifact_refs=[],
            )
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
            _rec(
                kind="reasoning",
                call_id=None,
                summary=_truncate("\n\n".join(thinking_parts)),
                artifact_refs=[],
            )
        )

    if text_parts:
        out.append(
            _rec(
                kind="message",
                role=role,
                call_id=None,
                summary=_truncate("\n\n".join(text_parts)),
                artifact_refs=[],
            )
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
                    _rec(
                        kind="tool_call",
                        tool=tool_name,
                        call_id=block.get("id"),
                        summary=_truncate(" ".join(summary_parts)),
                        artifact_refs=_claude_tool_call_artifact_refs(
                            tool_name,
                            tool_input,
                            repo=repo,
                            line_number=line_number,
                        ),
                    )
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
                    _rec(
                        kind="tool_result",
                        tool=None,
                        call_id=block.get("tool_use_id"),
                        summary=_truncate(result_text),
                        artifact_refs=[],
                    )
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
            kind = out.kind or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
            distilled.append(out.to_dict())
    index = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": rollout_filename,
        "record_count": len(distilled),
        "kind_counts": counts,
    }
    return distilled, index
