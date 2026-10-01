"""``HostAdapter``——core 业务逻辑唯一认识的 host 能力注入契约（S9 收尾 / S10 enabler）。

## 这个 Protocol 解决什么

去-codex 重构的终态要求 `core/` 完全 host-中性：相对 R-1 退出门，迁入 core 的
业务逻辑模块**不得**出现 ``HOST_CLAUDE`` / ``HOST_CODEX`` / ``host_kind`` 这些
host 身份词汇（守卫 `rg 'HOST_CLAUDE|HOST_CODEX|host_kind' core/ --glob '!core/host_adapter/**'`==0；
``core/host_adapter/**`` 本身是契约包，是 host 身份词汇的法定唯一归属，故 carve-out）。

当前引擎 ``stop_review_validate_fix.py`` 里仍有若干 **inline** ``host_kind ==``
分支，它们随 S10「中性体迁 core/」搬进 core 后就会触发该守卫。``HostAdapter`` 把
这些 host-身份相关行为收成一个**注入面**：core 持有一个已绑定到具体 host 的
``HostAdapter`` 实例，只调它的方法拿 host 行为，绝不命名 host 常量。

## v1 注入面（每个方法标注它在 S10 将取代的引擎分支 file:line）

仅收**确已坐实为 inline ``host_kind`` 分支**、且其宿主代码注定迁 core 的行为
（grounded spike，非臆测）：

- ``host_display_name()``——取代 ``parent_conversation_host_label(host_kind)``
  (engine:789，另 847/3118 调用点)：父会话 harness 的人类可读名。
- ``session_deep_link(session_id)``——取代 ``codex://local/{id}`` 三元
  (engine:774)：本 host 的会话深链；无该 scheme 的 host 返回 None。
- ``cline_kanban_agent_id()``——取代 ``default_cline_kanban_agent_id`` 的 host-镜像
  分支 (engine:1740-1743)：本 host 应镜像的 cline-kanban agent_id 默认值
  （显式 pin 与最终兜底仍是中性逻辑、留 core）。
- ``detect_host_skip_mode(event)``——取代 ``codex_goal_mode_context_from_event``
  (engine:6586)：本 host 特有的「这次 Stop 应跳过」检测（Codex=goal-mode；
  Claude 无此概念、返回 None）。中性 suppress marker/env 仍由 ``should_suppress``
  在 core 内判定，不进注入面。
- ``stop_hook_agent_directive_channel()``——Stop hook 上「必须驱动 agent 立刻行动」
  这一类出口在本 host 的线材：Claude / Oh My Pi 用非错误的
  ``hookSpecificOutput.additionalContext``，Codex / Cursor / Kimi / DeepSeek 用
  ``decision:block``。core 只声明受众（见
  ``core.stop_hook_output_audience``），线材是 host 行为、归注入面。
- ``host_synthesized_user_prompt_submit_turn_kind(event, prompt_text)``——这条
  UserPromptSubmit 是不是 **host 自己合成**的回合（不是开发者打的字）。UPS 的「开发者真实
  prompt」判定同时驱动再入预算复位、snooze 基线重置、round baseline 前移与 park 资格；
  合成回合的形态逐 host 不同（Claude Code 的后台任务完成通知是一条 ``<task-notification>``
  user message，Codex 的 Stop 续跑根本不触发 UPS），故识别归注入面，core 不写任何 host 字面量。

## 刻意**不**进 v1 的（避免 dead scaffolding / 提前 S10 工作）

- transcript 读取 / session-path 定位 / session_label：这些**早已**经
  ``detect_transcript_format`` → per-host adapter 模块的 facade 派发（不是 inline
  ``host_kind`` 分支），那条 facade 本身即注入机制，复制进 Protocol 是冗余。
- ``launch_review_fork`` / ``provider_login_requirements``：fork 执行与 provider
  登录的 host 缝随 S10/S11 折叠 ``launch_backend`` 时才有真实消费者；Protocol 加方法
  是**增量**变更，到那一步再补，避免现在包裹尚未迁出引擎的内部实现。

## resolver 与 concrete bundle 的归属

resolver（``detect_transcript_format`` 结果 → 装配具体 host 的 ``HostAdapter``）与
Codex / Claude 两个 concrete 实现**住 S10**：它们需要 S10 抽出的 per-host 可注入
callable + ``run_stop_pipeline(event, host_adapter, ledger)`` 这个真实注入点。本切片
只落**契约**（seam），让 S10 的 relocation diff 聚焦于「搬模块 + 把 ``host_kind``
分支换成 ``host_adapter.方法()``」，且契约能被独立评审。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from core.stop_hook_output_audience import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
)


@runtime_checkable
class HostAdapter(Protocol):
    """core 唯一认识的 host 表面。具体实现（Codex / Claude）在 S10 装配并注入。

    ``@runtime_checkable`` 让 S10 的 resolver 可在装配处 ``isinstance`` 自检
    bundle 是否实现了全部必需方法（注意：runtime_checkable 只校验方法名存在、
    不校验签名）。
    """

    def host_display_name(self) -> str:
        """本 host 的人类可读名，用于 prompt-block 文案标题（如 ``Claude Code`` / ``Codex``）。"""
        ...

    def session_deep_link(self, session_id: str | None) -> str | None:
        """本 host 的会话深链；无该 scheme 的 host（或 ``session_id`` 缺失）返回 None。

        Codex：``codex://local/{session_id}``；Claude Code：恒 None（其「打开」入口
        由 ``RVF_PARENT_TRANSCRIPT_PATH`` 承担）。
        """
        ...

    def cline_kanban_agent_id(self) -> str:
        """本 host 应镜像的 cline-kanban task agent_id 默认值（如 ``codex`` / ``claude``）。"""
        ...

    def detect_host_skip_mode(self, event: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """本 host 特有的「这次 Stop 应跳过 RVF」检测；无命中（或 host 无此概念）返回 None。

        命中时返回的 mapping 作为 skip-decision 的 host 上下文（如 Codex goal-mode
        的 reason/续跑 marker）。中性 suppress（marker / env）不走这里。
        """
        ...

    def parent_transcript_quoted_session_name(self, path: Any) -> str | None:
        """从父 transcript 首条 user message 取一段带引号的会话名摘录；无则返回 None。

        S10e 引入（path-A「读父 transcript 首条 user message」能力）：core 的
        ``freeze_parent_conversation_origin`` 在 app-server 名 / session-index 名均落空时，
        用它作 ``first_user_prompt_fallback`` 级会话标签。首条 user message 的抽取与
        preamble 剥离是 host-schema 相关（Codex 需 ``strip_codex_user_message_preamble``
        去掉 stitched 上下文），故整段是 host 行为、归注入面：Codex 委派
        ``session_label``（first_user_message + single_line_excerpt），返回 ``"摘录"``；
        Claude Code 无该 Codex-schema 抽取路径、恒返回 None（其会话名由 session_ref
        兜底承担）。``path`` 是父 transcript 路径（``pathlib.Path``；用 ``Any`` 避免 core
        对 host 侧路径类型的形态假设）。
        """
        ...

    def stop_hook_agent_directive_channel(self) -> str:
        """本 host 上「必须驱动 agent 立刻行动」这类 Stop hook 出口该走哪条线材。

        取值来自 ``core.stop_hook_output_audience``：

        - ``STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT``——
          ``hookSpecificOutput.additionalContext``，驱动 agent 但不带错误框架
          （Claude Code / Oh My Pi）。
        - ``STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION``——
          ``decision:block`` + ``reason``（Codex / Cursor / Kimi / DeepSeek；
          Codex 的 ``stop.command.output`` 是 ``additionalProperties:false``，
          发 ``hookSpecificOutput`` 会让整条 hook 输出 Failed）。

        ``developer_only`` 受众在两个 host 上都是 ``{"continue": true,
        "systemMessage": …}``，不需要注入面参与，故本方法只覆盖 agent-directive。
        """
        ...

    def host_synthesized_user_prompt_submit_turn_kind(
        self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
    ) -> str | None:
        """这条 UserPromptSubmit 若是**本 host 自己合成**的回合（开发者没有打字），返回合成回合的种类名；
        开发者真实输入（或本 host 无法判定）返回 None。

        返回非 None 时，UPS 把这一回合当作**上一回合的延续**：不复位再入预算、不重置 snooze 基线、
        不前移 round baseline、不改写 park 资格，也不跑任何 RVF 字面量检测器（合成正文会原样带回
        子代理 / 后台命令读写过的文本，常含 RVF 自己的 token 与触发字面量）。

        判定必须基于**正向证据**：误判成「合成」会吞掉一条真实的开发者 prompt（手动触发失效、
        round baseline 不前移），误判成「开发者」只是退回改动前的行为。拿不准就返回 None。
        """
        ...


def _self_check() -> None:
    """runtime_checkable 一致性自检：齐方法的 stub 通过 isinstance、缺方法的不通过。

    这是本（声明式）模块唯一非平凡行为的 runnable check——若 Protocol 必需方法集
    意外漂移（增删方法名），下面的断言立即失败。
    """

    class _Conforming:
        def host_display_name(self) -> str:
            return "Codex"

        def session_deep_link(self, session_id: str | None) -> str | None:
            return f"codex://local/{session_id}" if session_id else None

        def cline_kanban_agent_id(self) -> str:
            return "codex"

        def detect_host_skip_mode(self, event: Mapping[str, Any]) -> Mapping[str, Any] | None:
            return None

        def parent_transcript_quoted_session_name(self, path: Any) -> str | None:
            return None

        def stop_hook_agent_directive_channel(self) -> str:
            return STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION

        def host_synthesized_user_prompt_submit_turn_kind(
            self, user_prompt_submit_event: Mapping[str, Any], prompt_text: str
        ) -> str | None:
            return None

    class _MissingSkipMode:
        def host_display_name(self) -> str:
            return "Claude Code"

        def session_deep_link(self, session_id: str | None) -> str | None:
            return None

        def cline_kanban_agent_id(self) -> str:
            return "claude"

    conforming = _Conforming()
    partial = _MissingSkipMode()
    assert isinstance(conforming, HostAdapter), "齐方法 stub 应满足 HostAdapter Protocol"
    assert not isinstance(partial, HostAdapter), "缺 detect_host_skip_mode 的 stub 不应满足"
    # 顺带验证注入面的方法名集合就是这 7 个，防止悄悄增删。
    expected = {
        "host_display_name",
        "session_deep_link",
        "cline_kanban_agent_id",
        "detect_host_skip_mode",
        "parent_transcript_quoted_session_name",
        "stop_hook_agent_directive_channel",
        "host_synthesized_user_prompt_submit_turn_kind",
    }
    actual = {name for name in dir(HostAdapter) if not name.startswith("_")}
    assert actual == expected, f"HostAdapter 注入面漂移：{actual} != {expected}"
    print("host_adapter_protocol self-check OK")


if __name__ == "__main__":
    _self_check()
