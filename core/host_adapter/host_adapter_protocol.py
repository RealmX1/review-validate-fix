"""``HostAdapter``——core 业务逻辑唯一认识的 host 能力注入契约（S9 收尾 / S10 enabler）。

## 这个 Protocol 解决什么

去-codex 重构的终态要求 `core/` 完全 host-中性：相对 R-1 退出门，迁入 core 的
业务逻辑模块**不得**出现 ``HOST_CLAUDE`` / ``HOST_CODEX`` / ``host_kind`` 这些
host 身份词汇（守卫 `rg 'HOST_CLAUDE|HOST_CODEX|host_kind' core/ --glob '!core/host_adapter/**'`==0；
``core/host_adapter/**`` 本身是契约包，是 host 身份词汇的法定唯一归属，故 carve-out）。

当前引擎 ``codex_stop_review_validate_fix.py`` 里仍有若干 **inline** ``host_kind ==``
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
    # 顺带验证 v1 注入面的方法名集合就是这 4 个，防止悄悄增删。
    expected = {
        "host_display_name",
        "session_deep_link",
        "cline_kanban_agent_id",
        "detect_host_skip_mode",
    }
    actual = {name for name in dir(HostAdapter) if not name.startswith("_")}
    assert actual == expected, f"HostAdapter 注入面漂移：{actual} != {expected}"
    print("host_adapter_protocol self-check OK")


if __name__ == "__main__":
    _self_check()
