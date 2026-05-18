#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


SAMPLE_PATCH = """*** Begin Patch
*** Update File: src/foo.py
@@ -10,3 +10,5 @@
 unchanged
-old
+new
+also new
*** End Patch
"""


def test_distill_function_call_apply_patch_records_artifact_refs(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s"}},
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": SAMPLE_PATCH,
                    "call_id": "call_abc",
                },
            },
            {
                "timestamp": "t2",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_abc",
                    "output": "Patched.",
                },
            },
        ],
    )
    distilled, index = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    kinds = [item["kind"] for item in distilled]
    assert "phase_marker" in kinds  # session_meta
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    patch_call = next(item for item in distilled if item["kind"] == "tool_call")
    assert patch_call["tool"] == "apply_patch"
    assert patch_call["call_id"] == "call_abc"
    assert patch_call["artifact_refs"], "apply_patch must produce artifact_refs"
    ref = patch_call["artifact_refs"][0]
    assert ref["path"] == "src/foo.py"
    assert ref["op"] == "edit"
    assert ref["lines"] == [10, 14]
    # raw_ref byte_range covers the line
    assert patch_call["raw_ref"]["byte_range"] is not None
    start, end = patch_call["raw_ref"]["byte_range"]
    assert start < end <= rollout.stat().st_size
    assert index["kind_counts"].get("tool_call") == 1
    assert index["kind_counts"].get("tool_result") == 1


def test_distill_custom_tool_call_apply_patch_records_artifact_refs(tmp_path: Path) -> None:
    """Current Codex versions emit apply_patch under custom_tool_call/custom_tool_call_output
    (with payload.input rather than payload.arguments). Distill must handle both."""
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s"}},
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "apply_patch",
                    "input": SAMPLE_PATCH,
                    "call_id": "call_xyz",
                },
            },
            {
                "timestamp": "t2",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_xyz",
                    "output": "Success. Updated the following files:\nM src/foo.py",
                },
            },
        ],
    )
    distilled, index = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    patch_call = next(item for item in distilled if item["kind"] == "tool_call")
    assert patch_call["tool"] == "apply_patch"
    assert patch_call["call_id"] == "call_xyz"
    assert patch_call["artifact_refs"], "apply_patch via custom_tool_call must produce artifact_refs"
    ref = patch_call["artifact_refs"][0]
    assert ref["path"] == "src/foo.py"
    assert ref["op"] == "edit"
    assert ref["lines"] == [10, 14]
    tool_result = next(item for item in distilled if item["kind"] == "tool_result")
    assert tool_result["call_id"] == "call_xyz"
    assert "Success" in tool_result["summary"]
    assert index["kind_counts"].get("tool_call") == 1
    assert index["kind_counts"].get("tool_result") == 1


def test_distill_exec_command_summary_includes_cmd(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "t0",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "ls -la /tmp", "workdir": "/repo"}),
                    "call_id": "call_x",
                },
            },
        ],
    )
    distilled, _ = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    assert distilled[0]["kind"] == "tool_call"
    assert "ls -la /tmp" in distilled[0]["summary"]


def test_distill_reasoning_redacts_encrypted_blob(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "t0",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "AAAA-secret-blob-AAAA",
                },
            }
        ],
    )
    distilled, _ = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    assert distilled[0]["kind"] == "reasoning"
    assert "secret" not in distilled[0]["summary"]
    assert "encrypted" in distilled[0]["summary"]


def test_host_kind_constant_is_codex() -> None:
    distill = _load("trajectory_distill")
    assert distill.HOST_KIND == "codex"


def test_read_codex_originator_returns_value_from_session_meta(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "t0",
                "type": "session_meta",
                "payload": {"id": "s", "originator": "Codex Desktop"},
            },
            {"timestamp": "t1", "type": "event_msg", "payload": {"type": "task_started"}},
        ],
    )
    assert distill.read_codex_originator(rollout) == "Codex Desktop"


def test_read_codex_originator_none_when_field_missing(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [{"timestamp": "t0", "type": "session_meta", "payload": {"id": "s"}}],
    )
    assert distill.read_codex_originator(rollout) is None


def test_read_codex_originator_none_when_file_missing(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    assert distill.read_codex_originator(tmp_path / "nope.jsonl") is None


def test_distill_reviewer_stream(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    stdout = tmp_path / "reviewer.stdout.txt"
    stdout.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "hello reviewer"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "no issues"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    distilled = distill.distill_reviewer_stream(stdout_path=stdout, reviewer_id="codex-a")
    assert len(distilled) == 3
    assert distilled[0]["source"] == "reviewer:codex-a"
    assert distilled[0]["kind"] == "phase_marker"
    assert "hello reviewer" in distilled[1]["summary"]
    assert distilled[2]["kind"] == "phase_marker"
    assert "no issues" in distilled[2]["summary"]
