#!/usr/bin/env python3
"""Claude Code transcript distiller 单元测试。

测试 ``trajectory_distill`` 中新增的：
- ``detect_transcript_format``
- ``_claude_user_message_text``（在 trajectory_capture 模块）
- ``find_rvf_start_in_claude_jsonl``（在 trajectory_capture 模块）
- ``_distill_claude_record`` / ``distill_claude_jsonl``
"""
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


def _claude_user(text, ts="2026-05-09T10:00:00Z", uuid="u1"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": "claude-sess",
        "message": {"role": "user", "content": text},
    }


def _claude_assistant_blocks(blocks, ts="2026-05-09T10:00:01Z", uuid="a1"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": "claude-sess",
        "message": {
            "model": "claude-opus-4-7",
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": blocks,
        },
    }


def _claude_user_tool_result(tool_use_id, content, ts="2026-05-09T10:00:02Z", uuid="u2"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": "claude-sess",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": tool_use_id, "type": "tool_result", "content": content}
            ],
        },
    }


def test_detect_transcript_format_codex(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    rollout = tmp_path / "codex.jsonl"
    _write_jsonl(
        rollout,
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
        ],
    )
    assert distill.detect_transcript_format(rollout) == distill.HOST_CODEX


def test_detect_transcript_format_claude(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "plan", "sessionId": "s"},
            {"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}},
            _claude_user("hello"),
        ],
    )
    assert distill.detect_transcript_format(transcript) == distill.HOST_CLAUDE


def test_detect_transcript_format_returns_none_for_garbage(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    transcript = tmp_path / "garbage.jsonl"
    transcript.write_text(
        "\n".join(["", "not json", json.dumps({"type": "permission-mode"})] * 5),
        encoding="utf-8",
    )
    # permission-mode 不在 detector 的两个集合中，且只有它有效 → None（保险 fallback 由调用方决定）
    assert distill.detect_transcript_format(transcript) is None


def test_detect_transcript_format_missing_file_returns_none(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    assert distill.detect_transcript_format(tmp_path / "nope.jsonl") is None


def test_claude_user_message_text_string_content() -> None:
    capture = _load("trajectory_capture")
    record = _claude_user("plain text body")
    assert capture._claude_user_message_text(record) == "plain text body"


def test_claude_user_message_text_list_blocks() -> None:
    capture = _load("trajectory_capture")
    record = _claude_user(
        [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
    )
    assert capture._claude_user_message_text(record) == "first\nsecond"


def test_claude_user_message_text_skips_tool_results() -> None:
    capture = _load("trajectory_capture")
    record = _claude_user_tool_result("tu1", "stdout output here")
    # tool_result-only message must not match as a user RVF trigger
    assert capture._claude_user_message_text(record) is None


def test_find_rvf_start_in_claude_jsonl_matches_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "s"},
            _claude_user("background message", ts="2026-05-09T09:00:00Z"),
            _claude_assistant_blocks([{"type": "text", "text": "ack"}], ts="2026-05-09T09:00:01Z"),
            _claude_user(
                f"please run\n{capture.RVF_SKILL_TRIGGER}\nnow",
                ts="2026-05-09T10:00:00Z",
                uuid="trigger",
            ),
        ],
    )
    cut = capture.find_rvf_start_in_claude_jsonl(transcript)
    assert cut is not None
    assert cut.marker_matched == capture.RVF_SKILL_TRIGGER
    assert cut.line_index == 3
    assert cut.timestamp == "2026-05-09T10:00:00Z"
    pre = transcript.read_bytes()[: cut.byte_offset]
    post = transcript.read_bytes()[cut.byte_offset :]
    assert pre + post == transcript.read_bytes()
    assert capture.RVF_SKILL_TRIGGER.encode("utf-8") not in pre
    assert capture.RVF_SKILL_TRIGGER.encode("utf-8") in post


def test_find_rvf_start_in_claude_jsonl_skips_tool_result(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            # 仿场景：assistant 输出里提到 trigger 字面，user tool_result 把它回流
            _claude_assistant_blocks(
                [{"type": "text", "text": "I would mention $review-validate-fix"}],
            ),
            _claude_user_tool_result("tu1", "$review-validate-fix appears in output"),
        ],
    )
    assert capture.find_rvf_start_in_claude_jsonl(transcript) is None


def test_find_rvf_start_in_claude_jsonl_since_timestamp(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            _claude_user(
                f"first {capture.RVF_SKILL_TRIGGER}", ts="2026-05-09T08:00:00Z"
            ),
            _claude_user(
                f"second {capture.RVF_SKILL_TRIGGER}", ts="2026-05-09T10:00:00Z"
            ),
        ],
    )
    naive = capture.find_rvf_start_in_claude_jsonl(transcript)
    assert naive is not None
    assert naive.timestamp == "2026-05-09T10:00:00Z"
    bounded = capture.find_rvf_start_in_claude_jsonl(
        transcript, since_timestamp="2026-05-09T09:00:00Z"
    )
    assert bounded is not None
    assert bounded.timestamp == "2026-05-09T10:00:00Z"
    pre_only = capture.find_rvf_start_in_claude_jsonl(
        transcript, since_timestamp="2026-05-09T11:00:00Z"
    )
    assert pre_only is None


def test_distill_claude_jsonl_basic_kinds(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "summary", "summary": "Session about RVF dispatch", "leafUuid": "x"},
            _claude_user("kick off task"),
            _claude_assistant_blocks(
                [
                    {"type": "thinking", "thinking": "let me think", "signature": "sig"},
                    {"type": "text", "text": "OK starting"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/foo.py",
                            "old_string": "a",
                            "new_string": "b",
                        },
                    },
                ]
            ),
            _claude_user_tool_result("toolu_1", "edit applied"),
            _claude_assistant_blocks(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Write",
                        "input": {"file_path": "/tmp/bar.py", "content": "x = 1"},
                    },
                ],
                ts="2026-05-09T10:00:03Z",
                uuid="a2",
            ),
            _claude_user_tool_result("toolu_2", "wrote", uuid="u3"),
        ],
    )
    distilled, index = distill.distill_claude_jsonl(
        rollout_path=transcript,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    kinds = [r["kind"] for r in distilled]
    assert "phase_marker" in kinds  # summary
    assert "reasoning" in kinds  # thinking block
    assert kinds.count("message") >= 2  # user kickoff + assistant text
    assert kinds.count("tool_call") == 2  # Edit + Write
    assert kinds.count("tool_result") == 2

    edits = [r for r in distilled if r.get("tool") == "Edit"]
    assert edits and edits[0]["call_id"] == "toolu_1"
    assert edits[0]["artifact_refs"] == [
        {"path": "/tmp/foo.py", "lines": None, "op": "edit"}
    ]

    writes = [r for r in distilled if r.get("tool") == "Write"]
    assert writes and writes[0]["call_id"] == "toolu_2"
    assert writes[0]["artifact_refs"] == [
        {"path": "/tmp/bar.py", "lines": None, "op": "create"}
    ]

    results = [r for r in distilled if r["kind"] == "tool_result"]
    assert {r["call_id"] for r in results} == {"toolu_1", "toolu_2"}

    assert index["record_count"] == len(distilled)
    assert index["rollout_file"] == "rollout.jsonl"
    assert index["kind_counts"]["tool_call"] == 2


def test_distill_claude_bash_apply_patch_extracts_artifact_refs(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    transcript = tmp_path / "claude.jsonl"
    patch_text = (
        "*** Begin Patch\n"
        "*** Add File: foo/new.py\n"
        "+print('hi')\n"
        "*** End Patch"
    )
    bash_command = f"apply_patch <<'PATCH'\n{patch_text}\nPATCH\n"
    _write_jsonl(
        transcript,
        [
            _claude_assistant_blocks(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash",
                        "name": "Bash",
                        "input": {"command": bash_command},
                    }
                ]
            ),
        ],
    )
    distilled, _ = distill.distill_claude_jsonl(
        rollout_path=transcript,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    bash_calls = [r for r in distilled if r.get("tool") == "Bash"]
    assert bash_calls
    refs = bash_calls[0]["artifact_refs"]
    assert any(ref["path"] == "foo/new.py" and ref["op"] == "create" for ref in refs)


def test_distill_claude_jsonl_skips_unknown_record_types(tmp_path: Path) -> None:
    distill = _load("trajectory_distill")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "plan", "sessionId": "s"},
            {"type": "ai-title", "title": "session title"},
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "agent-name", "name": "claude"},
            _claude_user("hi"),
        ],
    )
    distilled, index = distill.distill_claude_jsonl(
        rollout_path=transcript,
        rollout_filename="rollout.jsonl",
        repo=None,
    )
    # 应当只有 user message 一条
    assert index["record_count"] == 1
    assert distilled[0]["kind"] == "message"
    assert distilled[0]["summary"] == "hi"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
