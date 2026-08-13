"""父 harness 身份 token（供 scope marker key 使用的稳定短标识）。

**这个模块解决什么问题**：``round_baseline_marker`` 记录的是「上一条 user prompt
提交时的 HEAD」——一个**每个会话各一份**的事实。但它的 key 一直是 ``task_id`` 优先，
而一个 cline-kanban task 的 worktree 可以同时被两个 harness（Claude 与 Codex）打开。
于是两个 harness 各自的 UserPromptSubmit 会写进**同一个文件**、互相覆盖：A 的 Stop
读到的是 B 上一条 prompt 的 HEAD，窗口下界被抬高 ⇒ 漏审。

这个分裂在历史上一直被**双状态根偶然掩盖着**（Claude hook 落 ``~/.claude/rvf``、
shell 进程落 ``~/.rvf/user-local-data/run-logs``），统一根之后它会立刻开始流血。
把 harness 身份编进 marker key 是消除它的最小手术。

**为什么放在 ``core/host_adapter/``**：本包是 RVF 里唯一允许出现 host 身份词汇
（``HOST_CODEX`` / ``HOST_CLAUDE`` / ``host_kind``）的地方，core 的 host-free 退出门
对本包开 carve-out。marker key 需要一个 host 身份 token，故 resolver 住这里，
``core/session_scope_allocation/`` 只消费返回值、不认识任何 host 名字。

**规范化是承重的，不是修饰**。同一个 harness 在不同调用点会命中不同的信号层：

===============================  ===================================  ===========
调用点                            实际可见的信号                        命中层
===============================  ===================================  ===========
UserPromptSubmit hook 子进程       ``CODEX_RVF_CLINE_KANBAN_AGENT_ID``   ②
Stop hook 子进程                   同上（同一 shim 注入）                 ②
``seal_round_baseline_to_head``   agent shell：``CLAUDECODE`` /         ④/⑤
（``/rvf-land`` 里以普通 CLI 跑）    ``AI_AGENT=claude-code_2-1-231_…``
===============================  ===================================  ===========

三个点写 / 读的是**同一个 marker**。若 ② 归一成 ``claude``、④ 归一成 ``claude_code``、
⑤ 归一成 ``claude-code_2-1-231_agent``，key 就会静默裂成三份——写者写 A、读者读 B，
marker 凭空消失。所以每一层都必须收敛到**同一个封闭词表**，且有测试逐层钉死
（``tests/test_review_support_scripts.py`` 的跨信号层一致性用例）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from core.host_adapter.host_transcript_format_detection import (
    HOST_CLAUDE,
    HOST_CODEX,
    detect_transcript_format,
)

PARENT_HARNESS_IDENTITY_CLAUDE_CODE = "claude-code"
PARENT_HARNESS_IDENTITY_CODEX_CLI = "codex-cli"
PARENT_HARNESS_IDENTITY_CURSOR_CLI = "cursor-cli"
PARENT_HARNESS_IDENTITY_UNSPECIFIED = "unspecified"
"""无法判定父 harness 时的取值。

**它是词表里的正常一员，不是 sentinel**：读侧候选列表显式包含 ``unspecified``
作为回退，于是「写者判不出、读者判得出」只会退化成**取到更保守的下界**（窗口偏宽
⇒ 多审），而不是 marker 找不到（窗口消失 ⇒ 漏审）。迁移工具写出的历史 marker 也用
这个取值——旧 marker 里根本没记过 harness，无从恢复。
"""

CLINE_KANBAN_AGENT_ID_ENV = "CODEX_RVF_CLINE_KANBAN_AGENT_ID"
KANBAN_RUNTIME_AGENT_DESCRIPTOR_ENV = "AI_AGENT"

# 各 harness 在**自己启动的进程树**里留下的原生环境标记（不经 RVF shim 注入）。
# ``seal_round_baseline_to_head`` 跑在 agent shell 里、拿不到 shim 注入的 agent id，
# 只能靠这一层。实测 Claude Code 会设 ``CLAUDECODE=1`` 与 ``CLAUDE_CODE_SESSION_ID``。
_HARNESS_NATIVE_ENVIRONMENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", PARENT_HARNESS_IDENTITY_CLAUDE_CODE),
    ("CLAUDE_CODE_SESSION_ID", PARENT_HARNESS_IDENTITY_CLAUDE_CODE),
    ("CODEX_SESSION_ID", PARENT_HARNESS_IDENTITY_CODEX_CLI),
    ("CODEX_HOME", PARENT_HARNESS_IDENTITY_CODEX_CLI),
    ("CURSOR_AGENT", PARENT_HARNESS_IDENTITY_CURSOR_CLI),
)

# 把「同一个 harness 的各种写法」折叠到封闭词表。键按**前缀**匹配，因为信号里既有
# 裸名（agent id ``claude``）、也有带版本的描述串（``AI_AGENT=claude-code_2-1-231_agent``）。
_HARNESS_IDENTITY_ALIAS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", PARENT_HARNESS_IDENTITY_CLAUDE_CODE),
    ("codex", PARENT_HARNESS_IDENTITY_CODEX_CLI),
    ("cursor", PARENT_HARNESS_IDENTITY_CURSOR_CLI),
)

_HARNESS_IDENTITY_MAX_TOKEN_CHARS = 32


def _sanitize_harness_identity_token(value: str) -> str:
    """未知 harness 也给它**自己的** bucket，而不是一律折进 ``unspecified``。

    折进 ``unspecified`` 会让两个互不相干的未知 harness 重新开始互相覆盖——正是本模块
    要消除的那件事。同时限长并只保留文件名安全字符（token 会直接进 marker 文件名）。
    """
    kept = [character if character.isalnum() else "-" for character in value.lower()]
    token = "".join(kept).strip("-")
    while "--" in token:
        token = token.replace("--", "-")
    return token[:_HARNESS_IDENTITY_MAX_TOKEN_CHARS].strip("-")


def normalize_parent_harness_identity(raw_signal: str | None) -> str | None:
    """把任意一层信号的原始取值折叠成封闭词表里的 token；空 / 无意义 → None。

    ``None`` 表示「这一层没给出信号」，调用方应继续往下一层探；它与
    ``PARENT_HARNESS_IDENTITY_UNSPECIFIED`` 语义不同（后者是「所有层都探过了，判不出」）。
    """
    if not isinstance(raw_signal, str):
        return None
    text = raw_signal.strip().lower()
    if not text:
        return None
    if text == PARENT_HARNESS_IDENTITY_UNSPECIFIED:
        return PARENT_HARNESS_IDENTITY_UNSPECIFIED
    # ``HOST_CLAUDE``/``HOST_CODEX`` 是 transcript-format 探测的取值，与 agent id
    # 拼写不同（``claude_code`` vs ``claude``）；前缀表把两套一并收进同一 token。
    if text == HOST_CLAUDE:
        return PARENT_HARNESS_IDENTITY_CLAUDE_CODE
    if text == HOST_CODEX:
        return PARENT_HARNESS_IDENTITY_CODEX_CLI
    for prefix, identity in _HARNESS_IDENTITY_ALIAS_PREFIXES:
        if text.startswith(prefix):
            return identity
    sanitized = _sanitize_harness_identity_token(text)
    return sanitized or None


def resolve_parent_harness_identity(
    *,
    explicit_host_kind: str | None = None,
    parent_transcript_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """判定父 harness 身份 token；判不出返回 ``PARENT_HARNESS_IDENTITY_UNSPECIFIED``。

    信号层按**可信度**排序，逐层下探（每一层都过 ``normalize_parent_harness_identity``）：

    ① ``explicit_host_kind``——调用方已经从 ``origin.json`` / ``parent_origin`` 拿到的
       ``host_kind``。这是唯一「被判定过并落过盘」的取值，故最高。
    ② ``CODEX_RVF_CLINE_KANBAN_AGENT_ID``——由各 host 的 hook shim 注入。hook 子进程
       （UPS / Stop）走这一层，也是本 marker 读写两侧最常命中的那层。
    ③ 父 transcript 的 schema 探测——手上有 transcript 路径时最直接的事实。
    ④ harness 原生环境标记——agent shell 里跑的 CLI（``seal_round_baseline_to_head``）
       只有这一层可用。
    ⑤ ``AI_AGENT``——cline-kanban runtime 写的带版本描述串，仅在 kanban task 内存在。

    纯函数式：``environ`` 可注入，不可注入时读 ``os.environ``；探测失败一律不抛。
    """
    resolved = normalize_parent_harness_identity(explicit_host_kind)
    if resolved:
        return resolved

    environment = environ if environ is not None else os.environ

    resolved = normalize_parent_harness_identity(environment.get(CLINE_KANBAN_AGENT_ID_ENV))
    if resolved:
        return resolved

    if parent_transcript_path:
        try:
            detected = detect_transcript_format(Path(parent_transcript_path))
        except (OSError, ValueError):
            detected = None
        resolved = normalize_parent_harness_identity(detected)
        if resolved:
            return resolved

    for variable_name, identity in _HARNESS_NATIVE_ENVIRONMENT_MARKERS:
        value = environment.get(variable_name)
        if isinstance(value, str) and value.strip():
            return identity

    resolved = normalize_parent_harness_identity(
        environment.get(KANBAN_RUNTIME_AGENT_DESCRIPTOR_ENV)
    )
    if resolved:
        return resolved

    return PARENT_HARNESS_IDENTITY_UNSPECIFIED
