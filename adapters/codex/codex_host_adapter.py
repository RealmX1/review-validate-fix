"""``CodexHostAdapter``——Codex host 的 ``HostAdapter`` 具体实现（S10 装配）。

把 core 业务逻辑经 ``core.host_adapter.HostAdapter`` 注入面索取的 host 行为，落到
Codex 的具体语义：

- ``host_display_name`` → ``Codex``（prompt-block 文案标题）。
- ``session_deep_link`` → ``codex://local/{session_id}``（Codex 会话深链）。
- ``cline_kanban_agent_id`` → ``codex``（应镜像的 cline-kanban agent_id 默认值；显式
  pin 与最终兜底仍是中性逻辑、留在 ``resolve_cline_kanban_agent_id``）。
- ``detect_host_skip_mode`` → 委派 ``adapters.codex.transcript.codex_goal_mode_context_from_event``
  （Codex goal-mode 的「本次 Stop 应跳过」检测）。
- ``stop_hook_agent_directive_channel`` → ``decision:block``：Codex 二进制内嵌的
  ``stop.command.output`` schema 是 ``additionalProperties: false``，多发一个
  ``hookSpecificOutput`` 键会让整条 hook 输出被判 Failed，故 agent-directive 只能
  走阻塞通道（代价是 harness 把它渲染成错误，这是 Codex 侧的已知残留）。
- ``host_synthesized_user_prompt_submit_turn_kind`` → 恒 ``None``。唯一被怀疑的合成回合是 Stop
  ``decision:block`` 的续跑，2026-09-30 真机取证（codex-cli 0.158.0 ``codex exec``，隔离
  ``CODEX_HOME`` + 真实 hook 脚本 + 本地复算的信任指纹）：UPS 只在真实 prompt 时触发 1 次；Stop
  触发 2 次、共享同一个 ``turn_id``（第二次 ``stop_hook_active=true``）；续跑正文在 rollout 里是
  同一 turn 内的一条 ``<hook_prompt hook_run_id="stop:…">`` user message，**不经过 UPS**。
  ``/goal`` 续跑是 developer 消息、Stop 侧另有 goal-mode 跳过，是否触发 UPS 未取证——拿不到正向
  证据前按 Protocol 的约定返回 None（误判成开发者 prompt 只是退回改动前的行为）。

装配由 ``host_adapter_resolver.resolve_host_adapter`` 按 ``detect_transcript_format``
结果完成：本 bundle 或 ``ClaudeCodeHostAdapter`` 二选一注入 core。本 bundle **只认识
Codex**，绝不反向 import 共享审查引擎、绝不知道 Claude——host 判断只在 resolver 一处。
"""

from __future__ import annotations

from typing import Any, Mapping

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 adapters.* / core.* import

from adapters.codex.transcript import (  # noqa: E402
    codex_goal_mode_context_from_event,
)
from core.stop_hook_output_audience import (  # noqa: E402
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
)
from session_label import (  # noqa: E402 — Codex rollout 首条 user message 抽取 + preamble 剥离 + 摘录
    first_user_message,
    parent_conversation_fallback_chars,
    single_line_excerpt,
)


class CodexHostAdapter:
    """Codex 的 ``HostAdapter`` 具体实现（满足 ``core.host_adapter.HostAdapter`` Protocol）。"""

    def host_display_name(self) -> str:
        return "Codex"

    def session_deep_link(self, session_id: str | None) -> str | None:
        return f"codex://local/{session_id}" if session_id else None

    def cline_kanban_agent_id(self) -> str:
        return "codex"

    def detect_host_skip_mode(
        self, event: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        return codex_goal_mode_context_from_event(dict(event))

    def parent_transcript_quoted_session_name(self, path: Any) -> str | None:
        # Codex rollout schema：首条 user message（已剥 stitched preamble）→ 单行摘录 → 带引号。
        if path is None:
            return None
        message = first_user_message(path)
        if not message:
            return None
        excerpt = single_line_excerpt(message, parent_conversation_fallback_chars())
        if not excerpt:
            return None
        return f'"{excerpt}"'

    def stop_hook_agent_directive_channel(self) -> str:
        return STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION

    def host_synthesized_user_prompt_submit_turn_kind(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        return None
