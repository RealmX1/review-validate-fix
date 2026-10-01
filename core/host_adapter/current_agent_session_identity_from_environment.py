"""从 agent shell 的环境变量解析「当前 agent 会话 id」与「当前 host 的等待方式」。

跑在 agent shell 里的 RVF 命令（``rvf`` 快捷入口、``seal_round_baseline_to_head``）拿不到 hook
事件里的 ``session_id``，只能读 harness 原生注入的环境变量。各家实测注入的名字：

* Claude Code：``CLAUDE_CODE_SESSION_ID``（与 Stop/UPS hook 事件的 ``session_id`` 同值）。
  历史上 ``seal_round_baseline_to_head`` 读的是 ``CLAUDE_SESSION_ID``——Claude Code **从未**
  设过这个名字，于是非 kanban 的 Claude 会话里封窗一直静默 no-op。
* Codex CLI：``CODEX_THREAD_ID``（exec_command 子进程环境白名单里的那一项，与 rollout 的
  thread id 同值）；``CODEX_SESSION_ID`` 作为兼容名一并认。
* 操作者显式覆盖：``RVF_SESSION_ID``。

本模块只做**读环境**，不 import 任何 host SDK，也不 spawn 子进程。
"""

from __future__ import annotations

import os
from typing import Mapping

from core.host_adapter.parent_harness_identity import (
    PARENT_HARNESS_IDENTITY_CLAUDE_CODE,
    PARENT_HARNESS_IDENTITY_CODEX_CLI,
    PARENT_HARNESS_IDENTITY_UNSPECIFIED,
    resolve_parent_harness_identity,
)

# 读取顺序即优先级：显式覆盖 > Claude > Codex。
CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES: tuple[str, ...] = (
    "RVF_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
)

# Codex 的 exec_command 子进程只保证带 ``CODEX_THREAD_ID``；``parent_harness_identity`` 的原生
# 标记层只认 ``CODEX_SESSION_ID``（它服务 marker 键归一，改它会动到封窗键），故 host 判定在这里
# 单独补认 thread id，不改那张共享表。
CODEX_EXEC_COMMAND_THREAD_ID_ENVIRONMENT_VARIABLE_NAME = "CODEX_THREAD_ID"


def resolve_current_agent_session_id_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    environment = environ if environ is not None else os.environ
    for name in CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES:
        value = environment.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ``rvf wait`` 的等待方式（host 专属能力，按 host 分支）：
# * Claude：Bash ``run_in_background`` 跑一次长等待，进程退出时 agent 被完成通知唤醒——零轮询。
# * Codex：exec 会话里跑一次长等待，agent 用 ``write_stdin`` 空轮询、每次尽量长地 yield。
# * 其它 host（Kimi 等）：保持有界轮询，单次阻塞 < 前台工具上限。
REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION = (
    "claude_background_task_completion_notification"
)
REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD = "codex_single_long_exec_session_yield"
REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING = "bounded_foreground_polling"


def resolve_current_host_reviewer_wait_mode(environ: Mapping[str, str] | None = None) -> str:
    environment = environ if environ is not None else os.environ
    identity = resolve_parent_harness_identity(environ=environment)
    if identity == PARENT_HARNESS_IDENTITY_CLAUDE_CODE:
        return REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION
    if identity == PARENT_HARNESS_IDENTITY_CODEX_CLI:
        return REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD
    if identity == PARENT_HARNESS_IDENTITY_UNSPECIFIED and (
        environment.get(CODEX_EXEC_COMMAND_THREAD_ID_ENVIRONMENT_VARIABLE_NAME) or ""
    ).strip():
        return REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD
    return REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING


# 主 agent 结束回合去等后台工作时，host 会不会在那份后台工作结束时**重新唤起**它（host 专属能力）。
# 只有会唤起的 host，等待期间才能让 Stop 静默（awaiting-dispatched-agent shield）——否则静默就是
# 把停下来的 agent 永久晾着，续跑提示反而是唯一能把它拉回来的东西。
# * Claude Code：后台 Bash（``run_in_background``）与后台子代理结束时都有完成通知 ⇒ 会唤起。
# * Codex：exec 会话 / 子代理结束后不会在回合外唤起主 agent（agent 自己 ``write_stdin`` / ``wait_agent``）。
# * 其它 host（Kimi 等）：未取证到会唤起，按不会处理（宁可多一次续跑提示，也不晾住 agent）。
REVIEWER_WAIT_MODES_WHERE_HOST_WAKES_MAIN_AGENT_WHEN_BACKGROUND_WORK_COMPLETES = frozenset(
    {REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION}
)


def current_host_wakes_main_agent_when_background_work_completes(
    environ: Mapping[str, str] | None = None,
) -> bool:
    return (
        resolve_current_host_reviewer_wait_mode(environ)
        in REVIEWER_WAIT_MODES_WHERE_HOST_WAKES_MAIN_AGENT_WHEN_BACKGROUND_WORK_COMPLETES
    )


__all__ = [
    "CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES",
    "REVIEWER_WAIT_MODES_WHERE_HOST_WAKES_MAIN_AGENT_WHEN_BACKGROUND_WORK_COMPLETES",
    "REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING",
    "REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION",
    "REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD",
    "current_host_wakes_main_agent_when_background_work_completes",
    "resolve_current_agent_session_id_from_environment",
    "resolve_current_host_reviewer_wait_mode",
]
