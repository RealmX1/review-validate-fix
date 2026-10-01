"""Kimi Code CLI host transcript adapter（读 per-agent ``wire.jsonl``）。

Kimi 的 wire JSONL 与 Codex rollout / Claude transcript 有一处**结构性**差异，是本模块
一切设计的起点：

> **助手的输出与工具调用都不在 ``context.append_message`` 里。**

实测（本机 42 份 wire.jsonl）``context.append_message`` 只承载 ``role:"user"`` 的消息；
agent 的实际活动全部落在 ``context.append_loop_event`` 的 ``event`` 下：

| ``event.type`` | 承载什么 |
| --- | --- |
| ``content.part`` | 助手输出。``part.type`` 实测只有 ``think`` 与 ``text``；**``think`` 的正文字段是 ``part.think``，不是 ``part.text``** |
| ``tool.call`` | ``name`` / ``args`` / ``toolCallId`` |
| ``tool.result`` | ``toolCallId`` / ``result.output``，经 ``toolCallId`` 与 call 配对 |
| ``step.begin`` / ``step.end`` | 步骤边界与用量 |

首行恒为 ``{"type":"metadata","protocol_version":…}``（实测 42/42）。

## `origin.kind` 决定一条记录是不是真的来自用户

``context.append_message`` 里 ``role`` 实测**全是** ``"user"``，真正区分来源的是
``origin.kind``。本机 48 份 wire 实测到 5 种：``"user"``（真实输入）以及 4 种 harness 自己
写的——``"injection"``（系统提醒，带 ``variant``，实测 ``permission_mode`` /
``todo_list_reminder``）、``"system_trigger"``（子代理派发 prompt）、``"skill_activation"``、
``"background_task"``（后台任务完成通知）。100% 的记录都带 ``origin``。

**不区分就会把 harness 写的正文当成用户发言**，而 RVF 的 scope 归属恰恰建立在「用户说了
什么」上。本模块把所有非 user 来源映射成 ``phase_marker``（``marker="<kind>[:<variant>]"``）
而不是 ``message``：既不污染 message 语义，也不丢信息。判据是**正向**的
``origin_kind_is_real_user_input``（``kind == "user"``）而不是「不等于 injection」，且**会话
命名与蒸馏共用同一个函数**——各写一份必然漂移。

``turn.prompt`` 也带自己的 ``origin``（实测 33 条 ``user`` + 19 条 ``system_trigger``）。它与
``origin.kind == "user"`` 的 ``context.append_message`` 是**同一条输入的两次记录**（实测逐条
对应），故蒸馏只认后者、跳过前者，避免把每条用户输入算两遍。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import

from core.transcript.io import (  # noqa: E402
    SCHEMA_VERSION,
    _byte_range_for_line,
    _read_jsonl_with_offsets,
    _truncate,
)
from core.transcript.models import TranscriptRecord  # noqa: E402
from core.transcript.patch_parsing import (  # noqa: E402
    apply_patch_hunk_line_range_for_path,
    apply_patch_operation_to_artifact_verb,
    extract_apply_patch_text_from_bash_command,
    parse_apply_patch_operations_without_repo,
)
from core.session_scope_allocation.session_change_manifest import (  # noqa: E402
    parse_apply_patch,
)

WIRE_RECORD_TYPE_METADATA = "metadata"
WIRE_RECORD_TYPE_TURN_PROMPT = "turn.prompt"
WIRE_RECORD_TYPE_CONTEXT_APPEND_MESSAGE = "context.append_message"
WIRE_RECORD_TYPE_CONTEXT_APPEND_LOOP_EVENT = "context.append_loop_event"

LOOP_EVENT_TYPE_CONTENT_PART = "content.part"
LOOP_EVENT_TYPE_TOOL_CALL = "tool.call"
LOOP_EVENT_TYPE_TOOL_RESULT = "tool.result"

CONTENT_PART_TYPE_TEXT = "text"
CONTENT_PART_TYPE_THINK = "think"

CONTENT_PART_TEXT_FIELD_BY_PART_TYPE = {
    CONTENT_PART_TYPE_TEXT: "text",
    CONTENT_PART_TYPE_THINK: "think",
}
"""part 的正文字段名**随 part 类型而变**。

``{"type":"think","think":"…"}`` —— think 的正文在 ``think`` 键下而不是 ``text`` 键下。
按 ``part["text"]`` 一把抓会让所有推理内容静默变成空串（think 实测占 content.part 的 78%）。
"""

FILE_MUTATING_TOOL_WIRE_NAMES = frozenset({"Write", "Edit", "Bash"})
"""写型工具的 **wire 名**（实测自真实 tool.call，非二进制类名）。

⚠️ 类名与 wire 名**不是一一对应**：二进制里注册的是 ``WriteTool`` / ``EditTool`` / ``BashTool``，
而 wire 上还出现了 ``ReadMediaFile`` / ``AgentSwarm`` 这类在类名列表里看不出的名字。
所以写操作归因必须按 wire 名，不能从类名推导。
"""


def _iter_wire_records(path: Path, max_records: int | None = None):
    """逐行 yield JSON-decodable 的 dict record；非法行跳过而不抛。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            emitted = 0
            for raw in handle:
                if max_records is not None and emitted >= max_records:
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    emitted += 1
                    yield record
    except OSError:
        return


def _text_from_input_parts(parts: Any) -> str | None:
    """把 ``turn.prompt.input``（part 列表，**不是**字符串）折叠成一段文本。"""
    if isinstance(parts, str):
        return parts.strip() or None
    if not isinstance(parts, list):
        return None
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        field = CONTENT_PART_TEXT_FIELD_BY_PART_TYPE.get(
            part.get("type"), CONTENT_PART_TYPE_TEXT
        )
        value = part.get(field)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    joined = "\n".join(chunks).strip()
    return joined or None


MESSAGE_ORIGIN_KIND_USER = "user"

KIMI_TRANSCRIPT_SOURCE = "kimi_code"


def _origin_kind(container: Any) -> str | None:
    """``turn.prompt`` 与 ``message`` 都把来源放在自己的 ``origin.kind`` 下。"""
    origin = container.get("origin") if isinstance(container, dict) else None
    if not isinstance(origin, dict):
        return None
    kind = origin.get("kind")
    return kind if isinstance(kind, str) else None


def origin_kind_is_real_user_input(container: Any) -> bool:
    """「这段文本真的来自用户吗」的**唯一**判据——会话命名与蒸馏共用这一个函数。

    判据是**正向**的 ``origin.kind == "user"``，而不是「不等于 injection」：本机 48 份真实
    wire 里非 user 的 ``origin.kind`` 实测有 4 种（``injection`` / ``system_trigger`` /
    ``skill_activation`` / ``background_task``），且 100% 的记录都带 ``origin``。负向判据只
    挡得住当时列举过的那几种，新增来源会默认落进「用户发言」——静默地。

    ⚠️ 不能用 ``role`` 代替：``context.append_message`` 的 ``role`` 实测**全是** ``"user"``，
    它区分不出来源。两处各写一份判据必然漂移，届时会话名会退化成 system-reminder 正文或
    子代理派发 prompt。
    """
    return _origin_kind(container) == MESSAGE_ORIGIN_KIND_USER


def first_user_prompt_text(path: Path, max_records: int = 256) -> str | None:
    """抽取 wire.jsonl 里第一条**用户**输入的正文。

    两个来源都认（取先出现者）：``turn.prompt`` 与 ``context.append_message``，但两者都必须
    先过 ``origin_kind_is_real_user_input``。实测这两者在同一次输入上会**成对出现**且内容
    一致，认任一即可；同时收下是为了不依赖「哪一条先落盘」这个未取证的顺序。

    ``turn.prompt`` 自己也带 ``origin``（本机实测 52 条 = 33 条 ``user`` + 19 条
    ``system_trigger``）：子代理 wire 的首条 ``turn.prompt`` 恰恰就是 harness 写的派发
    prompt，不过滤就会把它当成「用户说的第一句话」。
    """
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    for record in _iter_wire_records(path, max_records=max_records):
        record_type = record.get("type")
        if record_type == WIRE_RECORD_TYPE_TURN_PROMPT:
            if not origin_kind_is_real_user_input(record):
                continue
            text = _text_from_input_parts(record.get("input"))
            if text:
                return text
        elif record_type == WIRE_RECORD_TYPE_CONTEXT_APPEND_MESSAGE:
            message = record.get("message")
            if not isinstance(message, dict) or not origin_kind_is_real_user_input(message):
                continue
            text = _text_from_input_parts(message.get("content"))
            if text:
                return text
    return None


def latest_assistant_reply_text_in_most_recent_turn(path: Path) -> str | None:
    """最近一个回合里、最后一个产出过 ``text`` 的 step 的助手回复正文；该回合没有回复则 None。

    Stop hook 的 handoff 标记识别靠它：Kimi 的 Stop 事件没有 ``last_assistant_message``
    （见 ``hooks/_kimi_hook_entry.py``），只能从 wire 里还原。此前回落读取器只认 Codex rollout，
    于是 Kimi 上的 RVF run 写完 handoff 也永远不 finalize（F5·H5a：4/4）。

    * 回合边界是 ``turn.prompt``：遇到就清空——上一回合的 handoff 标记不能在下一回合被再次
      认出，否则之后每次 Stop 都会被当成 handoff 回合。
    * 只收 ``part.type == "text"``；``think`` 不算回复（实测 Kimi 会在 think 里复述
      「first line ``RVF_HANDOFF_FILE: <absolute path>``」）。
    * 同一 step 的多个 text part 按出现顺序拼接（本机 210 份 wire / 1040 个出文字的 step 实测
      每 step 至多一条，拼接只是防御）。
    """
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    latest_step_identity: tuple[Any, Any, Any] | None = None
    latest_step_text_chunks: list[str] = []
    for record in _iter_wire_records(path):
        record_type = record.get("type")
        if record_type == WIRE_RECORD_TYPE_TURN_PROMPT:
            latest_step_identity = None
            latest_step_text_chunks = []
            continue
        if record_type != WIRE_RECORD_TYPE_CONTEXT_APPEND_LOOP_EVENT:
            continue
        event = record.get("event")
        if not isinstance(event, dict) or event.get("type") != LOOP_EVENT_TYPE_CONTENT_PART:
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != CONTENT_PART_TYPE_TEXT:
            continue
        text = part.get(CONTENT_PART_TEXT_FIELD_BY_PART_TYPE[CONTENT_PART_TYPE_TEXT])
        if not isinstance(text, str) or not text.strip():
            continue
        step_identity = (event.get("turnId"), event.get("step"), event.get("stepUuid"))
        if step_identity != latest_step_identity:
            latest_step_identity = step_identity
            latest_step_text_chunks = []
        latest_step_text_chunks.append(text)
    joined = "\n".join(latest_step_text_chunks)
    return joined or None


_ARTIFACT_PATH_ARGUMENT_NAME = "path"
"""⚠️ Kimi 的 ``Write`` / ``Edit`` 用 ``path``，**不是** Claude 的 ``file_path``。

照抄 Claude 的键名会让 artifact_refs 恒为空——静默的，因为缺键只是拿不到值、不会抛。
"""


def _kimi_tool_call_artifact_refs(
    tool_name: Any, args: Any, *, repo: Path | None, line_number: int
) -> list[dict[str, Any]]:
    """由 ``tool.call`` 推断 ``artifact_refs``。

    Kimi 的 ``Edit`` / ``Write`` 参数不含行号（与 Codex 的 apply_patch 不同），``lines`` 写
    ``None``；``Bash`` 里检出 apply_patch heredoc 后复用 host-中性的 patch 解析 helper。
    """
    if not isinstance(args, dict):
        return []
    refs: list[dict[str, Any]] = []
    if tool_name in {"Write", "Edit"}:
        path_value = args.get(_ARTIFACT_PATH_ARGUMENT_NAME)
        if isinstance(path_value, str) and path_value:
            refs.append(
                {
                    "path": path_value,
                    "lines": None,
                    "op": "create" if tool_name == "Write" else "edit",
                }
            )
    elif tool_name == "Bash":
        command = args.get("command")
        if isinstance(command, str):
            patch_text = extract_apply_patch_text_from_bash_command(command)
            if patch_text:
                if repo is not None:
                    ops, _ = parse_apply_patch(repo, patch_text, line_number)
                else:
                    ops, _ = parse_apply_patch_operations_without_repo(patch_text, line_number)
                for op in ops:
                    refs.append(
                        {
                            "path": op.get("path"),
                            "lines": apply_patch_hunk_line_range_for_path(
                                patch_text, op.get("path")
                            ),
                            "op": apply_patch_operation_to_artifact_verb(op.get("operation")),
                        }
                    )
    return refs


def _distill_kimi_record(
    record: dict[str, Any],
    *,
    repo: Path | None,
    rollout_filename: str,
    byte_offsets: list[int],
) -> list[TranscriptRecord]:
    """单条 wire record → 0..N 条统一 trajectory record。

    未知 / 不抽取的 record type 返回空列表；调用方按 type 计数，**不抛**——新版本 Kimi
    加 record type 时应当是「可见而不炸」，而不是让整条蒸馏失败。
    """
    line_number = record.get("_line_number") or 0
    raw_ref = {
        "file": rollout_filename,
        "line": line_number,
        "byte_range": _byte_range_for_line(byte_offsets, line_number - 1),
    }
    ts = record.get("time")
    record_type = record.get("type")

    def _rec(**kwargs: Any) -> TranscriptRecord:
        return TranscriptRecord(
            schema_version=SCHEMA_VERSION,
            ts=ts,
            source=KIMI_TRANSCRIPT_SOURCE,
            raw_ref=raw_ref,
            **kwargs,
        )

    if record_type == WIRE_RECORD_TYPE_CONTEXT_APPEND_MESSAGE:
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        text = _text_from_input_parts(message.get("content"))
        if not origin_kind_is_real_user_input(message):
            # harness 写的（injection / system_trigger / skill_activation / background_task
            # 以及未来新增的来源）：落 phase_marker——正文不丢，也不冒充用户发言。
            origin = message.get("origin")
            origin = origin if isinstance(origin, dict) else {}
            origin_kind = _origin_kind(message) or "unknown_origin"
            variant = origin.get("variant")
            return [
                _rec(
                    kind="phase_marker",
                    call_id=None,
                    marker=f"{origin_kind}:{variant}" if variant else origin_kind,
                    summary=_truncate(text or f"<empty {origin_kind}>"),
                    artifact_refs=[],
                )
            ]
        if not text:
            return []
        return [
            _rec(
                kind="message",
                role=message.get("role") or "user",
                call_id=None,
                summary=_truncate(text),
                artifact_refs=[],
            )
        ]

    if record_type != WIRE_RECORD_TYPE_CONTEXT_APPEND_LOOP_EVENT:
        # turn.prompt 刻意不抽：它与 origin.kind=="user" 的 append_message 是同一条输入的
        # 两次记录，都抽会把每条用户输入算两遍。其余 type（config.update / usage.record /
        # llm.* / tools.* / permission.* / swarm_mode.* / full_compaction.* / metadata）
        # 不承载会话内容。
        return []

    event = record.get("event")
    if not isinstance(event, dict):
        return []
    event_type = event.get("type")

    if event_type == LOOP_EVENT_TYPE_CONTENT_PART:
        part = event.get("part")
        if not isinstance(part, dict):
            return []
        part_type = part.get("type")
        field = CONTENT_PART_TEXT_FIELD_BY_PART_TYPE.get(part_type)
        if field is None:
            return []
        body = part.get(field)
        if not isinstance(body, str) or not body.strip():
            return []
        if part_type == CONTENT_PART_TYPE_THINK:
            return [_rec(kind="reasoning", call_id=None, summary=_truncate(body), artifact_refs=[])]
        return [
            _rec(
                kind="message",
                role="assistant",
                call_id=None,
                summary=_truncate(body),
                artifact_refs=[],
            )
        ]

    if event_type == LOOP_EVENT_TYPE_TOOL_CALL:
        tool_name = event.get("name")
        args = event.get("args")
        return [
            _rec(
                kind="tool_call",
                tool=tool_name,
                call_id=event.get("toolCallId"),
                summary=_truncate(
                    event.get("description")
                    or json.dumps(args, ensure_ascii=False, sort_keys=True)
                    if args is not None
                    else str(tool_name)
                ),
                artifact_refs=_kimi_tool_call_artifact_refs(
                    tool_name, args, repo=repo, line_number=line_number
                ),
            )
        ]

    if event_type == LOOP_EVENT_TYPE_TOOL_RESULT:
        result = event.get("result")
        output = result.get("output") if isinstance(result, dict) else None
        return [
            _rec(
                kind="tool_result",
                tool=None,
                call_id=event.get("toolCallId"),
                summary=_truncate(
                    output
                    if isinstance(output, str)
                    else json.dumps(result, ensure_ascii=False, sort_keys=True)
                    if result is not None
                    else "<empty result>"
                ),
                artifact_refs=[],
            )
        ]

    return []


def distill_kimi_wire_jsonl(
    *,
    rollout_path: Path,
    rollout_filename: str,
    repo: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Kimi wire JSONL → 统一 trajectory schema。与另两家 distiller 同签名同返回类型。

    ``index`` 除常规字段外多一个 **``unknown_record_type_counts``**：本模块只抽取会话内容
    相关的 record，其余（含未来新增的 type）逐类计数而**不抛**。新版本 Kimi 加 record type
    时的正确行为是「可见而不炸」——静默丢弃会让蒸馏悄悄变得不完整，抛异常又会让整条链路
    因为一个无关紧要的新事件而断掉。
    """
    records, offsets = _read_jsonl_with_offsets(rollout_path)
    distilled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unrecognized: dict[str, int] = {}
    for record in records:
        outs = _distill_kimi_record(
            record, repo=repo, rollout_filename=rollout_filename, byte_offsets=offsets
        )
        if not outs:
            record_type = record.get("type")
            key = record_type if isinstance(record_type, str) else "<non-string type>"
            unrecognized[key] = unrecognized.get(key, 0) + 1
        for out in outs:
            kind = out.kind or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
            distilled.append(out.to_dict())
    index = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": rollout_filename,
        "record_count": len(distilled),
        "kind_counts": counts,
        "unknown_record_type_counts": unrecognized,
    }
    return distilled, index
