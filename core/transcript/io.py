"""Host 无关的 trajectory IO / 文本原语。

这些 helper 与具体 host schema 无关（纯 JSON / 字节 / 文本处理），由 Codex 与
Claude 两个 adapter 共享。``distill_reviewer_stream`` 解析 Claude Code stream-json
(子进程 reviewer 的 stdout NDJSON)，与主轨迹解析无关，沿用其独立键序、直接构造
dict（不经 ``TranscriptRecord``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
SUMMARY_MAX_BYTES = 2048


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
