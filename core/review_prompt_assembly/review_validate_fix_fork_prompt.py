"""RVF fork prompt 的父会话来源块装配——host-中性核（S10g，从引擎 PROMPT_ASSEM 簇迁入）。

``parent_origin_prompt_block`` 把已冻结的父会话来源 dict 渲染成注入 fork prompt 的
``RVF_PARENT_CONVERSATION_*`` / ``RVF_PARENT_KANBAN_*`` 元数据块；``add_parent_origin_to_rvf_fork_prompt``
把该块追加到一段已含 fork marker 的 prompt 尾部（幂等，marker 缺失或已注入则原样返回）。

**R-1 边界**：块标题里的 host 人类可读名由调用方（引擎 assembly wrapper）经注入的
``HostAdapter.host_display_name()`` 预解析后以 ``host_display_label`` 字符串传入，本模块
不出现任何 host 身份常量或 host 归属键 token。``codex_url`` 等中性字段名不触 R-1（守卫只
禁 host 身份常量/归属键）。
"""

from __future__ import annotations

from typing import Any

from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import RVF_FORK_MARKER


def value_or_unavailable(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None:
        text = str(value).strip()
        if text:
            return text
    return "<unavailable>"


def parent_origin_prompt_block(
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
    host_display_label: str,
) -> str:
    parent_conversation_ref = value_or_unavailable(
        parent_origin.get("label") or parent_origin.get("session_id")
    )
    parent_conversation_source = value_or_unavailable(parent_origin.get("name_source"))
    parent_codex_url = value_or_unavailable(parent_origin.get("codex_url"))
    parent_transcript_path = value_or_unavailable(parent_origin.get("transcript_path"))
    parent_transcript_file = value_or_unavailable(parent_origin.get("transcript_file"))
    parent_origin_path = value_or_unavailable(origin_path)
    host_label = host_display_label
    lines = [
        f"Original {host_label} conversation metadata:\n"
        f"RVF_PARENT_CONVERSATION_REF: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME_SOURCE: {parent_conversation_source}\n"
        f"RVF_PARENT_CODEX_URL: {parent_codex_url}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {parent_transcript_path}\n"
        f"RVF_PARENT_TRANSCRIPT_FILE: {parent_transcript_file}\n"
        f"RVF_ORIGIN_METADATA: {parent_origin_path}\n"
    ]
    if parent_origin.get("source_kind") == "cline-kanban-task":
        lines.append(
            "RVF_PARENT_SOURCE_KIND: cline-kanban-task\n"
            f"RVF_PARENT_KANBAN_TASK_ID: {value_or_unavailable(parent_origin.get('kanban_task_id'))}\n"
            f"RVF_PARENT_KANBAN_ATTEMPT_ID: {value_or_unavailable(parent_origin.get('kanban_attempt_id'))}\n"
            f"RVF_PARENT_KANBAN_TASK_TITLE: {value_or_unavailable(parent_origin.get('kanban_task_title'))}\n"
            "RVF_PARENT_KANBAN_TASK_TITLE_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('kanban_task_title_source'))}\n"
            "RVF_PARENT_SOURCE_SESSION_REF: "
            f"{value_or_unavailable(parent_origin.get('source_session_label'))}\n"
            "RVF_PARENT_SOURCE_SESSION_NAME_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('source_session_name_source'))}\n"
        )
    lines.append(
        "\n"
        "维护 handoff.md 时，`## Origin` 必须逐字保留上面的 original "
        f"{host_label} conversation name/ref、name source、codex URL、transcript path "
        "和 origin metadata path；如果存在 `RVF_PARENT_KANBAN_TASK_ID`，还必须写入 "
        "`source Kanban task id` 和 `source Kanban attempt id`，以便任务改名后仍可反查"
        "当前 task title；不要把 `RVF_PARENT_SESSION_ID` 当成 conversation name source。"
    )
    return "".join(lines)


def add_parent_origin_to_rvf_fork_prompt(
    prompt: str,
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
    host_display_label: str,
) -> str:
    if RVF_FORK_MARKER not in prompt:
        return prompt
    if "RVF_PARENT_CONVERSATION_NAME_SOURCE:" in prompt:
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        f"{parent_origin_prompt_block(parent_origin=parent_origin, origin_path=origin_path, host_display_label=host_display_label)}"
    )
