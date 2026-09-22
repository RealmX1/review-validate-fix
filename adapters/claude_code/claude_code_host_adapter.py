"""``ClaudeCodeHostAdapter``——Claude Code host 的 ``HostAdapter`` 具体实现（S10 装配）。

Claude Code 侧的 host 行为全是 host 常量：

- ``host_display_name`` → ``Claude Code``（prompt-block 文案标题）。
- ``session_deep_link`` → 恒 ``None``：Claude Code 无 ``codex://`` 那类会话深链
  scheme，其「打开」入口由已输出的 ``RVF_PARENT_TRANSCRIPT_PATH`` 承担。
- ``cline_kanban_agent_id`` → ``claude``（应镜像的 cline-kanban agent_id 默认值）。
- ``detect_host_skip_mode`` → 恒 ``None``：Claude 无 Codex goal-mode 那类 host 特有的
  「本次 Stop 应跳过」概念（中性 suppress marker / env 仍由 core 的 ``should_suppress``
  判定，不走注入面）。
- ``stop_hook_agent_directive_channel`` → 非错误的
  ``hookSpecificOutput.additionalContext``：Claude Code 2.1.233 起 Stop 事件支持该
  hook-specific 出口（二进制内嵌 schema：「additionalContext is non-error feedback
  delivered to the model; the conversation continues so the model can act on it」，
  实测确认它照样驱动 agent 续跑，只是不落 ``hook_blocking_error`` / 不印
  ``Stop hook error:``）。

因全是常量，本 bundle 除该受众契约的常量外**不 import 任何 adapter / core 业务模块**。装配由
``host_adapter_resolver.resolve_host_adapter`` 按 host 探测在本 bundle 与
``CodexHostAdapter`` 间二选一完成；本 bundle 只认识 Claude Code、不知道 Codex。
"""

from __future__ import annotations

from typing import Any, Mapping

from core.stop_hook_output_audience import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
)


class ClaudeCodeHostAdapter:
    """Claude Code 的 ``HostAdapter`` 具体实现（满足 ``core.host_adapter.HostAdapter`` Protocol）。"""

    def host_display_name(self) -> str:
        return "Claude Code"

    def session_deep_link(self, session_id: str | None) -> str | None:
        return None

    def cline_kanban_agent_id(self) -> str:
        return "claude"

    def detect_host_skip_mode(
        self, event: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        return None

    def parent_transcript_quoted_session_name(self, path: Any) -> str | None:
        # Claude Code 无 Codex-schema 首条-user-message 抽取路径；会话名由 session_ref 兜底承担。
        return None

    def stop_hook_agent_directive_channel(self) -> str:
        return STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT
