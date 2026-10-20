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

- ``agent_turn_identifier_shared_by_hook_events_of_one_turn`` → 事件的 ``prompt_id``。2026-10-02
  真机取证（2.1.287，``claude -p`` stream-json + 仅含落盘 UPS/Stop hook 的隔离 settings）：
  * agent 跑 ``sleep 15`` 时从 stdin 再写一条 ⇒ 这条排队消息在工具结果之后**并入当前回合**（transcript
    ``queue-operation remove reason=absorbed_mid_turn`` + ``queued_command`` 附件），它的 UPS 在并入那一刻
    才触发，``prompt_id`` 与该回合首条 prompt 的 UPS、该回合的 Stop **相同**；
  * Stop hook ``decision:block`` 一次 ⇒ 续跑后的第二次 Stop（``stop_hook_active=true``）``prompt_id``
    **不变**；
  * 空闲时发出的下一条 prompt 换新 ``prompt_id``。

- ``user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste`` → 剥一层
  ``<pasted_content id="…">…</pasted_content id="…">``。Claude Code（≥2.1.278）把 bracketed paste 包成这一层，
  UPS 事件的 ``prompt`` 里就是包好的形态。2026-10-02 取证：本机 round-baseline 记录的 UPS ``prompt`` 摘录里，
  cline-kanban 投递的消息逐字以 ``\\n\\n<pasted_content id="…">\\n`` 起头、以 ``\\n</pasted_content id="…">\\n``
  收尾；transcript 里 158 处开标签全部带 ``id``、闭标签重复同一个 ``id``；单行边界标记的 A/B transcript
  是同一形态。包裹前面有开发者打的字（13 例）或一条里有多段粘贴（3 例）⇒ 不是整条单段粘贴，不剥。

因全是常量，本 bundle 除该受众契约的常量外**不 import 任何 adapter / core 业务模块**。装配由
``host_adapter_resolver.resolve_host_adapter`` 按 host 探测在本 bundle 与
``CodexHostAdapter`` 间二选一完成；本 bundle 只认识 Claude Code、不知道 Codex。
"""

from __future__ import annotations

import re
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

CLAUDE_CODE_HOOK_EVENT_AGENT_TURN_IDENTIFIER_FIELD = "prompt_id"
"""Claude Code hook 事件里标识「这一回合」的字段。取证见模块 docstring 的
``agent_turn_identifier_shared_by_hook_events_of_one_turn`` 一条。"""

CLAUDE_CODE_PASTED_CONTENT_ENVELOPE_OPENING_TAG_PREFIX = "<pasted_content"
CLAUDE_CODE_WHOLE_PROMPT_IS_ONE_PASTED_CONTENT_ENVELOPE_RE = re.compile(
    r'<pasted_content(?P<pasted_content_envelope_attributes>(?: id="[^"]*")?)>'
    r"(?P<pasted_text>.*)"
    r"</pasted_content(?P=pasted_content_envelope_attributes)>",
    re.DOTALL,
)
"""整条（已去首尾空白的）prompt 恰好是一段粘贴包裹：闭标签重复开标签的 ``id``。取证见模块 docstring 的
``user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste`` 一条。"""


class ClaudeCodeHostAdapter:
    """Claude Code 的 ``HostAdapter`` 具体实现（满足 ``core.host_adapter.HostAdapter`` Protocol）。"""

    def read_independent_user_turn_boundary_evidence(self, *, receipt):
        return {"independent_user_turn_verified": False, "capability_unverified": True, "reason": "此宿主的开审 receipt 与原生回退绑定尚未取证"}

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

    def agent_turn_identifier_shared_by_hook_events_of_one_turn(
        self, hook_event: Mapping[str, Any]
    ) -> str | None:
        value = hook_event.get(CLAUDE_CODE_HOOK_EVENT_AGENT_TURN_IDENTIFIER_FIELD)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        if not isinstance(prompt_text, str):
            return None
        match = CLAUDE_CODE_WHOLE_PROMPT_IS_ONE_PASTED_CONTENT_ENVELOPE_RE.fullmatch(prompt_text.strip())
        if match is None:
            return None
        pasted_text = match.group("pasted_text")
        # 一条里有多段粘贴时，贪婪匹配可能把「第一段开标签 … 最后一段闭标签」整个吞下（id 恰好相同时）。
        if CLAUDE_CODE_PASTED_CONTENT_ENVELOPE_OPENING_TAG_PREFIX in pasted_text:
            return None
        return pasted_text
