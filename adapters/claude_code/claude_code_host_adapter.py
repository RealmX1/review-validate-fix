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
- ``host_synthesized_user_prompt_submit_turn_kind`` → 后台任务完成通知。后台 Bash 或后台子代理
  （Agent 工具）结束时，Claude Code 合成一条以 ``<task-notification>`` 开头的 user message 并**照常
  触发 UserPromptSubmit**。2026-09-30 取证：
  * 真机（2.1.285，``claude -p`` + 仅含一个落盘 UPS hook 的隔离 settings）：后台 ``sleep 4`` 完成后
    UPS 第二次触发，事件键只有 ``cwd / hook_event_name / permission_mode / prompt / prompt_id /
    session_id / transcript_path``，``prompt`` 逐字是 ``<task-notification>\n<task-id>…</task-notification>``
    ——**没有**任何 origin 字段，模型侧看到的「SYSTEM NOTIFICATION」前言也不在里面；
  * 本机 4 天 transcript（2.1.281–2.1.285）：119 条 ``origin.kind == "task-notification"`` 的 user
    message 里 118 条紧跟 UPS 附件，正文全部以 ``<task-notification>`` 开头。
  二进制里同一个 UPS 构造函数只拼 ``prompt``，故正文前缀是 hook 侧**唯一**可用的证据。

因全是常量，本 bundle 除该受众契约的常量外**不 import 任何 adapter / core 业务模块**。装配由
``host_adapter_resolver.resolve_host_adapter`` 按 host 探测在本 bundle 与
``CodexHostAdapter`` 间二选一完成；本 bundle 只认识 Claude Code、不知道 Codex。
"""

from __future__ import annotations

from typing import Any, Mapping

from core.stop_hook_output_audience import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
)


CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_TURN_KIND = (
    "claude_code_background_task_completion_notification"
)
"""合成回合种类：后台 Bash / 后台子代理完成时 Claude Code 自己合成的那条 ``<task-notification>``。"""

CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_OPENING_TAG = "<task-notification>"
CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_CLOSING_TAG = "</task-notification>"


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

    def host_synthesized_user_prompt_submit_turn_kind(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        # 正文**以**开标签起头、且带闭标签才算：开发者粘贴的内容会被 Claude Code（≥2.1.278）包进
        # ``<pasted_content …>``，不会以 ``<task-notification>`` 起头；只在句中提到这个标签更不算。
        if not isinstance(prompt_text, str):
            return None
        if not prompt_text.lstrip().startswith(
            CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_OPENING_TAG
        ):
            return None
        if CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_CLOSING_TAG not in prompt_text:
            return None
        return CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_TURN_KIND
