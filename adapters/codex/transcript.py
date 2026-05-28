"""Codex host transcript adapter。

解析 **Codex** rollout JSONL schema (``session_meta`` / ``turn_context`` /
``event_msg`` / ``response_item.function_call`` / ``response_item.custom_tool_call``
/ ``apply_patch`` custom-format patch 等)，归一成 ``core.transcript.models``。
Codex 把 ``apply_patch`` 调用走 ``custom_tool_call`` (``payload.input``)；
``exec_command`` 等仍走 ``function_call`` (``payload.arguments``)；两条路径都识别。
output 既可能是 ``function_call_output`` 也可能是 ``custom_tool_call_output``。

``apply_patch`` patch-text 解析 helper（``_codex_parse_apply_patch_fallback`` /
``_patch_lines_for_op`` / ``_patch_op_name`` / ``_extract_apply_patch_from_bash``）
是 Codex 原生格式，Claude adapter 的 Bash ``apply_patch`` 检测复用它们。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import（由 facade 预置 scripts_dir）

from core.transcript.io import (  # noqa: E402
    SCHEMA_VERSION,
    _byte_range_for_line,
    _norm_text,
    _read_jsonl_with_offsets,
    _safe_json_loads,
    _truncate,
)
from core.transcript.models import TranscriptRecord  # noqa: E402
from session_manifest import parse_apply_patch  # noqa: E402


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


def _distill_codex_record(
    record: dict[str, Any],
    *,
    repo: Path | None,
    rollout_filename: str,
    byte_offsets: list[int],
) -> TranscriptRecord | None:
    line_number = record.get("_line_number") or 0
    raw_ref = {
        "file": rollout_filename,
        "line": line_number,
        "byte_range": _byte_range_for_line(byte_offsets, line_number - 1),
    }
    ts = record.get("timestamp")
    record_type = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else None

    def _rec(**kwargs: Any) -> TranscriptRecord:
        return TranscriptRecord(
            schema_version=SCHEMA_VERSION,
            ts=ts,
            source="codex",
            raw_ref=raw_ref,
            **kwargs,
        )

    if record_type == "session_meta" and payload:
        return _rec(
            kind="phase_marker",
            call_id=None,
            marker="session_meta",
            summary=_truncate(
                f"session_meta id={payload.get('id')} cwd={payload.get('cwd')}"
            ),
            artifact_refs=[],
        )

    if record_type == "turn_context" and payload:
        return _rec(
            kind="phase_marker",
            call_id=None,
            marker="turn_context",
            summary=_truncate(json.dumps(payload, ensure_ascii=False)),
            artifact_refs=[],
        )

    if record_type == "event_msg" and payload:
        sub = payload.get("type")
        if sub in {"task_started", "task_complete"}:
            return _rec(
                kind="phase_marker",
                call_id=None,
                marker=sub,
                summary=_truncate(
                    f"{sub} turn={payload.get('turn_id', '')}".strip()
                ),
                artifact_refs=[],
            )
        if sub == "agent_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return _rec(
                kind="message",
                role="assistant",
                call_id=None,
                summary=_truncate(message),
                artifact_refs=[],
            )
        if sub == "user_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return _rec(
                kind="message",
                role="user",
                call_id=None,
                summary=_truncate(message),
                artifact_refs=[],
            )
        if sub == "exec_command_end":
            return _rec(
                kind="tool_result",
                tool="exec_command",
                call_id=payload.get("call_id"),
                summary=_truncate(
                    f"exec_command_end exit={payload.get('exit_code')} duration_ms={payload.get('duration_ms')}"
                ),
                artifact_refs=[],
            )
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
            return _rec(
                kind="tool_call",
                tool=tool,
                call_id=payload.get("call_id"),
                summary=_truncate(" ".join(summary_parts)),
                artifact_refs=artifact_refs,
            )
        if sub in ("function_call_output", "custom_tool_call_output"):
            output_text = payload.get("output")
            if isinstance(output_text, dict):
                output_text = output_text.get("output") or output_text.get("text") or json.dumps(output_text, ensure_ascii=False)
            if not isinstance(output_text, str):
                output_text = ""
            return _rec(
                kind="tool_result",
                tool=None,
                call_id=payload.get("call_id"),
                summary=_truncate(output_text),
                artifact_refs=[],
            )
        if sub == "message":
            role = payload.get("role") or "assistant"
            text = _norm_text(payload.get("content")) or ""
            return _rec(
                kind="message",
                role=role,
                call_id=None,
                summary=_truncate(text),
                artifact_refs=[],
            )
        if sub == "reasoning":
            text = _norm_text(payload.get("summary"))
            if text is None:
                # keep an indicator without leaking encrypted_content blob
                text = "<encrypted reasoning>"
            return _rec(
                kind="reasoning",
                call_id=None,
                summary=_truncate(text),
                artifact_refs=[],
            )
        return None

    return None


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
