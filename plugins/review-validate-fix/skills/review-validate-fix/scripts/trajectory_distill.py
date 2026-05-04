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
  "raw_ref": {"file": "rollout.codex.jsonl", "line": 12, "byte_range": [a, b]},
  "artifact_refs": [{"path": "...", "lines": [42, 58], "op": "edit|create|delete"}]
}

只做确定性蒸馏（无 LLM），为后续 `/rvf-analyze` 复盘 agent 提供基础。

Host 耦合说明:
- ``distill_codex_jsonl`` / ``_distill_codex_record`` / ``_codex_parse_apply_patch_fallback``
  / ``_patch_lines_for_op`` / ``_patch_op_name`` 全部专门解析 **Codex** rollout
  schema (``event_msg`` / ``response_item.function_call`` /
  ``response_item.custom_tool_call`` / ``apply_patch`` custom-format patch 等)。
  当前 Codex 把 ``apply_patch`` 调用走 ``custom_tool_call`` (``payload.input``);
  ``exec_command`` 等仍走 ``function_call`` (``payload.arguments``); 两条路径
  都要识别。同理 output 既可能是 ``function_call_output`` 也可能是
  ``custom_tool_call_output``。
- ``distill_reviewer_stream`` 解析 Claude Code stream-json (Claude Code 子进程
  reviewer 的 stdout NDJSON)，与上面的 Codex 主轨迹解析无关。
- 未来若要让 RVF 主流程跑在 Claude Code host 上，应平行实现
  ``distill_claude_jsonl`` / ``_distill_claude_record`` 等而不是扩展 Codex 函数。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_manifest import parse_apply_patch  # noqa: E402

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
