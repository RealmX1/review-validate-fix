"""统一 trajectory record 模型（host 无关）。

``TranscriptRecord`` 是 adapters 蒸馏主轨迹（Codex rollout / Claude transcript）后的
内部建模单元；``to_dict()`` 把它序列化回既有 ``trajectory.jsonl`` schema，且
**逐 kind 复刻历史键序**，保证 ``write_jsonl`` 写出的字节与重构前完全一致
（byte-equal——S1 纯重构的安全网）。

历史键序（base = ``schema_version, ts, source, raw_ref``，随后按 kind）：

- ``phase_marker``: kind, call_id, marker, summary, artifact_refs
- ``message``    : kind, role, call_id, summary, artifact_refs
- ``tool_call``  : kind, tool, call_id, summary, artifact_refs
- ``tool_result``: kind, tool, call_id, summary, artifact_refs
- ``reasoning``  : kind, call_id, summary, artifact_refs

注意：``distill_reviewer_stream`` 的 reviewer-stdout record 采用另一套键序
（raw_ref 在 summary 之后、且不含 tool/role/marker），不经本模型，仍在
``core.transcript.io`` 内直接构造 dict。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_TOOL_KINDS = ("tool_call", "tool_result")


@dataclass
class TranscriptRecord:
    """单条归一 trajectory record。

    ``tool`` / ``role`` / ``marker`` 是按 kind 选用的可选字段；``to_dict()`` 只在
    对应 kind 下 emit 它们（即便值为 ``None`` 也照旧 emit，复刻历史 ``"tool": null``
    等输出）。
    """

    schema_version: int
    ts: Any
    source: str
    raw_ref: Any
    kind: str
    summary: Any = None
    artifact_refs: list[Any] = field(default_factory=list)
    call_id: Any = None
    tool: Any = None
    role: Any = None
    marker: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "ts": self.ts,
            "source": self.source,
            "raw_ref": self.raw_ref,
            "kind": self.kind,
        }
        if self.kind in _TOOL_KINDS:
            d["tool"] = self.tool
            d["call_id"] = self.call_id
        elif self.kind == "message":
            d["role"] = self.role
            d["call_id"] = self.call_id
        elif self.kind == "phase_marker":
            d["call_id"] = self.call_id
            d["marker"] = self.marker
        else:
            d["call_id"] = self.call_id
        d["summary"] = self.summary
        d["artifact_refs"] = self.artifact_refs
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptRecord":
        return cls(
            schema_version=data["schema_version"],
            ts=data.get("ts"),
            source=data.get("source"),
            raw_ref=data.get("raw_ref"),
            kind=data.get("kind"),
            summary=data.get("summary"),
            artifact_refs=data.get("artifact_refs", []),
            call_id=data.get("call_id"),
            tool=data.get("tool"),
            role=data.get("role"),
            marker=data.get("marker"),
        )


@dataclass
class NormalizedTranscript:
    """一组归一 record 的容器，提供与既有 ``list[dict]`` 边界互转。"""

    records: list[TranscriptRecord] = field(default_factory=list)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.records]

    @classmethod
    def from_dicts(cls, dicts: list[dict[str, Any]]) -> "NormalizedTranscript":
        return cls(records=[TranscriptRecord.from_dict(d) for d in dicts])

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)
