#!/usr/bin/env python3
"""``resolve_host_adapter``——把 ``detect_transcript_format`` 的 host 探测结果装配成
具体 ``HostAdapter`` bundle 注入 core（S10 装配点 / HostAdapter 注入面的 resolver）。

复用现成 facade 范式（如 ``subagent_capture`` / ``rvf_parent_context``）：本模块是
RVF 中**唯一**同时认识 Codex / Claude / Kimi 三个 concrete bundle 的地方（assembly/entry
层）。「当前这次 hook 事件属于哪个 host 的会话」的判定（``resolve_current_session_host_adapter``）
也住这里：Stop 与 UserPromptSubmit 两个 hook 都要问这个问题，而 UPS 不能 import 巨型 Stop 引擎。core 业务逻辑模块只拿被注入的 ``HostAdapter`` 实例、绝不在 core 内做 host 判断
或装配，从而守住 ``core/`` 的 host-中性不变量（R-1 退出门）。

装配口径（与 ``default_cline_kanban_agent_id`` / ``parent_conversation_origin`` 的既有
host 兜底一致）：

- ``HOST_CLAUDE`` → ``ClaudeCodeHostAdapter``。
- ``HOST_KIMI_CODE`` → ``KimiCodeCliHostAdapter``。
- ``HOST_CODEX`` / host 未知（``None``，探测失败或未识别格式）→ ``CodexHostAdapter``，
  沿用历史「无法识别 → 退回 Codex」口径，保证既有 Codex-only 用例零回归。

⚠️ **兜底对 Kimi 不提供保护。** 「未知 → Codex」这条口径的正当性来自 Codex 的失败是**响亮**的
（``stop.command.output`` 是 ``additionalProperties:false``，多一个键整条 hook 输出 Failed）。
但 Kimi 的 hook 输出 schema 是 ``looseObject`` 且只读 ``hookSpecificOutput.permissionDecision``
⇒ 一个被误判成 Codex 的 Kimi 会话会收到 ``decision:block``，**静默 allow、零报错**，RVF 完全
失声。所以 Kimi **必须被正向识别**；兜底只是防止更坏的情况，不是安全网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* / adapters.* import

from core.host_adapter import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_KIMI_CODE,
    KNOWN_HOST_IDENTITIES,
    HostAdapter,
    detect_transcript_format,
)
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import (  # noqa: E402
    SESSION_PATH_KEYS,
    SESSION_SCOPE_PATH_KEYS,
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

    ``host`` 取 ``HOST_CODEX`` / ``HOST_CLAUDE`` / ``HOST_KIMI_CODE`` / ``None``
    （探测失败或未识别）。Claude → ``ClaudeCodeHostAdapter``；Kimi →
    ``KimiCodeCliHostAdapter``；其余（Codex 及 host 未知兜底）→ ``CodexHostAdapter``。
    """
    if host == HOST_CLAUDE:
        return ClaudeCodeHostAdapter()
    if host == HOST_KIMI_CODE:
        return KimiCodeCliHostAdapter()
    return CodexHostAdapter()


# ── 当前会话的 host 判定（Stop 与 UPS 两个 hook 共用的唯一实现）───────────────────────────
#
# 原本住在 ``stop_review_validate_fix``；UPS 也需要它（识别 host 合成的回合，见
# ``HostAdapter.host_synthesized_user_prompt_submit_turn_kind``），而 UPS 刻意不 import 那个巨型
# 引擎模块（热路径）。两份各自实现的判定顺序迟早漂移，故整段搬到装配层，Stop 从这里 re-import。


def _event_session_paths_for_host_resolution(event: dict[str, Any]) -> list[Path]:
    """与引擎 ``event_session_paths`` 同口径：``SESSION_PATH_KEYS`` 全集（含 ``log_path``）。"""
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def _first_readable_session_scope_path_for_host_resolution(event: dict[str, Any]) -> Path | None:
    """与引擎 ``first_readable_session_path`` 同口径：``SESSION_SCOPE_PATH_KEYS`` 里第一份可读文件。"""
    for key in SESSION_SCOPE_PATH_KEYS:
        value = event.get(key)
        if not (isinstance(value, str) and value):
            continue
        try:
            resolved = Path(value).expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            with resolved.open("rb"):
                pass
        except OSError:
            continue
        return resolved
    return None


CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL = "/.codex/sessions/"
"""Codex rollout **默认**根 ``~/.codex/sessions/`` 的路径字面量。

单独命名是为了钉住它**不是**判定 Codex 的充分条件：``CODEX_HOME`` 一改，真实 rollout
路径里根本不含这个字面量（见 ``codex_rollout_session_path_markers``）。
"""


def codex_rollout_session_path_markers() -> tuple[str, ...]:
    """能判定「这是一条 Codex rollout 文件路径」的路径证据集合（正向证据，保守）。

    除默认根 ``~/.codex/sessions/`` 外，还认 ``$CODEX_HOME/sessions/``——Codex CLI 自己
    就用 ``CODEX_HOME`` override 安装根（本仓 ``adapters.codex.subagent.codex_sessions_root``
    正是这么解析的），自定义过它的用户，其 rollout 路径里根本不含默认字面量。

    ``CODEX_HOME`` 在这里**只**用来回答「sessions 目录在哪」，绝不当成「当前会话是 Codex」
    的身份信号——后者是 ``core/host_adapter/parent_harness_identity`` 明令禁止的误用（它是
    常驻安装路径 override，本机装过 Codex 的用户在任何 host 下都设着它）。
    """
    markers = [CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL]
    codex_home = os.environ.get("CODEX_HOME")
    if isinstance(codex_home, str) and codex_home.strip():
        try:
            root = os.path.normpath(str(Path(codex_home).expanduser() / "sessions"))
        except (OSError, ValueError, RuntimeError):  # 解析失败按「无此证据」处理
            root = ""
        if root:
            markers.append(root.rstrip(os.sep) + os.sep)
    return tuple(markers)


def event_session_path_is_codex_rollout(event: dict[str, Any]) -> bool:
    """event 的任一会话路径键落在 Codex rollout 根下 ⇒ 确诊 Codex（正向证据）。

    覆盖**全部** ``SESSION_PATH_KEYS``（含 ``log_path``），而不是其中一个子集：漏掉的键
    正是「真 Codex 会话溜过守卫」的入口。

    与 ``_claude_hook_entry.is_foreign_invocation`` **同判据**；那个模块刻意 stdlib-only、
    不 import 本仓 ``core``（hook 是最该 fail-open 的安全面），故那边自带一份等价副本——
    两处改动必须同步。
    """
    markers = codex_rollout_session_path_markers()
    for path in _event_session_paths_for_host_resolution(event):
        raw = str(path)
        try:
            normalized = os.path.normpath(str(Path(raw).expanduser()))
        except (OSError, ValueError, RuntimeError):  # 规范化失败退回原串
            normalized = raw
        for marker in markers:
            if marker == CODEX_ROLLOUT_DEFAULT_SESSIONS_PATH_LITERAL:
                if marker in normalized or marker in raw:
                    return True
            elif normalized.startswith(marker):
                return True
    return False


HOST_BY_PLUGIN_ENTRY_SOURCE_PROVIDER_STAMP: dict[str, str] = {
    "claude-code": HOST_CLAUDE,
    "kimi-code": HOST_KIMI_CODE,
}
"""``event["source"]["provider"]`` → host 身份。键是各 host 的 plugin 入口 ``setdefault``
上去的那个字面量（``hooks/_claude_hook_entry.py`` / ``hooks/_kimi_hook_entry.py``）。

**每多一个自带 hook 入口的 host，这里就必须多一条**：漏一条时该 host 的 stamp 会被无视，
在 transcript 探测落空的场合直接掉进「未知 → Codex」兜底。Codex 兜底对 Codex 自己是安全的
（发错键会**响亮**地整条 Failed），对 Kimi 则是**静默 allow**，RVF 完全失声、零报错。

Codex 不在表里，也不需要在：它没有自己的 stamp，而它的两条正向证据（会话路径、rollout
schema）排在本表之前。
"""


def resolve_current_session_host_adapter(event: dict[str, Any]) -> HostAdapter:
    """装配**当前这次 hook 事件（Stop / UserPromptSubmit）所属会话**的 HostAdapter（≠ 父会话 host）。

    出口线材必须按「谁在读这次 hook 输出」决定，而既有的
    ``resolve_host_adapter(parent_origin["host_kind"])`` 问的是父会话——两者在
    Kanban task / fork 场景下会不同，用错会把 ``hookSpecificOutput`` 发给 Codex
    （其 ``stop.command.output`` 是 ``additionalProperties:false``，整条 Failed）。

    **判据顺序刻意「先否定 Claude、后采信 stamp」**，因为两个方向的误判代价不对称：

    - Claude 被误判成 Codex：只是少了个非错误通道（降级到 ``decision: block``），仍能工作。
    - Codex 被误判成 Claude：出口里会出现 ``hookSpecificOutput.additionalContext``，而
      Codex 的 ``stop.command.output`` 是 ``additionalProperties: false`` ⇒ **整条 hook
      输出被判 Failed**，RVF 在 Codex 上静默停摆（``codex exec`` 下零警告）。

    而 ``source.provider`` stamp 并不可信到可以压过路径/内容证据：它是
    ``hooks/_claude_hook_entry.py`` 在**它自己那道 foreign check 之后** ``setdefault``
    上去的。那道 check 一旦漏判（自定义 ``CODEX_HOME``、或事件只带它没查的路径键），一个
    真实的 Codex 会话就会带着 ``claude-code`` stamp 走到这里。所以顺序是：

    1. 任一会话路径落在 Codex rollout 根下（默认 ``~/.codex/sessions/`` 或
       ``$CODEX_HOME/sessions/``）→ Codex。**否定 Claude 的证据排在 stamp 之前。**
    2. ``detect_transcript_format`` 嗅出当前会话 transcript 是 Codex rollout schema →
       Codex。这条不依赖任何 env / 路径约定，是路径约定被改掉时的真相源。
    3. ``detect_transcript_format`` 嗅出当前会话是 **Kimi** wire schema → Kimi；嗅出
       Claude transcript schema → Claude。理由与第 2 条一致：schema 是文件内容的事实，
       stamp 只是入口自己 ``setdefault`` 的，所以**整层** schema 证据排在**整层** stamp 之前。
       Kimi 方向的代价更高：它若被误判成 Claude 会收到 ``hookSpecificOutput.additionalContext``，
       而 Kimi 的 hook 输出 schema 是 ``looseObject`` 且只读 ``permissionDecision`` ⇒
       **静默 allow、零报错**，RVF 完全失声。实测（2026-08-23）：不加 Kimi schema 这条时，
       一个带 ``claude-code`` stamp 的 Kimi 会话确实被判成 Claude —— 这不是假想，Kimi 的 shim
       要做大量事件归一，复用或误配 Claude 入口逻辑就会打上那个 stamp。
    4. ``event["source"]["provider"]`` stamp——各 host 的 plugin 入口亲手打的标
       （``claude-code`` → Claude，``kimi-code`` → Kimi）。在没有任何 schema / 路径反证时
       它是最强的正向 host 证据，也是 **transcript 不可读 / 尚未落盘时唯一的线索**。
       Kimi 尤其依赖这一层：它的 Stop 事件原本**没有** ``transcript_path``，那个键由
       ``hooks/_kimi_hook_entry.py`` 合成（会话目录 → ``agents/main/wire.jsonl``），而合成
       有两条正常的落空路径——会话目录还没进 ``session_index.jsonl`` 且扫桶没命中，以及
       ``wire.jsonl`` 尚未落盘（``resolve_agent_wire_transcript_path`` 要求 ``is_file()``）。
       落空即探测为 ``None`` ⇒ 兜底 Codex ⇒ 发 ``decision:block`` ⇒ Kimi 静默 allow。
       这是 HOST-2（「Claude 在 transcript 不可读时仍采信 stamp」）的 Kimi 对偶。
    5. 探测结果本身（``HOST_CLAUDE`` → Claude；``None`` → resolver 兜底 Codex + 阻塞通道），
       沿用「无法识别退回 Codex」口径：兜底到最保守的通道，绝不赌未知 host 认识
       ``hookSpecificOutput``。

    **stamp 之间不排序，因为 ``provider`` 只有一个值**；而 stamp 整体仍排在第 1–3 条
    schema / 路径证据之后，所以即使某个入口的归属判据漏判、把 stamp 打到了别家的会话上，
    正向的 Codex 证据依旧最先命中——Codex 方向的既有不对称性一个字符都没被放松。

    ⚠️ **兜底的不对称性只覆盖 Codex 方向。** 「未知 → Codex + 阻塞通道」之所以安全，是因为
    Codex 收到不认识的键会**响亮**地整条 Failed。Kimi 方向没有这层保护：任何把 Kimi 判成
    别家的路径都是静默失效，所以 Kimi 只能靠**正向识别**，兜底救不了它。
    """
    if event_session_path_is_codex_rollout(event):
        return resolve_host_adapter(HOST_CODEX)
    session_path = _first_readable_session_scope_path_for_host_resolution(event)
    detected = None
    if session_path is not None:
        try:
            detected = detect_transcript_format(session_path)
        except Exception:  # noqa: BLE001 - 探测失败按未知处理，走兜底
            detected = None
    if detected == HOST_CODEX:
        return resolve_host_adapter(HOST_CODEX)
    if detected == HOST_KIMI_CODE:
        return resolve_host_adapter(HOST_KIMI_CODE)
    if detected == HOST_CLAUDE:
        # 第 3.5 条：**所有** schema 证据都排在**所有** stamp 之前。对既有行为是恒等的
        # ——探测出 Claude 时，旧代码要么走 claude-code stamp 分支、要么落到末尾的
        # ``resolve_host_adapter(detected)``，两条路都返回 Claude。它存在是为了让新增的
        # 第二枚 stamp（kimi-code）无法抢走一个内容上明确是 Claude 的会话。
        return resolve_host_adapter(HOST_CLAUDE)
    source = event.get("source")
    if isinstance(source, dict):
        stamped_host = HOST_BY_PLUGIN_ENTRY_SOURCE_PROVIDER_STAMP.get(source.get("provider"))
        if stamped_host is not None:
            return resolve_host_adapter(stamped_host)
    return resolve_host_adapter(detected)


def _self_check() -> None:
    """runnable check：两 bundle 满足 Protocol、resolver 按 host 正确装配、各方法行为正确。

    这是本装配模块唯一非平凡行为的可运行检查——契约 gate 直接跑本 ``__main__``，
    覆盖 ``resolve_cline_kanban_agent_id`` 集成测试未触及的 ``host_display_name`` /
    ``session_deep_link`` / ``detect_host_skip_mode`` 三法。
    """
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

    kimi = resolve_host_adapter(HOST_KIMI_CODE)
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

    # host_synthesized_user_prompt_submit_turn_kind：只有 Claude 认 ``<task-notification>``；同一段正文
    # 在 Codex / Kimi 会话里就是开发者 prompt（识别归注入面，core 不写 host 字面量）。
    from adapters.claude_code.claude_code_host_adapter import (
        CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_TURN_KIND,
    )

    notification = (
        "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"
    )
    assert (
        claude.host_synthesized_user_prompt_submit_turn_kind({}, notification)
        == CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_TURN_KIND
    )
    assert claude.host_synthesized_user_prompt_submit_turn_kind({}, "\n  " + notification) == (
        CLAUDE_CODE_BACKGROUND_TASK_COMPLETION_NOTIFICATION_TURN_KIND
    )
    assert claude.host_synthesized_user_prompt_submit_turn_kind(
        {}, "帮我看看 <task-notification> 这个标签是什么意思"
    ) is None, "只在句中提到标签的开发者 prompt 不是合成回合"
    assert claude.host_synthesized_user_prompt_submit_turn_kind({}, "<task-notification>") is None, (
        "缺闭标签 ⇒ 不是完整的合成通知，宁可当开发者 prompt"
    )
    assert codex.host_synthesized_user_prompt_submit_turn_kind({}, notification) is None
    assert kimi.host_synthesized_user_prompt_submit_turn_kind({}, notification) is None

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
