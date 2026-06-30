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

本 facade 保留 CLI ``main`` + 对既有 import 点的 transcript-distill 符号 re-export
（``distill_*_jsonl`` / ``write_jsonl`` / models / io）。``distill_*_jsonl`` 公共边界
仍返回 ``list[dict]``（内部建模走 ``TranscriptRecord``，边界处 ``.to_dict()``）。

**host 探测组合根（``detect_transcript_format`` 与 ``HOST_CODEX`` / ``HOST_CLAUDE``
常量）已于 S9c 上提到 ``core/host_adapter/host_transcript_format_detection.py``**——
唯一允许同时知晓两个 host record 签名的地方从此住 ``core/host_adapter/``，本 distill
facade 不再持有 host 身份真相源。

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
