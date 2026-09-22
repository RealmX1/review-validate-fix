"""让 vendored ``codex_invoked_skill`` 认得第三代 Codex rollout schema 的薄桥接。

为什么要有这一层
----------------
``codex_invoked_skill.py`` 是从 ``RealmX1/codex-invoked-skill`` 单文件 vendored 进来的，
其文件头明写 **“Do not edit here — change upstream and re-vendor.”**——在本地直接打补丁会
在下次 re-vendor 时被静默冲掉，是典型的「修好了又悄悄坏回去」。

而它确实盲了一处：``_iter_user_messages`` 只认第二代的
``event_msg`` / ``payload.type == "user_message"``，**没有任何兜底**。2026-08 的
``codex-tui`` rollout（第三代）把用户消息搬进了
``event_msg`` / ``payload.type == "item_completed"`` 的 ``item.type == "UserMessage"``，
承载显式 ``$skill`` 调用标记的 ``text_elements`` 也随之搬到 ``item.content[].text_elements``。
标记本身完好——实测那次失败的 transcript 里就有

    {"byte_range": {"start": 0, "end": 40},
     "placeholder": "$review-validate-fix:review-validate-fix"}

即开发者**确实手动敲了** ``$review-validate-fix``，而 RVF 没认出来。它的失败形式是「什么都
没发生」，比自动路径的 loud-skip 更隐蔽。

本模块的边界
------------
只重写 **schema 耦合的那一段**（怎么从 rollout 里迭代出用户消息 payload），并把结果整形成
vendored 模块期待的第二代形状 ``{"message": str, "text_elements": [...]}``；**skill 匹配
判定一律回调 vendored 模块**（``_element_to_skill`` / ``match_invocation_in_text`` /
``InvokedSkill.matches``），不复制第二套判定——避免再造第三套触发检测。

上游可提 issue（``text_elements`` 搬进 ``item.content[]``）；上游修好并 re-vendor 后，
本模块可整体删除，消费者改回直接 import vendored 模块即可。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

import _rvf_pyroot  # noqa: F401  — 把 pyroot 加入 sys.path，供 core.* import

import codex_invoked_skill as _vendored
from core.transcript.codex_rollout_item_envelope import (  # noqa: E402
    codex_item_of_type,
    user_message_text_and_elements_from_user_message_item,
)

# 直接透传 vendored 的类型与纯文本兜底：调用方按需从本模块取，无需再 import 两个模块。
InvokedSkill = _vendored.InvokedSkill
parse_placeholder = _vendored.parse_placeholder
match_invocation_in_text = _vendored.match_invocation_in_text

__all__ = [
    "InvokedSkill",
    "parse_placeholder",
    "match_invocation_in_text",
    "invoked_skills_from_transcript",
    "invoked_skills_from_event",
    "was_skill_invoked",
]

# 行级快筛：第二代写 ``"user_message"``，第三代写 ``"UserMessage"``。两者都不含 → 跳过。
# 与 vendored 的单串快筛同一意图（避免对每行做 json.loads），只是多认一代。
_USER_MESSAGE_LINE_HINTS = ('"user_message"', '"UserMessage"')


def _iter_user_message_payloads(transcript_path: "str | Path") -> "Iterator[dict[str, Any]]":
    """按文件顺序产出「第二代形状」的用户消息 payload，跨两代 schema。

    第二代原样产出；第三代把 ``item_completed`` 信封里的 ``UserMessage`` item 整形成
    ``{"message": ..., "text_elements": [...]}``。best-effort：文件不可读 → 不产出；
    坏行跳过（与 vendored 同口径）。
    """
    path = Path(transcript_path).expanduser()
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or not any(hint in line for hint in _USER_MESSAGE_LINE_HINTS):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "user_message":
                yield payload
                continue
            item = codex_item_of_type(payload, "UserMessage")
            if item is None:
                continue
            text, elements = user_message_text_and_elements_from_user_message_item(item)
            yield {"message": text, "text_elements": elements}


def _elements(payload: "dict[str, Any]") -> "list[InvokedSkill]":
    """整形后的 payload → InvokedSkill 列表；逐元素判定全部回调 vendored。"""
    out: "list[InvokedSkill]" = []
    for element in payload.get("text_elements") or []:
        skill = _vendored._element_to_skill(element)
        if skill is not None:
            out.append(skill)
    return out


def invoked_skills_from_transcript(
    transcript_path: "str | Path",
    *,
    which: str = "latest",
    match_prompt: "str | None" = None,
) -> "list[InvokedSkill]":
    """与 ``codex_invoked_skill.invoked_skills_from_transcript`` 同语义、同参数，
    但迭代器认第二代与第三代两种 rollout schema。"""
    payloads = list(_iter_user_message_payloads(transcript_path))
    if match_prompt is not None:
        for payload in reversed(payloads):
            if payload.get("message") == match_prompt:
                return _elements(payload)
        return []
    if which == "all":
        out: "list[InvokedSkill]" = []
        for payload in payloads:
            out.extend(_elements(payload))
        return out
    if which == "latest":
        return _elements(payloads[-1]) if payloads else []
    raise ValueError(f"which must be 'latest' or 'all', got {which!r}")


def invoked_skills_from_event(
    event: "dict[str, Any]",
    *,
    which: str = "latest",
    match_prompt: "str | bool | None" = None,
) -> "list[InvokedSkill]":
    """与 ``codex_invoked_skill.invoked_skills_from_event`` 同语义；transcript 路径的
    提取沿用 vendored 的 ``_transcript_from_event``（key 列表是上游契约）。"""
    transcript = _vendored._transcript_from_event(event)
    if transcript is None:
        return []
    pin: "str | None"
    if match_prompt is True:
        prompt = event.get("prompt")
        pin = prompt if isinstance(prompt, str) else None
    elif isinstance(match_prompt, str):
        pin = match_prompt
    else:
        pin = None
    return invoked_skills_from_transcript(transcript, which=which, match_prompt=pin)


def was_skill_invoked(
    source: "str | Path | dict[str, Any]",
    name: str,
    *,
    namespace: "str | None" = None,
    which: str = "latest",
) -> bool:
    """与 ``codex_invoked_skill.was_skill_invoked`` 同语义；匹配用 vendored 的
    ``InvokedSkill.matches``。"""
    if isinstance(source, dict):
        skills = invoked_skills_from_event(source, which=which)
    else:
        skills = invoked_skills_from_transcript(source, which=which)
    return any(skill.matches(name, namespace=namespace) for skill in skills)


def known_invocations_in_text(prompt: str, known: Iterable[str]) -> "list[str]":
    """``match_invocation_in_text`` 的同名转发（保持本模块作为单一 import 点）。"""
    return _vendored.match_invocation_in_text(prompt, known)
