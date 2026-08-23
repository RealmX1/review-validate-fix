#!/usr/bin/env python3
"""``resolve_host_adapter``——把 ``detect_transcript_format`` 的 host 探测结果装配成
具体 ``HostAdapter`` bundle 注入 core（S10 装配点 / HostAdapter 注入面的 resolver）。

复用现成 facade 范式（如 ``subagent_capture`` / ``rvf_parent_context``）：本模块是
RVF 中**唯一**同时认识 Codex / Claude / Kimi 三个 concrete bundle 的地方（assembly/entry
层）。core 业务逻辑模块只拿被注入的 ``HostAdapter`` 实例、绝不在 core 内做 host 判断
或装配，从而守住 ``core/`` 的 host-中性不变量（R-1 退出门）。

装配口径（与 ``default_cline_kanban_agent_id`` / ``parent_conversation_origin`` 的既有
host 兜底一致）：

- ``HOST_CLAUDE`` → ``ClaudeCodeHostAdapter``。
- ``HOST_KIMI_CODE_CLI`` → ``KimiCodeCliHostAdapter``。
- ``HOST_CODEX`` / host 未知（``None``，探测失败或未识别格式）→ ``CodexHostAdapter``，
  沿用历史「无法识别 → 退回 Codex」口径，保证既有 Codex-only 用例零回归。

⚠️ **兜底对 Kimi 不提供保护。** 「未知 → Codex」这条口径的正当性来自 Codex 的失败是**响亮**的
（``stop.command.output`` 是 ``additionalProperties:false``，多一个键整条 hook 输出 Failed）。
但 Kimi 的 hook 输出 schema 是 ``looseObject`` 且只读 ``hookSpecificOutput.permissionDecision``
⇒ 一个被误判成 Codex 的 Kimi 会话会收到 ``decision:block``，**静默 allow、零报错**，RVF 完全
失声。所以 Kimi **必须被正向识别**；兜底只是防止更坏的情况，不是安全网。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* / adapters.* import

from core.host_adapter import (  # noqa: E402
    HOST_CLAUDE,
    HOST_KIMI_CODE_CLI,
    KNOWN_HOST_IDENTITIES,
    HostAdapter,
)
from adapters.codex.codex_host_adapter import CodexHostAdapter  # noqa: E402
from adapters.claude_code.claude_code_host_adapter import (  # noqa: E402
    ClaudeCodeHostAdapter,
)
from adapters.kimi.kimi_code_cli_host_adapter import (  # noqa: E402
    KimiCodeCliHostAdapter,
)


def resolve_host_adapter(host: str | None) -> HostAdapter:
    """按 ``detect_transcript_format`` 的 host 字符串装配并返回具体 ``HostAdapter``。

    ``host`` 取 ``HOST_CODEX`` / ``HOST_CLAUDE`` / ``HOST_KIMI_CODE_CLI`` / ``None``
    （探测失败或未识别）。Claude → ``ClaudeCodeHostAdapter``；Kimi →
    ``KimiCodeCliHostAdapter``；其余（Codex 及 host 未知兜底）→ ``CodexHostAdapter``。
    """
    if host == HOST_CLAUDE:
        return ClaudeCodeHostAdapter()
    if host == HOST_KIMI_CODE_CLI:
        return KimiCodeCliHostAdapter()
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
        STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY,
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

    kimi = resolve_host_adapter(HOST_KIMI_CODE_CLI)
    assert isinstance(kimi, HostAdapter), "KimiCodeCliHostAdapter 应满足 HostAdapter Protocol"
    assert isinstance(kimi, KimiCodeCliHostAdapter), (
        "Kimi 未被正向装配就会落进 Codex 兜底 ⇒ 收到 decision:block ⇒ Kimi 静默 allow ⇒ 完全失声"
    )
    assert kimi.host_display_name() == "Kimi Code CLI"
    assert kimi.session_deep_link("session_abc") is None, (
        "Kimi 的 `kimi -S <id>` 是 CLI 参数不是 URI scheme；深链消费者要的是可点击链接"
    )
    assert kimi.cline_kanban_agent_id() == "kimi", (
        "实测 kanban 0.1.68 的 task create --preview 接受 --agent-id kimi"
        "（阴性对照：非法 id 返回 rc=1/ok=false）。其 --help 文案未列出 kimi，属文案陈旧。"
    )
    assert kimi.detect_host_skip_mode({}) is None
    assert kimi.parent_transcript_quoted_session_name(None) is None
    assert (
        kimi.stop_hook_agent_directive_channel()
        == STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY
    )

    # ── 完备性：每个可被正向识别的 host 都必须有自己的装配分支 ──────────────
    # 少一条分支不会报错，只会让该 host 静默落进 Codex 兜底。用「装配出的类互不相同」来抓它。
    assembled = {host: resolve_host_adapter(host) for host in sorted(KNOWN_HOST_IDENTITIES)}
    assert len(KNOWN_HOST_IDENTITIES) == 3, KNOWN_HOST_IDENTITIES
    for host, adapter in assembled.items():
        assert isinstance(adapter, HostAdapter), f"{host} 装配出的实例不满足 Protocol"
    assembled_types = {host: type(adapter).__name__ for host, adapter in assembled.items()}
    assert len(set(assembled_types.values())) == len(assembled_types), (
        f"两个 host 装配出了同一个 adapter 类：{assembled_types}——"
        "多半是新 host 忘了加装配分支而落进了兜底。"
    )
    channels = {host: adapter.stop_hook_agent_directive_channel() for host, adapter in assembled.items()}
    assert len(set(channels.values())) == len(channels), (
        f"两个 host 用了同一条出口线材：{channels}。三家的线材互不兼容，共用即意味着"
        "其中一家在线上是静默失效的。"
    )
    print("host_adapter_resolver self-check OK")


if __name__ == "__main__":
    _self_check()
