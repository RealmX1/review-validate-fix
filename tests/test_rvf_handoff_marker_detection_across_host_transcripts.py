#!/usr/bin/env python3
"""行为 + 结构 gate：Stop 事件缺 ``last_assistant_message`` 时，按 host 读 transcript 找 handoff 标记。

## 为什么需要它（F5·H5a 取证）

RVF run 的 finalize（标 units reviewed、释放 lease、清在途锁、推高水位、生成 analyze 骨架与
trajectory）只有一个触发点：Stop hook 在「本回合最后一条助手消息」里看到
``RVF_HANDOFF_FILE: <path>``。标记先从事件的 ``last_assistant_message`` 取，没有时回落读
transcript。

回落读取器此前**只认 Codex rollout**。Claude / Codex 的 Stop 事件都自带
``last_assistant_message``，所以没人发现；Kimi 的 Stop 事件只有 4 个字段（shim 补了
``transcript_path``，没补助手消息）⇒ 读 Kimi ``wire.jsonl`` 永远得到 None ⇒ **4/4 个 Kimi run
写完 handoff 却从未 finalize**（2026-08-28/29，部署版 2297c29 / f57cdf0；Stop hook 确实在跑，
标记确实在 wire 的 ``content.part`` 里）。cursor 同形（1 例），但 cursor 不是 RVF 的主 host 身份，
本 gate 不覆盖。

## 护的不变量

* **HANDOFF-1**：每个 ``KNOWN_HOST_IDENTITIES`` 都有自己的回落读取器，两两不同；host 未知仍兜底
  Codex（与 ``detect_transcript_format`` 的约定一致）。漏接一个 host 不会报错，只会让它的 run
  静默永不 finalize——所以必须是结构性断言。
* **HANDOFF-2**：只认**最近一个回合**的最终回复。上一回合的 handoff 标记不得在下一回合被
  再次认出（否则之后每次 Stop 都会被当成 handoff 回合，跳过正常的发起判定）。
* **HANDOFF-3**：推理内容（Kimi ``think`` / Claude ``thinking``）不算回复——Kimi 实测会在
  think 里复述「first line ``RVF_HANDOFF_FILE: <absolute path>``」。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"


def _run_in_repo_context(body: str) -> object:
    """子进程里跑，避免把 core / adapters 的模块级状态带进 pytest 进程（与 Kimi gate 同口径）。"""
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n" + body
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _handoff_path_from_event(event: dict) -> str | None:
    return _run_in_repo_context(
        "from rvf_handoff import handoff_path_from_event\n"
        f"found = handoff_path_from_event({event!r})\n"
        "print(json.dumps(None if found is None else str(found)))\n"
    )


# ── Kimi wire.jsonl ───────────────────────────────────────────────────────────

KIMI_METADATA = {"type": "metadata", "protocol_version": "1.4", "created_at": 1787371449575}


def _kimi_turn_prompt(text: str) -> dict:
    return {"type": "turn.prompt", "input": [{"type": "text", "text": text}], "origin": {"kind": "user"}}


def _kimi_part(turn_id: str, step: int, part: dict) -> dict:
    return {
        "type": "context.append_loop_event",
        "event": {
            "type": "content.part",
            "turnId": turn_id,
            "step": step,
            "stepUuid": f"step-{turn_id}-{step}",
            "part": part,
        },
    }


def _kimi_tool_call(turn_id: str, step: int) -> dict:
    return {
        "type": "context.append_loop_event",
        "event": {
            "type": "tool.call",
            "turnId": turn_id,
            "step": step,
            "name": "Bash",
            "args": {"command": "true"},
            "toolCallId": f"call-{turn_id}-{step}",
        },
    }


def _kimi_stop_event(wire: Path) -> dict:
    """``_kimi_hook_entry.normalize_kimi_event`` 之后的形状：有 transcript_path，没有助手消息。"""
    return {
        "hook_event_name": "Stop",
        "session_id": "session_abc",
        "cwd": "/repo",
        "stop_hook_active": False,
        "source": {"provider": "kimi-code", "plugin": "review-validate-fix"},
        "transcript_path": str(wire),
        "session_file": str(wire),
    }


def test_kimi_handoff_marker_in_the_final_reply_names_the_handoff_file(tmp_path: Path) -> None:
    handoff = tmp_path / "run" / "artifacts" / "handoff.md"
    wire = _write_jsonl(
        tmp_path / "wire.jsonl",
        [
            KIMI_METADATA,
            _kimi_turn_prompt("跑 RVF"),
            _kimi_part("10", 33, {"type": "text", "text": "先派 reviewer。"}),
            _kimi_tool_call("10", 33),
            _kimi_part("10", 34, {"type": "think", "think": "All done. Final reply per the template."}),
            _kimi_part("10", 34, {"type": "text", "text": f"RVF_HANDOFF_FILE: {handoff}\n\n两条已修复。"}),
        ],
    )
    assert _handoff_path_from_event(_kimi_stop_event(wire)) == str(handoff)


def test_kimi_handoff_marker_from_an_earlier_turn_is_not_reported_again(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    wire = _write_jsonl(
        tmp_path / "wire.jsonl",
        [
            KIMI_METADATA,
            _kimi_turn_prompt("跑 RVF"),
            _kimi_part("10", 34, {"type": "text", "text": f"RVF_HANDOFF_FILE: {handoff}"}),
            _kimi_turn_prompt("顺便看下别的"),
            _kimi_part("11", 1, {"type": "text", "text": "看完了，没有问题。"}),
        ],
    )
    assert _handoff_path_from_event(_kimi_stop_event(wire)) is None


def test_kimi_final_turn_without_reply_text_yields_no_latest_message(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    wire = _write_jsonl(
        tmp_path / "wire.jsonl",
        [
            KIMI_METADATA,
            _kimi_turn_prompt("跑 RVF"),
            _kimi_part("10", 34, {"type": "text", "text": f"RVF_HANDOFF_FILE: {handoff}"}),
            _kimi_turn_prompt("中断的一轮"),
            _kimi_tool_call("11", 1),
        ],
    )
    latest = _run_in_repo_context(
        "from pathlib import Path\n"
        "from rvf_handoff import latest_assistant_message\n"
        f"print(json.dumps(latest_assistant_message(Path({str(wire)!r}))))\n"
    )
    assert latest is None


def test_kimi_reasoning_that_quotes_the_marker_is_not_the_reply(tmp_path: Path) -> None:
    wire = _write_jsonl(
        tmp_path / "wire.jsonl",
        [
            KIMI_METADATA,
            _kimi_turn_prompt("跑 RVF"),
            _kimi_part(
                "10", 34,
                {"type": "think", "think": "模板要求首行：\nRVF_HANDOFF_FILE: /abs/handoff.md"},
            ),
            _kimi_part("10", 34, {"type": "text", "text": "还差一步，先不写 handoff。"}),
        ],
    )
    assert _handoff_path_from_event(_kimi_stop_event(wire)) is None


# ── Claude Code transcript ────────────────────────────────────────────────────


def _claude_user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _claude_tool_result() -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
        },
    }


def _claude_assistant(message_id: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "message": {"id": message_id, "role": "assistant", "content": blocks},
    }


def _claude_stop_event_without_last_assistant_message(transcript: Path) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "cwd": "/repo",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }


def test_claude_transcript_fallback_finds_the_marker_in_the_final_reply(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    transcript = _write_jsonl(
        tmp_path / "claude.jsonl",
        [
            _claude_user("跑 RVF"),
            _claude_assistant("msg_1", [{"type": "text", "text": "先派 reviewer。"}]),
            _claude_assistant("msg_1", [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]),
            _claude_tool_result(),
            _claude_assistant("msg_2", [{"type": "thinking", "thinking": "写 handoff"}]),
            _claude_assistant("msg_2", [{"type": "text", "text": f"RVF_HANDOFF_FILE: {handoff}"}]),
            _claude_assistant("msg_2", [{"type": "text", "text": "两条已修复。"}]),
        ],
    )
    event = _claude_stop_event_without_last_assistant_message(transcript)
    assert _handoff_path_from_event(event) == str(handoff)


def test_claude_transcript_fallback_does_not_resurrect_an_earlier_turn(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    transcript = _write_jsonl(
        tmp_path / "claude.jsonl",
        [
            _claude_user("跑 RVF"),
            _claude_assistant("msg_1", [{"type": "text", "text": f"RVF_HANDOFF_FILE: {handoff}"}]),
            _claude_user("再看一眼别的"),
            _claude_assistant("msg_2", [{"type": "text", "text": "看完了。"}]),
        ],
    )
    event = _claude_stop_event_without_last_assistant_message(transcript)
    assert _handoff_path_from_event(event) is None


# ── Codex rollout（既有行为，回归钉住）─────────────────────────────────────────


def test_codex_rollout_fallback_still_reads_the_latest_agent_message(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    rollout = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread-1"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "先派 reviewer。"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": f"RVF_HANDOFF_FILE: {handoff}"}},
        ],
    )
    event = {"session_id": "thread-1", "cwd": "/repo", "transcript_path": str(rollout)}
    assert _handoff_path_from_event(event) == str(handoff)


# ── HANDOFF-1：结构性 ─────────────────────────────────────────────────────────


def test_every_known_host_identity_has_its_own_latest_assistant_message_reader() -> None:
    result = _run_in_repo_context(
        "import rvf_handoff\n"
        "from core.host_adapter import KNOWN_HOST_IDENTITIES\n"
        "readers = {h: rvf_handoff.latest_assistant_message_reader_for_host(h)\n"
        "           for h in sorted(KNOWN_HOST_IDENTITIES)}\n"
        "print(json.dumps({\n"
        "  'readers': {h: f'{r.__module__}.{r.__name__}' for h, r in readers.items()},\n"
        "  'unknown': (lambda r: f'{r.__module__}.{r.__name__}')(\n"
        "      rvf_handoff.latest_assistant_message_reader_for_host(None)),\n"
        "  'codex': (lambda r: f'{r.__module__}.{r.__name__}')(\n"
        "      rvf_handoff.latest_assistant_message_reader_for_host('codex')),\n"
        "}))\n"
    )
    readers = result["readers"]
    assert len(set(readers.values())) == len(readers), readers
    assert readers["kimi_code"].startswith("adapters.kimi."), readers
    assert readers["claude_code"].startswith("adapters.claude_code."), readers
    assert result["unknown"] == result["codex"], "host 未知仍兜底 Codex"
