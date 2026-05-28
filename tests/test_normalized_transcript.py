#!/usr/bin/env python3
"""``core.transcript.models`` 的 round-trip 与 **byte-equal 键序** 回归。

S1 把 distill 主轨迹改为内部建模走 ``TranscriptRecord``、边界 ``.to_dict()``。
本测试钉死 ``to_dict()`` 逐 kind 的键序与历史 ``trajectory.jsonl`` 完全一致——
这是 S1「纯重构 / 零行为变更」的字节级安全网（Python dict ``==`` 与下标访问对
键序不敏感，唯有 JSON 序列化对键序敏感，故必须显式比对序列化字节）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.transcript.models import NormalizedTranscript, TranscriptRecord


# 每个 kind × 变体一条，键序与历史输出逐字段对齐（见 trajectory.jsonl schema）。
GOLDEN = [
    {"schema_version": 1, "ts": "t", "source": "codex", "raw_ref": {"file": "r", "line": 1, "byte_range": [0, 5]}, "kind": "phase_marker", "call_id": None, "marker": "session_meta", "summary": "x", "artifact_refs": []},
    {"schema_version": 1, "ts": None, "source": "claude_code", "raw_ref": {"file": "r", "line": 2, "byte_range": None}, "kind": "message", "role": "assistant", "call_id": None, "summary": "hi", "artifact_refs": []},
    {"schema_version": 1, "ts": "t", "source": "codex", "raw_ref": {"file": "r", "line": 3, "byte_range": [5, 9]}, "kind": "tool_call", "tool": "apply_patch", "call_id": "call_a", "summary": "call apply_patch", "artifact_refs": [{"path": "a.py", "lines": [1, 2], "op": "edit"}]},
    {"schema_version": 1, "ts": "t", "source": "codex", "raw_ref": {"file": "r", "line": 4, "byte_range": None}, "kind": "tool_result", "tool": None, "call_id": "call_a", "summary": "done", "artifact_refs": []},
    {"schema_version": 1, "ts": "t", "source": "codex", "raw_ref": {"file": "r", "line": 5, "byte_range": None}, "kind": "reasoning", "call_id": None, "summary": "<encrypted reasoning>", "artifact_refs": []},
]


def _dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def test_from_dict_to_dict_is_value_and_byte_equal_per_kind() -> None:
    for original in GOLDEN:
        record = TranscriptRecord.from_dict(original)
        out = record.to_dict()
        assert out == original
        assert _dumps(out) == _dumps(original)  # 键序敏感的字节级一致


def test_normalized_transcript_roundtrips_whole_list() -> None:
    bundle = NormalizedTranscript.from_dicts(GOLDEN)
    assert len(bundle) == len(GOLDEN)
    assert bundle.to_dicts() == GOLDEN
    expected = "\n".join(_dumps(r) for r in GOLDEN)
    actual = "\n".join(_dumps(r) for r in bundle.to_dicts())
    assert actual == expected


def test_to_dict_emits_only_kind_specific_optional_keys() -> None:
    phase_marker = TranscriptRecord(1, "t", "codex", None, "phase_marker", summary="x", marker="m").to_dict()
    assert list(phase_marker.keys()) == [
        "schema_version", "ts", "source", "raw_ref", "kind", "call_id", "marker", "summary", "artifact_refs",
    ]
    message = TranscriptRecord(1, "t", "codex", None, "message", summary="x", role="user").to_dict()
    assert list(message.keys()) == [
        "schema_version", "ts", "source", "raw_ref", "kind", "role", "call_id", "summary", "artifact_refs",
    ]
    tool_call = TranscriptRecord(1, "t", "codex", None, "tool_call", summary="x", tool="T", call_id="c").to_dict()
    assert list(tool_call.keys()) == [
        "schema_version", "ts", "source", "raw_ref", "kind", "tool", "call_id", "summary", "artifact_refs",
    ]
    reasoning = TranscriptRecord(1, "t", "codex", None, "reasoning", summary="x").to_dict()
    assert list(reasoning.keys()) == [
        "schema_version", "ts", "source", "raw_ref", "kind", "call_id", "summary", "artifact_refs",
    ]


def test_iter_yields_records() -> None:
    bundle = NormalizedTranscript.from_dicts(GOLDEN)
    kinds = [record.kind for record in bundle]
    assert kinds == [d["kind"] for d in GOLDEN]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
