#!/usr/bin/env python3
"""A2：Claude `Task` 子代理捕获（host 归一）。

证明 ``subagent_capture`` 在 ``host_kind="claude_code"`` 下，由原始父 transcript
路径定位 ``<uuid>/subagents/agent-*.jsonl``、用 Claude distiller 蒸馏出带
``artifact_refs`` + ``call_id`` 的 write-op tool_call，**完全不依赖
~/.codex/sessions**。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _rvf_test_support.loader import load_script_module as _load


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _build_claude_session(project_dir: Path, session_uuid: str, agent_id: str) -> Path:
    """造一个父 transcript ``<uuid>.jsonl`` + 子代理 ``<uuid>/subagents/agent-<id>.jsonl``。

    返回父 transcript 路径（= ``original_transcript``）。
    """
    parent = project_dir / f"{session_uuid}.jsonl"
    _write_jsonl(
        parent,
        [
            {
                "type": "user",
                "uuid": "p1",
                "timestamp": "2026-05-20T13:39:30.000Z",
                "sessionId": session_uuid,
                "message": {"role": "user", "content": "do the work"},
            },
        ],
    )
    sub_path = project_dir / session_uuid / "subagents" / f"agent-{agent_id}.jsonl"
    _write_jsonl(
        sub_path,
        [
            # 首条 = 子代理初始 prompt，携带 agentId / slug（spawn 元数据来源）。
            {
                "type": "user",
                "uuid": "s1",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": agent_id,
                "slug": "transient-tinkering-iverson",
                "sessionId": session_uuid,
                "timestamp": "2026-05-20T13:39:33.779Z",
                "message": {"role": "user", "content": "you are the explorer subagent"},
            },
            {
                "type": "assistant",
                "uuid": "s2",
                "agentId": agent_id,
                "isSidechain": True,
                "sessionId": session_uuid,
                "timestamp": "2026-05-20T13:39:34.000Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "id": "msg_sub",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_sub_edit_1",
                            "name": "Edit",
                            "input": {
                                "file_path": "/tmp/sub.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "s3",
                "agentId": agent_id,
                "isSidechain": True,
                "sessionId": session_uuid,
                "timestamp": "2026-05-20T13:39:35.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "toolu_sub_edit_1",
                            "type": "tool_result",
                            "content": "edit applied",
                        }
                    ],
                },
            },
        ],
    )
    return parent


def test_capture_all_subagents_claude_discovers_and_distills(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    project_dir = tmp_path / "projects" / "-Users-x-repo"
    parent = _build_claude_session(project_dir, "sess-uuid-1", "abc123def456")

    out_root = tmp_path / "out_subagents"
    # 关键：不传 sessions_root（Codex 布局）；只给 original_transcript。
    manifests = mod.capture_all_subagents(
        main_rollout_path=parent,  # Claude 路径忽略此参数
        dst_root=out_root,
        host_kind=mod.HOST_CLAUDE,
        original_transcript=parent,
    )

    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest["host"] == "claude_code"
    assert manifest["host_originator"] is None
    assert manifest["status"] == "ok"
    assert manifest["spawn"]["agent_id"] == "abc123def456"
    assert manifest["spawn"]["nickname"] == "transient-tinkering-iverson"
    assert manifest["spawn"]["role"] is None
    assert manifest["distill_status"] == "ok"

    sub_dir = out_root / "abc123def456"
    for name in ("rollout.jsonl", "trajectory.jsonl", "trajectory.index.json", "manifest.json"):
        assert (sub_dir / name).is_file(), f"missing {name}"

    distilled = [
        json.loads(line)
        for line in (sub_dir / "trajectory.jsonl").read_text().splitlines()
        if line.strip()
    ]
    edits = [
        rec
        for rec in distilled
        if rec.get("kind") == "tool_call" and rec.get("tool") == "Edit"
    ]
    assert len(edits) == 1
    assert edits[0]["call_id"] == "toolu_sub_edit_1"
    refs = edits[0]["artifact_refs"]
    # 非空 artifact_refs 正是 S1.5 `_is_write_op` 的 write-op 信号。
    assert refs and any("sub.py" in str(r.get("path", "")) for r in refs)


def test_capture_all_subagents_claude_no_original_transcript_returns_empty(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    manifests = mod.capture_all_subagents(
        main_rollout_path=tmp_path / "nope.jsonl",
        dst_root=tmp_path / "out",
        host_kind=mod.HOST_CLAUDE,
        original_transcript=None,
    )
    assert manifests == []
    assert not (tmp_path / "out").exists()


def test_capture_all_subagents_claude_no_subagents_dir_returns_empty(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    project_dir = tmp_path / "projects" / "-Users-x-repo"
    project_dir.mkdir(parents=True)
    parent = project_dir / "sess-uuid-2.jsonl"
    parent.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    manifests = mod.capture_all_subagents(
        main_rollout_path=parent,
        dst_root=tmp_path / "out",
        host_kind=mod.HOST_CLAUDE,
        original_transcript=parent,
    )
    assert manifests == []
    assert not (tmp_path / "out").exists()
