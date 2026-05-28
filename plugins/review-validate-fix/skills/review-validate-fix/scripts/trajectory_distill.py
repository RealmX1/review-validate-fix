#!/usr/bin/env python3
"""统一 trajectory 蒸馏的 **facade**（S1 起：core/adapters 拆分后的兼容层）。

历史上本文件是 Codex/Claude 两栈解析 + IO 原语 + host 探测的单体（872 行）。S1
把它按指南 6 维契约拆分：

- ``core/transcript/models.py``：``NormalizedTranscript`` / ``TranscriptRecord``
  统一 record 模型（host 无关，``to_dict()`` 逐 kind 复刻历史键序，保 byte-equal）。
- ``core/transcript/io.py``：host 无关 IO/文本原语（``_truncate`` / ``write_jsonl``
  / ``distill_reviewer_stream`` 等）。
- ``adapters/codex/transcript.py``：Codex rollout 解析栈 + apply_patch patch-text
  helper + ``read_codex_originator``。
- ``adapters/claude_code/transcript.py``：Claude transcript 解析栈。

本 facade 仅保留 **host 探测**（``detect_transcript_format`` 与 host 常量——作为
组合根，是唯一允许同时知晓两个 host record 签名的地方）+ CLI ``main`` + 对既有
import 点的符号 re-export。``distill_*_jsonl`` 公共边界仍返回 ``list[dict]``
（内部建模走 ``TranscriptRecord``，边界处 ``.to_dict()``），所以既有 import 点与
4 个 transcript 测试**无需改动**。

输出 schema (``trajectory.jsonl`` 每行) 不变：
{
  "schema_version": 1,
  "ts": "ISO8601-UTC",
  "source": "codex" | "claude_code" | "reviewer:<id>",
  "kind": "tool_call" | "tool_result" | "message" | "reasoning" | "phase_marker",
  "call_id": "...",
  "summary": "短文本",
  "raw_ref": {"file": "rollout.jsonl", "line": 12, "byte_range": [a, b]},
  "artifact_refs": [{"path": "...", "lines": [42, 58], "op": "edit|create|delete"}]
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* / adapters.* import

from core.transcript.io import (  # noqa: E402,F401  — re-export
    SCHEMA_VERSION,
    SUMMARY_MAX_BYTES,
    distill_reviewer_stream,
    write_jsonl,
)
from core.transcript.models import (  # noqa: E402,F401  — re-export
    NormalizedTranscript,
    TranscriptRecord,
)
from adapters.codex.transcript import (  # noqa: E402,F401  — re-export
    distill_codex_jsonl,
    read_codex_originator,
)
from adapters.claude_code.transcript import (  # noqa: E402,F401  — re-export
    distill_claude_jsonl,
)

HOST_CODEX = "codex"
"""Codex rollout JSONL schema (``session_meta`` / ``turn_context`` / ``event_msg``
/ ``response_item`` record types)。由 ``adapters.codex.transcript`` 解析。"""

HOST_CLAUDE = "claude_code"
"""Claude Code transcript NDJSON schema (``user`` / ``assistant`` / ``summary``
record types，无 ``payload`` 包裹)。由 ``adapters.claude_code.transcript`` 解析。"""

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
