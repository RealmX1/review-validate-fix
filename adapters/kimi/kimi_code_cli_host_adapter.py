"""``KimiCodeCliHostAdapter``——Kimi Code CLI host 的 ``HostAdapter`` 具体实现。

各方法的 Kimi 侧语义：

- ``host_display_name`` → ``Kimi Code CLI``。
- ``session_deep_link`` → 恒 ``None``。Kimi 能用 ``kimi -S <id>`` 恢复会话，但那是 CLI 参数
  而**不是** URI scheme；本方法的消费者是可点击的深链，给它一段命令串是错的。
- ``cline_kanban_agent_id`` → ``kimi``。**实测**：`kanban` 0.1.68 的 ``task create --preview``
  接受 ``--agent-id kimi``（阴性对照：非法 id 返回 ``rc=1, ok=false``，证明该 flag 确被校验）。
  ⚠️ 它的 ``--help`` 文案**没有**列出 kimi——文案已陈旧，不是真相源。
- ``detect_host_skip_mode`` → 恒 ``None``：Kimi 没有 Codex goal-mode 那类 host 特有的
  「本次 Stop 应跳过」概念。
- ``parent_transcript_quoted_session_name`` → 从 per-agent ``wire.jsonl`` 抽首条用户输入。
- ``stop_hook_agent_directive_channel`` → ``hookSpecificOutput.permissionDecision:"deny"``。
- ``host_synthesized_user_prompt_submit_turn_kind`` → 恒 ``None``：尚无 Kimi 会合成 user 回合并触发
  UserPromptSubmit 的证据（未取证，不是已证否）。
- ``agent_turn_identifier_shared_by_hook_events_of_one_turn`` → 恒 ``None``：未取证 Kimi hook 事件里
  有没有「同回合共享、新回合换新」的回合标识字段。
- ``user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste`` → 恒 ``None``：未取证 Kimi
  是否把终端粘贴包一层再交给 UPS；按 Protocol 约定拿不准就不剥。

## 为什么这条通道不能选错（Kimi 与 Codex 的失败形态是相反的）

Kimi 的 hook 输出 schema 逐字是 ``looseObject({ message, hookSpecificOutput })``，block 判据
逐字是 ``if (hookSpecificOutput?.permissionDecision !== "deny") return result;``。于是：

- 发 ``decision:block``（Codex 的线材）→ 顶层 ``decision`` 通过 ``looseObject`` 校验但**从不被
  读取** ⇒ **allow**，且**零报错**。RVF 在 Kimi 上完全失声，看起来只是「没反应」。
- 发 ``additionalContext``（Claude 的线材）→ 同样落在未命名区 ⇒ 同样静默 allow。

对比 Codex：给它发 ``hookSpecificOutput`` 会让整条 hook 输出被判 Failed（``additionalProperties:
false``）——**响亮**的失败。所以「host 未知兜底 Codex」这条既有纪律仍然正确，但它在 Kimi 方向
上不提供任何保护：**Kimi 必须被正向识别出来**，否则就是静默失效。

完整取证见 ``docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 adapters.* / core.* import

from adapters.kimi.session_layout import (  # noqa: E402
    SESSION_STATE_FILE_NAME,
)
from adapters.kimi.transcript import first_user_prompt_text  # noqa: E402
from core.stop_hook_output_audience import (  # noqa: E402
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY,
)
from session_label import (  # noqa: E402
    parent_conversation_fallback_chars,
    single_line_excerpt,
)

CLINE_KANBAN_AGENT_IDENTIFIER = "kimi"
HOST_DISPLAY_NAME = "Kimi Code CLI"


def _session_title_from_state_beside(wire_transcript_path: Path) -> str | None:
    """兜底：从 ``wire.jsonl`` 反推同会话的 ``state.json`` 并取 ``title``。

    布局是 ``<session_dir>/agents/<agent_id>/wire.jsonl``，故 ``parents[2]`` 即 session 目录。

    **这一级只在 wire 读不出、而 state 读得出时才有意义**（文件损坏 / 截断）。会话根本没收过
    用户输入时它同样为空——本机 42 份 wire 里有 6 份正是这种「已初始化、零输入」的会话，
    它们的正确答案就是 ``None``，不该硬凑一个名字出来。
    """
    import json

    parents = wire_transcript_path.parents
    if len(parents) < 3:
        return None
    try:
        raw = (parents[2] / SESSION_STATE_FILE_NAME).read_text(
            encoding="utf-8", errors="replace"
        )
        state = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    # 没有 lastPrompt 就是零输入会话；此时 title 只是占位，不构成「会话名」。
    if not (state.get("lastPrompt") or "").strip():
        return None
    title = state.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


class KimiCodeCliHostAdapter:
    """Kimi Code CLI 的 ``HostAdapter`` 具体实现（满足 ``core.host_adapter.HostAdapter``）。"""

    def host_display_name(self) -> str:
        return HOST_DISPLAY_NAME

    def session_deep_link(self, session_id: str | None) -> str | None:
        return None

    def cline_kanban_agent_id(self) -> str:
        return CLINE_KANBAN_AGENT_IDENTIFIER

    def detect_host_skip_mode(
        self, event: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        return None

    def parent_transcript_quoted_session_name(self, path: Any) -> str | None:
        if path is None:
            return None
        transcript_path = Path(path)
        message = first_user_prompt_text(transcript_path)
        if not message:
            message = _session_title_from_state_beside(transcript_path)
        if not message:
            return None
        excerpt = single_line_excerpt(message, parent_conversation_fallback_chars())
        if not excerpt:
            return None
        return f'"{excerpt}"'

    def stop_hook_agent_directive_channel(self) -> str:
        return STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY

    def host_synthesized_user_prompt_submit_turn_kind(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        # 没有 Kimi 会合成 user 回合并触发 UserPromptSubmit 的证据；按 Protocol 约定无正向证据即 None。
        return None

    def agent_turn_identifier_shared_by_hook_events_of_one_turn(
        self, hook_event: Mapping[str, Any]
    ) -> str | None:
        # 未取证 Kimi hook 事件里有没有满足「同回合共享、新回合换新」的字段；无正向证据即 None。
        return None

    def user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        # 未取证 Kimi 是否包裹终端粘贴；无正向证据即不剥。
        return None
