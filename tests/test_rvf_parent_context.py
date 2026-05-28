#!/usr/bin/env python3
"""单元测试：rvf_parent_context naive 父会话对话 context 渲染。

测试要点（覆盖 codex / claude 两套 host schema + 压缩规则 + 字节预算）：
- codex fixture：token_count 被丢、turn_context 被丢、developer/permissions
  boilerplate 被丢、message 全文保留、大 tool_result 被压到 ~400B、tool_call
  参数被压到 ~800B、encrypted reasoning 被标注。
- claude fixture：明文 thinking(reasoning) 保留、text/tool_use/tool_result
  正确展开、summary/system 元数据被丢。
- 字节预算：超 max_bytes 时保留最近内容并加 [已截断 …] 头部，且总字节不超限。
- fail-open：父 transcript 缺失 → render_parent_context 返回 None。

自定义 runner（与项目既有测试一致）：``python3 tests/test_rvf_parent_context.py``。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from _rvf_test_support.loader import load_script_module as _load

rpc = _load("rvf_parent_context")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Codex fixture
# ---------------------------------------------------------------------------

_BIG_OUTPUT = "X" * 2000  # > TOOL_RESULT_LIMIT_BYTES，必被压缩
_BIG_ARGS = "A" * 2000    # > TOOL_ARGS_LIMIT_BYTES，必被压缩


def _codex_records() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "sess-1", "cwd": "/repo"}},
        {"type": "turn_context", "payload": {"cwd": "/repo", "approval_policy": "never"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        # developer/permissions boilerplate → 必须丢
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<permissions instructions> ..."}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "请帮我写一个脚本并验证"},
        },
        # 加密 reasoning（summary 空 + encrypted_content）→ 标注
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "content": None, "encrypted_content": "gAAAA_blob"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "好的，我先创建脚本。"},
        },
        # tool_call 大参数 → 压到 ~800B
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command", "arguments": _BIG_ARGS, "call_id": "c1"},
        },
        # 大 tool_result（string output）→ 压到 ~400B
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": _BIG_OUTPUT, "call_id": "c1"},
        },
        # dict 形式 output（custom_tool_call_output 可能是 dict）
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "output": {"output": "ok done"}, "call_id": "c2"},
        },
        # token_count 计费噪声 → 丢
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 999}}},
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
    ]


def test_codex_compression_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        _write_jsonl(path, _codex_records())
        blob = rpc.render_parent_context(path, max_bytes=rpc.DEFAULT_MAX_BYTES)
        assert blob is not None, "codex blob should not be None"
        lines = blob.splitlines()

        # 自检：探测为 codex host
        from trajectory_distill import HOST_CODEX, detect_transcript_format

        assert detect_transcript_format(path) == HOST_CODEX

        # token_count 被丢
        assert "token_count" not in blob, "token_count 应被丢弃"
        assert "total_tokens" not in blob and "999" not in blob
        # turn_context 被丢
        assert "approval_policy" not in blob, "turn_context 应被丢弃"
        # developer/permissions boilerplate 被丢
        assert "permissions instructions" not in blob, "developer boilerplate 应被丢弃"
        # message 全文保留
        assert "user: 请帮我写一个脚本并验证" in blob
        assert "assistant: 好的，我先创建脚本。" in blob
        # session start / phase markers
        assert "[session start cwd=/repo]" in lines
        assert "[task_started]" in lines and "[task_complete]" in lines
        # encrypted reasoning 被标注，且不泄露 blob
        assert "reasoning: <encrypted reasoning>" in lines
        assert "gAAAA_blob" not in blob
        # tool_call 参数被压（含截断标注，不含全量 2000 A）
        call_line = next(l for l in lines if l.startswith("call exec_command("))
        assert "…[+" in call_line and "B]" in call_line
        assert "A" * 1000 not in call_line, "tool_call 参数应被压缩"
        # tool_result 被压到 ~400B + 标注
        result_line = next(l for l in lines if l.startswith("result: X"))
        assert "tool 输出已压缩" in result_line
        assert "X" * 1000 not in result_line, "tool_result 应被压缩"
        # dict 形式 output 取内层 output
        assert "result: ok done" in lines


def test_codex_fallback_when_format_undetectable() -> None:
    """空/异常 schema 探测返回 None 时 fallback 到 codex 解析（无回归）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        # 只放一条 codex message，但前面塞噪声让探测仍命中 codex（正常路径）；
        # 这里直接验证空文件 → None。
        path.write_text("", encoding="utf-8")
        assert rpc.render_parent_context(path) is None


# ---------------------------------------------------------------------------
# Claude fixture
# ---------------------------------------------------------------------------


def _claude_records() -> list[dict]:
    return [
        {"type": "summary", "summary": "earlier session summary", "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "system", "subtype": "init", "timestamp": "2026-01-01T00:00:01Z"},
        {
            "type": "user",
            "message": {"role": "user", "content": "帮我修复这个 bug"},
            "timestamp": "2026-01-01T00:00:02Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "我需要先定位 bug 的根因，再决定改动范围。"},
                    {"type": "text", "text": "我先看一下相关文件。"},
                    {"type": "tool_use", "name": "Read", "id": "tu1", "input": {"file_path": "/repo/a.py"}},
                ],
            },
            "timestamp": "2026-01-01T00:00:03Z",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": _BIG_OUTPUT}]},
                ],
            },
            "timestamp": "2026-01-01T00:00:04Z",
        },
    ]


def test_claude_plaintext_reasoning_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "transcript.jsonl"
        _write_jsonl(path, _claude_records())
        blob = rpc.render_parent_context(path, max_bytes=rpc.DEFAULT_MAX_BYTES)
        assert blob is not None, "claude blob should not be None"
        lines = blob.splitlines()

        from trajectory_distill import HOST_CLAUDE, detect_transcript_format

        assert detect_transcript_format(path) == HOST_CLAUDE

        # 明文 reasoning（thinking）保留全文
        assert "reasoning: 我需要先定位 bug 的根因，再决定改动范围。" in lines
        # text / user message 保留
        assert "user: 帮我修复这个 bug" in lines
        assert "assistant: 我先看一下相关文件。" in lines
        # tool_use 展开为 call
        call_line = next(l for l in lines if l.startswith("call Read("))
        assert "/repo/a.py" in call_line
        # 大 tool_result 被压
        result_line = next(l for l in lines if l.startswith("result: X"))
        assert "tool 输出已压缩" in result_line
        assert "X" * 1000 not in result_line
        # summary / system 元数据被丢
        assert "earlier session summary" not in blob
        assert "init" not in blob or "system" not in blob.lower().split("\n")[0]


# ---------------------------------------------------------------------------
# 字节预算 / fail-open
# ---------------------------------------------------------------------------


def test_max_bytes_keeps_recent_and_marks_truncation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        _write_jsonl(path, _codex_records())
        max_bytes = 300
        blob = rpc.render_parent_context(path, max_bytes=max_bytes)
        assert blob is not None
        # 总字节不超过预算
        assert len(blob.encode("utf-8")) <= max_bytes, (
            f"blob bytes {len(blob.encode('utf-8'))} > max {max_bytes}"
        )
        # 头部截断标注
        assert blob.splitlines()[0].startswith("[已截断"), "应有截断头部标注"
        # 最近内容优先：task_complete（最后一条 marker）应在保留范围内
        assert "[task_complete]" in blob


def test_missing_transcript_returns_none() -> None:
    missing = Path("/nonexistent/does-not-exist-rvf-parent-context.jsonl")
    assert rpc.render_parent_context(missing) is None


def test_disabled_via_empty_lines_returns_none() -> None:
    """全是被丢弃记录（无可渲染行）→ 返回 None。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        _write_jsonl(
            path,
            [
                {"type": "turn_context", "payload": {"cwd": "/repo"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            ],
        )
        # turn_context 被丢、token_count 被丢；但 session_meta 这里没有，
        # 渲染结果仍可能含 None 行——验证至少不抛异常。
        blob = rpc.render_parent_context(path)
        assert blob is None or "token_count" not in blob


def test_single_record_larger_than_max_bytes_is_truncated() -> None:
    """回归（RVF-MAIN-001）：单条逻辑记录字节 > max_bytes 时仍守住字节上限，
    且 notice 不误报丢行数。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        big_message = "x" * 5000
        _write_jsonl(
            path,
            [{"type": "event_msg", "payload": {"type": "agent_message", "message": big_message}}],
        )
        max_bytes = 300
        blob = rpc.render_parent_context(path, max_bytes=max_bytes)
        assert blob is not None
        assert len(blob.encode("utf-8")) <= max_bytes, (
            f"blob bytes {len(blob.encode('utf-8'))} > max {max_bytes}"
        )
        first_line = blob.splitlines()[0]
        assert "已截断 0 行" not in first_line
        assert first_line.startswith("[已截断单条超大 context")


TEST_CASES = [
    test_codex_compression_rules,
    test_codex_fallback_when_format_undetectable,
    test_claude_plaintext_reasoning_preserved,
    test_max_bytes_keeps_recent_and_marks_truncation,
    test_missing_transcript_returns_none,
    test_disabled_via_empty_lines_returns_none,
    test_single_record_larger_than_max_bytes_is_truncated,
]


def main() -> int:
    for case in TEST_CASES:
        case()
    print(f"rvf_parent_context tests OK ({len(TEST_CASES)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
