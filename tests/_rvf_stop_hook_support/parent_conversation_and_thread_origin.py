#!/usr/bin/env python3
"""parent conversation / thread origin 解析与标签 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_parent_conversation_origin_prefers_app_server_chat_name',
    'test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed',
    'test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed',
    'test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed',
    'test_parent_conversation_origin_uses_session_index_when_chat_lookup_fails',
    'test_parent_conversation_origin_quotes_first_user_prompt_when_chat_lookup_fails',
    'test_parent_conversation_origin_uses_stable_ref_when_prompt_fallback_unavailable',
    'test_parent_conversation_origin_labels_claude_host_when_codex_lookups_miss',
    'test_parent_origin_prompt_block_uses_claude_heading_for_claude_host',
    'test_parent_thread_name_from_app_server_reads_thread_name',
    'test_parent_thread_path_for_origin_returns_codex_validated_path',
    'test_parent_thread_path_for_origin_falls_back_to_existing_file',
    'test_parent_thread_path_for_origin_emits_diagnostic_when_event_empty',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_parent_conversation_origin_prefers_app_server_chat_name(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    origin = module.parent_conversation_origin(
        parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
        parent_thread_path=transcript,
        run_id="rvf-20260501T032651Z-stop-hook-562915ad",
        parent_thread_name="Find RVF_STOP_HOOK behavior",
        name_lookup={"name": "Find RVF_STOP_HOOK behavior", "source": "desktop-control"},
    )

    assert origin["label"] == "Find RVF_STOP_HOOK behavior"
    assert origin["name_source"] == "app_server_name"
    assert origin["task_title"] == "RVF from Find RVF_STOP_HOOK behavior run 562915ad"
    assert origin["codex_url"] == "codex://local/019de191-ba6c-7b13-9874-65eeabb6a6a7"
    assert origin["transcript_file"] == transcript.name


def test_parent_conversation_origin_quotes_first_user_prompt_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    first_prompt = (
        "for the path in RVF hook fork to cline kanban, we need way to trace "
        "which original conversation the fork comes from"
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": first_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["label"] == '"for the path in RVF hook fork to cline kanban, we need way t"'
    assert origin["name_source"] == "first_user_prompt_fallback"
    assert origin["task_title"] == (
        'RVF from "for the path in RVF hook fork to cline kanban, we need way t" run 562915ad'
    )


def test_parent_conversation_origin_strips_stitched_codex_context_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    user_prompt = (
        "currently the fallback chat session name in handoff as well as cline-task "
        "is incorrectly using the stitched prompt"
    )
    stitched_prompt = (
        "# AGENTS.md instructions for /Users/bominzhang/Documents/GitHub/review-validate-fix\n\n"
        "<INSTRUCTIONS>\n"
        "你应该默认使用中文作为主要语言进行回复。\n"
        "</INSTRUCTIONS><environment_context>\n"
        "  <cwd>/Users/bominzhang/Documents/GitHub/review-validate-fix</cwd>\n"
        "</environment_context>\n"
        f"{user_prompt}"
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": stitched_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    expected_excerpt = module.single_line_excerpt(
        user_prompt,
        module.DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS,
    )
    assert origin["label"] == f'"{expected_excerpt}"'
    assert "AGENTS.md instructions" not in origin["task_title"]
    assert origin["name_source"] == "first_user_prompt_fallback"


def test_parent_conversation_origin_skips_context_only_user_messages_when_chat_unnamed(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    context_only = (
        "# AGENTS.md instructions for /Users/bominzhang/Documents/GitHub/review-validate-fix\n\n"
        "<INSTRUCTIONS>\n"
        "project instructions\n"
        "</INSTRUCTIONS><environment_context>\n"
        "  <cwd>/Users/bominzhang/Documents/GitHub/review-validate-fix</cwd>\n"
        "</environment_context>\n"
    )
    user_prompt = "please run review validate fix for the current change"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": context_only},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "thread_found": True, "source": "desktop-control"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["label"] == f'"{user_prompt}"'
    assert "AGENTS.md instructions" not in origin["task_title"]


def test_parent_conversation_origin_uses_session_index_when_chat_lookup_fails(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    session_id = "019de191-ba6c-7b13-9874-65eeabb6a6a7"
    transcript = tmp_path / f"rollout-2026-05-01T11-25-17-{session_id}.jsonl"
    session_index = tmp_path / "session_index.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "this prompt is lower priority"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_index.write_text(
        json.dumps({"id": session_id, "thread_name": "Trace original chat names"})
        + "\n",
        encoding="utf-8",
    )

    original_session_index = os.environ.get("CODEX_SESSION_INDEX_PATH")
    try:
        os.environ["CODEX_SESSION_INDEX_PATH"] = str(session_index)
        origin = module.parent_conversation_origin(
            parent_session_id=session_id,
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "source": "unavailable", "error": "socket unavailable"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["label"] == "Trace original chat names"
    assert origin["name_source"] == "session_index_thread_name"
    assert origin["task_title"] == "RVF from Trace original chat names run 562915ad"


def test_parent_conversation_origin_quotes_first_user_prompt_when_chat_lookup_fails(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "this prompt should be quoted"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "source": "unavailable", "error": "socket unavailable"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["label"] == '"this prompt should be quoted"'
    assert origin["name_source"] == "first_user_prompt_fallback"
    assert origin["task_title"] == 'RVF from "this prompt should be quoted" run 562915ad'


def test_parent_conversation_origin_uses_stable_ref_when_prompt_fallback_unavailable(
    tmp_path: Path,
) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "rollout-2026-05-01T11-25-17-019de191-ba6c-7b13-9874-65eeabb6a6a7.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "019de191-ba6c-7b13-9874-65eeabb6a6a7"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id="019de191-ba6c-7b13-9874-65eeabb6a6a7",
            parent_thread_path=transcript,
            run_id="rvf-20260501T032651Z-stop-hook-562915ad",
            name_lookup={"name": None, "source": "unavailable", "error": "socket unavailable"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["label"] == "Codex 2026-05-01T11-25-17 019de191"
    assert origin["name_source"] == "session_ref_fallback"
    assert '"' not in origin["task_title"]
    # host=Codex 兜底无回归：host_kind 标 Codex、codex:// URL 仍铸造。
    assert origin["host_kind"] == module.HOST_CODEX
    assert origin["codex_url"] == "codex://local/019de191-ba6c-7b13-9874-65eeabb6a6a7"


def test_parent_conversation_origin_labels_claude_host_when_codex_lookups_miss(
    tmp_path: Path,
) -> None:
    """Claude Code 会话：前置 Codex-only lookup 全落空时，前缀应为 Claude 而非 Codex，
    且不再铸造 codex:// URL（复现并锁死 board.json 里 ``RVF from Codex … / agentId=claude``
    的矛盾根因）。"""
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    session_id = "57f1d2ff-8470-48e5-a259-0b51e2002603"
    # Claude transcript：文件名是裸 UUID（非 rollout-…），记录用 Claude schema
    # （type=user/assistant），Codex 侧 first_user_message 解析不到 → 落到 host-aware 兜底。
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "codex 解析器读不到"}})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "ok"}})
        + "\n",
        encoding="utf-8",
    )

    original_session_index = isolate_codex_session_index(tmp_path)
    try:
        origin = module.parent_conversation_origin(
            parent_session_id=session_id,
            parent_thread_path=transcript,
            run_id="rvf-20260620T153456Z-stop-hook-134fa44d",
            name_lookup={"name": None, "source": "unavailable", "error": "socket unavailable"},
        )
    finally:
        restore_env_var("CODEX_SESSION_INDEX_PATH", original_session_index)

    assert origin["host_kind"] == module.HOST_CLAUDE
    assert origin["name_source"] == "session_ref_fallback"
    assert origin["label"] == "Claude 57f1d2ff"
    assert origin["task_title"] == "RVF from Claude 57f1d2ff run 134fa44d"
    assert "Codex" not in origin["task_title"]
    assert origin["codex_url"] is None


def test_parent_origin_prompt_block_uses_claude_heading_for_claude_host(tmp_path: Path) -> None:
    """prompt-block 人类可读文案随 host_kind 走：Claude 会话不应再写 'Original Codex …'
    标题，codex_url 缺失时渲染 <unavailable>。"""
    module = load_hook_module()
    block = module.parent_origin_prompt_block(
        parent_origin={
            "label": "Claude 57f1d2ff",
            "name_source": "session_ref_fallback",
            "host_kind": module.HOST_CLAUDE,
            "codex_url": None,
            "session_id": "57f1d2ff-8470-48e5-a259-0b51e2002603",
            "transcript_path": "/Users/x/.claude/projects/p/57f1d2ff.jsonl",
            "transcript_file": "57f1d2ff.jsonl",
        },
        origin_path="/tmp/origin.json",
    )
    assert "Original Claude Code conversation metadata:" in block
    assert "Original Codex conversation metadata:" not in block
    assert "original Claude Code conversation name/ref" in block
    assert "RVF_PARENT_CODEX_URL: <unavailable>" in block
    # Codex host 仍写 'Original Codex …'（兜底口径，无回归）。
    codex_block = module.parent_origin_prompt_block(
        parent_origin={
            "label": "Codex 019de191",
            "name_source": "session_ref_fallback",
            "host_kind": module.HOST_CODEX,
            "codex_url": "codex://local/019de191",
        },
        origin_path="/tmp/origin.json",
    )
    assert "Original Codex conversation metadata:" in codex_block
    assert "RVF_PARENT_CODEX_URL: codex://local/019de191" in codex_block


def test_parent_thread_name_from_app_server_reads_thread_name(tmp_path: Path) -> None:
    module = load_hook_module()
    # S9a 后 app-server client 等 adapter-内部符号已迁出引擎，monkeypatch 须打在 adapter 模块上
    # （引擎只 re-import 公开入口 parent_thread_name_from_app_server，其内部调用解析的是 adapter 自身的 globals）。
    import adapters.codex.codex_gui_fork_app_server_bridge as appserver
    socket_path = tmp_path / "app-server.sock"
    calls: list[tuple[str, dict[str, object] | None]] = []
    notifications: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, socket: Path) -> None:
            assert socket == socket_path
            self.notifications: list[dict[str, object]] = []

        def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
            calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "parent-thread",
                        "name": "Find RVF_STOP_HOOK behavior",
                    }
                }
            raise AssertionError(method)

        def send_json(self, payload: dict[str, object]) -> None:
            notifications.append(payload)

        def close(self) -> None:
            pass

    original_client = appserver.AppServerWebSocket
    original_select = appserver.select_existing_app_server_socket_for_metadata
    try:
        appserver.AppServerWebSocket = FakeClient
        appserver.select_existing_app_server_socket_for_metadata = lambda: (
            socket_path,
            "desktop-control",
            {},
        )
        lookup = module.parent_thread_name_from_app_server("parent-thread", str(tmp_path))
    finally:
        appserver.AppServerWebSocket = original_client
        appserver.select_existing_app_server_socket_for_metadata = original_select

    assert lookup["name"] == "Find RVF_STOP_HOOK behavior"
    assert lookup["thread_found"] is True
    assert lookup["source"] == "desktop-control"
    assert lookup["method"] == "thread/read"
    assert calls[0][0] == "initialize"
    assert calls[1] == ("thread/read", {"threadId": "parent-thread", "includeTurns": False})
    assert notifications == [{"method": "initialized"}]


def test_parent_thread_path_for_origin_returns_codex_validated_path(tmp_path: Path) -> None:
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "codex-sess"}}) + "\n",
        encoding="utf-8",
    )
    event = {"transcript_path": str(rollout)}
    result = module.parent_thread_path_for_origin(event)
    assert result == rollout.resolve()


def test_parent_thread_path_for_origin_falls_back_to_existing_file(tmp_path: Path) -> None:
    """Claude transcript：file 存在但 session_meta 校验失败 → 走 fallback。"""
    module = load_hook_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps({"type": "permission-mode", "permissionMode": "plan", "sessionId": "claude"})
        + "\n",
        encoding="utf-8",
    )
    event = {"transcript_path": str(transcript)}
    ledger = module.start_run(component="stop-hook-test", run_dir=tmp_path / "run")
    result = module.parent_thread_path_for_origin(
        event, ledger=ledger, repo=str(tmp_path), cwd=str(tmp_path)
    )
    assert result == transcript.resolve()
    events = _read_ledger_events(ledger)
    assert any(
        e.get("event") == "origin_metadata_transcript_path_fallback" for e in events
    )


def test_parent_thread_path_for_origin_emits_diagnostic_when_event_empty(tmp_path: Path) -> None:
    module = load_hook_module()
    ledger = module.start_run(component="stop-hook-test", run_dir=tmp_path / "run")
    result = module.parent_thread_path_for_origin(
        {}, ledger=ledger, repo=str(tmp_path), cwd=str(tmp_path)
    )
    assert result is None
    events = _read_ledger_events(ledger)
    assert any(
        e.get("event") == "origin_metadata_missing_transcript_path" for e in events
    )

