#!/usr/bin/env python3
"""``resolve_host_adapter``——把 ``detect_transcript_format`` 的 host 探测结果装配成
具体 ``HostAdapter`` bundle 注入 core（S10 装配点 / HostAdapter 注入面的 resolver）。

复用现成 facade 范式（如 ``subagent_capture`` / ``rvf_parent_context``）：本模块是
RVF 中**唯一**同时认识 Codex 与 Claude 两个 concrete bundle 的地方（assembly/entry
层）。core 业务逻辑模块只拿被注入的 ``HostAdapter`` 实例、绝不在 core 内做 host 判断
或装配，从而守住 ``core/`` 的 host-中性不变量（R-1 退出门）。

装配口径（与 ``default_cline_kanban_agent_id`` / ``parent_conversation_origin`` 的既有
host 兜底一致）：

- ``HOST_CLAUDE`` → ``ClaudeCodeHostAdapter``。
- ``HOST_CODEX`` / host 未知（``None``，探测失败或未识别格式）→ ``CodexHostAdapter``，
  沿用历史「无法识别 → 退回 Codex」口径，保证既有 Codex-only 用例零回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* / adapters.* import

from core.host_adapter import HOST_CLAUDE, HostAdapter  # noqa: E402
from adapters.codex.codex_host_adapter import CodexHostAdapter  # noqa: E402
from adapters.claude_code.claude_code_host_adapter import (  # noqa: E402
    ClaudeCodeHostAdapter,
)


def resolve_host_adapter(host: str | None) -> HostAdapter:
    """按 ``detect_transcript_format`` 的 host 字符串装配并返回具体 ``HostAdapter``。

    ``host`` 取 ``HOST_CODEX`` / ``HOST_CLAUDE`` / ``None``（探测失败或未识别）。
    Claude → ``ClaudeCodeHostAdapter``；其余（Codex 及 host 未知兜底）→
    ``CodexHostAdapter``。
    """
    if host == HOST_CLAUDE:
        return ClaudeCodeHostAdapter()
    return CodexHostAdapter()


def _self_check() -> None:
    """runnable check：两 bundle 满足 Protocol、resolver 按 host 正确装配、各方法行为正确。

    这是本装配模块唯一非平凡行为的可运行检查——契约 gate 直接跑本 ``__main__``，
    覆盖 ``resolve_cline_kanban_agent_id`` 集成测试未触及的 ``host_display_name`` /
    ``session_deep_link`` / ``detect_host_skip_mode`` 三法。
    """
    from core.host_adapter import HOST_CODEX

    codex = resolve_host_adapter(HOST_CODEX)
    claude = resolve_host_adapter(HOST_CLAUDE)
    fallback = resolve_host_adapter(None)

    assert isinstance(codex, HostAdapter), "CodexHostAdapter 应满足 HostAdapter Protocol"
    assert isinstance(claude, HostAdapter), "ClaudeCodeHostAdapter 应满足 HostAdapter Protocol"
    assert isinstance(fallback, CodexHostAdapter), "host 未知应兜底 CodexHostAdapter"

    assert codex.host_display_name() == "Codex"
    assert claude.host_display_name() == "Claude Code"
    assert codex.session_deep_link("abc") == "codex://local/abc"
    assert codex.session_deep_link(None) is None
    assert claude.session_deep_link("abc") is None
    assert codex.cline_kanban_agent_id() == "codex"
    assert claude.cline_kanban_agent_id() == "claude"
    # detect_host_skip_mode：Claude 恒 None；Codex 委派 goal-mode 扫描（空 event 无 session
    # path → goal-mode 不命中 → None）。
    assert claude.detect_host_skip_mode({}) is None
    assert codex.detect_host_skip_mode({}) is None
    # parent_transcript_quoted_session_name：Claude 恒 None；Codex 对 None/不存在 path 返 None
    # （真实摘录行为由 parent_conversation_origin 端到端测试覆盖）。
    assert claude.parent_transcript_quoted_session_name(None) is None
    assert codex.parent_transcript_quoted_session_name(None) is None
    # stop_hook_agent_directive_channel：Claude 走非错误的 additionalContext，Codex 走 decision:block
    # （Codex stop.command.output 是 additionalProperties:false，多发一个键整条 Failed）。
    from core.stop_hook_output_audience import (
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
    )

    assert (
        claude.stop_hook_agent_directive_channel()
        == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT
    )
    assert (
        codex.stop_hook_agent_directive_channel()
        == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION
    )
    assert (
        fallback.stop_hook_agent_directive_channel()
        == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION
    ), "host 未知兜底 Codex ⇒ 必须用最保守的阻塞通道，不得赌 host 认识 hookSpecificOutput"
    print("host_adapter_resolver self-check OK")


if __name__ == "__main__":
    _self_check()
