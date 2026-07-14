#!/usr/bin/env python3
from __future__ import annotations

import json
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


from _rvf_test_support.loader import load_script_module as _load


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


# ── 新 Codex（gpt-5.6-sol 世代）schema 漂移：编辑工具统一成 custom_tool_call
# name="exec"、apply_patch 以 JS 文本内嵌 input、旧 apply_patch 工具调用消失。归属真相源
# 转为跨版本稳定的 event_msg/patch_apply_end。以下测试锁定 adapter 对新旧两代的处理。


def _new_codex_patch_apply_end_record(path: str) -> dict:
    """gpt-5.6-sol 世代的 patch_apply_end event（结构化、带 success/call_id/turn_id）。"""
    return {
        "timestamp": "t1",
        "type": "event_msg",
        "payload": {
            "type": "patch_apply_end",
            "call_id": "exec-abc",
            "turn_id": "turn-1",
            "success": True,
            "changes": {
                path: {
                    "type": "update",
                    "unified_diff": "@@ -10,3 +10,5 @@\n unchanged\n-old\n+new\n+also new\n",
                }
            },
            "status": "completed",
        },
    }


def test_distill_new_codex_patch_apply_end_records_artifact_refs(tmp_path: Path) -> None:
    """新 Codex：apply_patch 只以 exec custom_tool_call（无 artifact）出现，编辑真相源是
    patch_apply_end event。adapter 必须由 patch_apply_end 合成补丁文本、恢复 artifact_refs，
    否则 rvf-analyze 的 issue↔patch 因果归属在新-Codex run 上全盲。"""
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
                    "name": "exec",
                    "input": 'const r = await tools.apply_patch("*** Begin Patch\\n*** Update File: src/foo.py\\n...");',
                    "call_id": "call_exec",
                },
            },
            _new_codex_patch_apply_end_record("src/foo.py"),
        ],
    )
    distilled, index = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    patch_results = [
        item
        for item in distilled
        if item["kind"] == "tool_result" and item.get("tool") == "apply_patch"
    ]
    assert patch_results, "patch_apply_end must distill into an apply_patch tool_result"
    result = patch_results[0]
    assert result["call_id"] == "exec-abc"
    assert result["artifact_refs"], "new-Codex patch_apply_end must recover artifact_refs"
    ref = result["artifact_refs"][0]
    assert ref["path"] == "src/foo.py"
    assert ref["op"] == "edit"


def test_distill_old_codex_patch_apply_end_suppressed_when_apply_patch_tool_call_present(
    tmp_path: Path,
) -> None:
    """旧 Codex 对同一编辑同时发 apply_patch 工具调用与 patch_apply_end。工具调用已带
    artifact_refs，patch_apply_end 保持旧行为（不进 distilled），避免同一编辑被双重归属，
    且旧格式 trajectory 逐字节不变。"""
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
                    "name": "apply_patch",
                    "input": SAMPLE_PATCH,
                    "call_id": "call_xyz",
                },
            },
            _new_codex_patch_apply_end_record("src/foo.py"),
        ],
    )
    distilled, _index = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    # apply_patch 工具调用产出 artifact_refs（旧路径不变）。
    tool_calls = [item for item in distilled if item["kind"] == "tool_call" and item.get("tool") == "apply_patch"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["artifact_refs"]
    # patch_apply_end 不产生第二条 apply_patch tool_result（避免双重归属）。
    apply_patch_results = [
        item for item in distilled if item["kind"] == "tool_result" and item.get("tool") == "apply_patch"
    ]
    assert apply_patch_results == [], apply_patch_results


def test_distill_resume_mixed_new_path_patch_apply_end_not_suppressed(tmp_path: Path) -> None:
    """RVF-001 F1（adapter 侧）：resume / 切换模型的混合 session——前段旧式 apply_patch 工具调用
    编辑 src/foo.py、后段新式 patch_apply_end 编辑 src/bar.py（无对应旧式调用）。旧的全局布尔
    gate（存在任意旧式调用即抑制所有 patch_apply_end）会丢弃后段 bar.py 的 artifact_refs → 新
    编辑在 rvf-analyze 因果链缺失。路径级去重后，未被旧式覆盖的 bar.py 必须照常产出。"""
    distill = _load("trajectory_distill")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s"}},
            # 前段旧式：apply_patch 工具调用编辑 src/foo.py。
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": SAMPLE_PATCH,  # *** Update File: src/foo.py
                    "call_id": "call_foo",
                },
            },
            # 后段新式：patch_apply_end 编辑 src/bar.py（旧式未覆盖的新路径）。
            _new_codex_patch_apply_end_record("src/bar.py"),
        ],
    )
    distilled, _index = distill.distill_codex_jsonl(
        rollout_path=rollout,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    # 旧式 apply_patch 工具调用仍产出 tool_call（src/foo.py，路径不变）。
    foo_calls = [
        item for item in distilled
        if item["kind"] == "tool_call" and item.get("tool") == "apply_patch"
        and any((ref.get("path") == "src/foo.py") for ref in (item.get("artifact_refs") or []))
    ]
    assert len(foo_calls) == 1, foo_calls
    # 后段 bar.py 的 patch_apply_end 未被抑制：产出 apply_patch tool_result 且 artifact_refs 恢复。
    bar_results = [
        item for item in distilled
        if item["kind"] == "tool_result" and item.get("tool") == "apply_patch"
        and any((ref.get("path") == "src/bar.py") for ref in (item.get("artifact_refs") or []))
    ]
    assert len(bar_results) == 1, bar_results
    assert bar_results[0]["call_id"] == "exec-abc"


def test_synthesize_apply_patch_text_from_patch_apply_end_shapes(tmp_path: Path) -> None:
    """共享 helper：仅对 success 记录、按 update/add/delete 合成可解析的 apply_patch 文本。"""
    _load("trajectory_distill")  # 确保 pyroot 在 sys.path 上
    from core.transcript.patch_parsing import (  # noqa: PLC0415
        parse_apply_patch_operations_without_repo,
        synthesize_apply_patch_text_from_patch_apply_end,
    )

    update_payload = {
        "type": "patch_apply_end",
        "success": True,
        "changes": {"a.py": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"}},
    }
    text = synthesize_apply_patch_text_from_patch_apply_end(update_payload)
    assert text is not None and "*** Update File: a.py" in text
    ops, paths = parse_apply_patch_operations_without_repo(text, 0)
    assert paths == {"a.py"} and ops[0]["operation"] == "update"

    add_payload = {
        "type": "patch_apply_end",
        "success": True,
        "changes": {"b.py": {"type": "add", "content": "line1\nline2\n"}},
    }
    add_text = synthesize_apply_patch_text_from_patch_apply_end(add_payload)
    assert add_text is not None and "*** Add File: b.py" in add_text
    assert "+line1" in add_text and "+line2" in add_text

    delete_payload = {
        "type": "patch_apply_end",
        "success": True,
        "changes": {"c.py": {"type": "delete", "content": "gone\n"}},
    }
    del_text = synthesize_apply_patch_text_from_patch_apply_end(delete_payload)
    assert del_text is not None and "*** Delete File: c.py" in del_text
    assert "-gone" in del_text

    # 失败/空 → None（不贡献归属）。
    assert synthesize_apply_patch_text_from_patch_apply_end({"type": "patch_apply_end", "success": False, "changes": {"a": {"type": "update", "unified_diff": "x"}}}) is None
    assert synthesize_apply_patch_text_from_patch_apply_end({"type": "patch_apply_end", "success": True}) is None
    assert synthesize_apply_patch_text_from_patch_apply_end({"type": "patch_apply_end", "success": True, "changes": {}}) is None
