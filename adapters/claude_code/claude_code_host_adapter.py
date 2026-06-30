"""``ClaudeCodeHostAdapter``——Claude Code host 的 ``HostAdapter`` 具体实现（S10 装配）。

Claude Code 侧的 host 行为全是 host 常量：

- ``host_display_name`` → ``Claude Code``（prompt-block 文案标题）。
- ``session_deep_link`` → 恒 ``None``：Claude Code 无 ``codex://`` 那类会话深链
  scheme，其「打开」入口由已输出的 ``RVF_PARENT_TRANSCRIPT_PATH`` 承担。
- ``cline_kanban_agent_id`` → ``claude``（应镜像的 cline-kanban agent_id 默认值）。
- ``detect_host_skip_mode`` → 恒 ``None``：Claude 无 Codex goal-mode 那类 host 特有的
  「本次 Stop 应跳过」概念（中性 suppress marker / env 仍由 core 的 ``should_suppress``
  判定，不走注入面）。

因全是常量，本 bundle **stdlib-only**、不 import 任何 adapter / core 业务模块。装配由
``host_adapter_resolver.resolve_host_adapter`` 按 host 探测在本 bundle 与
``CodexHostAdapter`` 间二选一完成；本 bundle 只认识 Claude Code、不知道 Codex。
"""

from __future__ import annotations

from typing import Any, Mapping


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
