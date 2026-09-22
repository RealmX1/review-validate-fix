#!/usr/bin/env python3
"""把父会话 transcript naive 抽取 + 轻压缩成可读对话 context blob，注入 child agent。

背景
----
RVF Stop hook 把 review 任务派给 cline-kanban 的独立 worktree child agent。
cline-kanban 路径**永远是全新 child 会话**（不像 legacy GUI 路径会同 harness
fork），所以无论同/异 harness，child 都看不到父会话的对话/推理内容——它只拿到
diff/scope/ownership 元数据 + transcript 路径指针。本模块在 dispatch freeze 期把
父会话对话原文抽出来、轻压缩成一份可读 blob，写进 run artifacts，让 child 在
review 前能读到父会话背景。

边界
----
- **这不恢复 prompt cache**（cache 仅同 harness fork 才有），只补对话可见性。
- **Codex 的 reasoning 是加密的**（``encrypted_content``），任何方案都拿不到，
  统一标 ``<encrypted reasoning>``。Claude transcript 的 ``thinking`` 是明文，
  保留全文。
- 输出仅作 review 背景，**不得用它重定义 scope**——scope 仍以
  ``$RVF_SCOPE_CONTRACT`` 为准（由 task prompt / artifact 头部说明强调）。

压缩规则（已用 demo 验证：体积 ≈ 原文 33%、保真度高于 distill）
----------------------------------------------------------------
- **丢弃**：``event_msg/token_count``（计费噪声）、``turn_context``、以及
  system/developer boilerplate（如 permissions 前导）。
- **保留全文**：user/assistant message、Claude 明文 reasoning（thinking）。
- **tool_call**：保留 ``name`` + 参数（参数上限 ~800B，超出截断标注）。
- **tool_result / exec 输出**：压到 ~400B/条 + ``…[+NB tool 输出已压缩]`` 标注。
- **Codex reasoning**：加密 → 标 ``<encrypted reasoning>``。
- 输出为可读文本 blob（每条一行 ``role: 内容`` / ``call X(...)`` / ``result: ...``）。

总字节上限
----------
``render_parent_context`` 接受 ``max_bytes``；超限时**优先保留最近的内容**
（从尾部往前累加），并在头部插入 ``[已截断 …]`` 标注。

Host-aware
----------
复用 ``core.host_adapter.host_transcript_format_detection.detect_transcript_format`` 判定父 transcript 格式：
``HOST_CODEX`` 走 Codex rollout schema，``HOST_CLAUDE`` 走 Claude transcript
schema，``HOST_KIMI_CODE`` 走 Kimi 的 per-agent wire schema；探测失败（空文件/
异常 schema）fallback 到 Codex 解析，与既有 Codex-only 用例保持一致。

⚠️ **漏接一个 host 在这里是静默的**：Codex 的渲染器读别家的 record 只会逐条返回 ``None``，
于是那个 host 的父会话 context blob 变成空白——没有异常、没有日志，child agent 只是拿不到
背景。所以渲染器分派集中在 ``_host_record_renderer``，并由结构性 gate 逐 host 断言。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import

from core.transcript.codex_rollout_item_envelope import (  # noqa: E402
    agent_message_text_from_item,
    codex_item_of_type,
)
from core.host_adapter.host_transcript_format_detection import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_KIMI_CODE,
    detect_transcript_format,
)

# ---------------------------------------------------------------------------
# 压缩规则常量（集中、注释清楚，便于调参）
# ---------------------------------------------------------------------------

#: tool_call 参数渲染上限（字节，UTF-8）。超出截断并标注 ``…[+NB]``。
TOOL_ARGS_LIMIT_BYTES = 800
#: tool_result / exec 输出渲染上限（字节，UTF-8）。超出截断并标注。
TOOL_RESULT_LIMIT_BYTES = 400
#: 默认总字节预算（与 codex_stop hook 的 RVF_PARENT_CONTEXT_MAX_BYTES 默认对齐）。
DEFAULT_MAX_BYTES = 64 * 1024

#: Codex ``event_msg`` 中视为纯噪声、整条丢弃的 subtype。
_CODEX_DROP_EVENT_SUBTYPES = frozenset({"token_count"})
#: 渲染 message 时丢弃的 role（system/developer boilerplate，如 permissions 前导）。
_BOILERPLATE_ROLES = frozenset({"system", "developer"})

#: 截断标注后缀模板。
_TRUNCATE_ARGS_SUFFIX = " …[+{n}B]"
_TRUNCATE_RESULT_SUFFIX = " …[+{n}B tool 输出已压缩]"


def _truncate_bytes(text: str, limit: int, suffix_template: str) -> str:
    """按 UTF-8 字节上限截断 ``text``，超限时附加 ``suffix_template.format(n=超出字节数)``。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    head = encoded[:limit].decode("utf-8", errors="ignore")
    return head + suffix_template.format(n=len(encoded) - limit)


def _collect_texts(node: Any) -> list[str]:
    """递归收集 dict/list 里的 text 字段（codex message/reasoning content 通用）。"""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "message", "summary") and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_collect_texts(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_collect_texts(value))
    return out


# ---------------------------------------------------------------------------
# Codex rollout schema 渲染
# ---------------------------------------------------------------------------


def _codex_agent_message_text_superseded_by_adjacent_response_item_twin(
    agent_message_text: str, next_record: dict[str, Any] | None
) -> bool:
    """信封 ``AgentMessage`` 的正文是否已由**紧随其后**的 ``response_item`` 孪生承载。

    本机 1955 个 rollout 的逐条普查（2026-09-23）给出的结构规律，是这里敢用「只看下一条
    记录」这种窄判据的全部依据：

    * 63921 条 ``AgentMessage`` 信封里，59167 条有 ``response_item/message role=assistant``
      孪生，其中 **58856 条孪生就在下一条记录**、58856+310 条与信封逐字相等或以信封正文
      **为前缀**；跨距 >1 的孪生全语料只有 1 条（且方向相反，见下）。
    * 310 条孪生比信封正文**更长**——``response_item`` 那份额外带着被 UI 剥掉的
      ``<proposed_plan>`` 等正文。所以孪生判据必须是「孪生以信封正文为前缀」而不是
      「逐字相等」：用相等判据，这 310 条最终回答会各渲染两遍。
    * 反过来「看上一条记录」不能用：24 条信封的上一条恰好是**另一条**助手
      ``response_item``（连续两条助手消息），按上一条去重会把这 24 条真内容误删。
      唯一一条孪生在前的记录（1/63921）因此是已知残留，宁可多渲染一条也不误删 24 条——
      重复只是让 reviewer 多读一行，误删是静默丢内容，正是本函数要根治的那类失败。

    全语料回归（用本文件的 ``render_lines`` 逐个渲染 1955 个 rollout）：assistant 行
    59338 → 64069，净增 4731 恰等于无孪生的条数；**零内容丢失**；新增重复 1 条，即上述
    已知残留。
    """
    if not isinstance(next_record, dict) or next_record.get("type") != "response_item":
        return False
    payload = next_record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return False
    if payload.get("role") != "assistant":
        return False
    twin_text = " ".join(_collect_texts(payload.get("content"))).strip()
    return twin_text.startswith(agent_message_text)


def _render_codex_record_to_single_line(
    record: dict[str, Any], next_record: dict[str, Any] | None = None
) -> str | None:
    """把一条 raw Codex rollout 记录压成一行可读 context；返回 None=丢弃。

    ``next_record`` 只用于信封 ``AgentMessage`` 的孪生去重（见
    ``_codex_agent_message_text_superseded_by_adjacent_response_item_twin``）；其余分支
    与记录一一对应，不看邻居。
    """
    rtype = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    sub = payload.get("type")

    if rtype == "session_meta":
        return f"[session start cwd={payload.get('cwd')}]"
    if rtype == "turn_context":
        return None
    if rtype == "event_msg":
        if sub in _CODEX_DROP_EVENT_SUBTYPES:
            return None
        if sub == "agent_message":
            message = payload.get("message")
            return f"assistant: {message}" if isinstance(message, str) and message.strip() else None
        if sub == "user_message":
            message = payload.get("message")
            return f"user: {message}" if isinstance(message, str) and message.strip() else None
        if sub in ("task_started", "task_complete", "turn_aborted", "thread_rolled_back"):
            return f"[{sub}]"
        if sub == "patch_apply_end":
            return f"[patch_apply success={payload.get('success')}]"
        # ``item_completed`` 信封把 patch_apply_end 搬进了 ``FileChange`` item，该编辑成功
        # 标记在信封形态下会整条消失。补回后与 ``patch_apply_end`` 形态严格 parity：两种
        # 形态都是「exec 调用渲染出补丁内容 + 一行成功标记」，不产生重复。
        file_change_item = codex_item_of_type(payload, "FileChange")
        if file_change_item is not None:
            return f"[patch_apply success={file_change_item.get('status') == 'completed'}]"
        # ``AgentMessage`` 曾按「``response_item`` 孪生已逐条渲染，补了就是重复」被刻意
        # 跳过。该论证在单个样本上成立，在全语料上不成立：1955 个 rollout 里 4731 条
        # ``AgentMessage`` **没有任何**孪生（全文件比对，也不与 reasoning summary 重合），
        # 其中绝大多数是 ``phase == "commentary"`` 的助手过程说明（「我先去看 X，再改 Y」）。
        # 它们整条被丢弃，reviewer 因此看不到父会话的意图与自述。这不是历史遗留：仅
        # 2026-07 一个月就有 3969/14577 条（27%）是这种 envelope-only 形态。
        #
        # 所以改成**按孪生存在与否逐条判定**，而不是按 item 类型无条件忽略整类——两个
        # 不变量同时守住：有孪生不重复渲染，无孪生不丢内容。
        #
        # 其余三类维持现状，依据各不相同、不可一概而论：``UserMessage`` 实测 5484/5486
        # 有 ``response_item`` 孪生覆盖，跳过成立；``Reasoning`` / ``CommandExecution``
        # 未按本次口径全语料复核过，此处不对它们作任何「已验证」断言。
        agent_message_item = codex_item_of_type(payload, "AgentMessage")
        if agent_message_item is not None:
            text = (agent_message_text_from_item(agent_message_item) or "").strip()
            if not text:
                return None
            if _codex_agent_message_text_superseded_by_adjacent_response_item_twin(
                text, next_record
            ):
                return None
            return f"assistant: {text}"
        return None
    if rtype == "response_item":
        if sub == "message":
            role = payload.get("role") or "?"
            if role in _BOILERPLATE_ROLES:
                return None
            text = " ".join(_collect_texts(payload.get("content"))).strip()
            return f"{role}: {text}" if text else None
        if sub == "reasoning":
            text = " ".join(_collect_texts(payload.get("summary"))).strip()
            if text:
                return f"reasoning: {text}"
            # summary 为空但有 encrypted_content → 加密 reasoning，标注而非泄露 blob。
            if payload.get("encrypted_content"):
                return "reasoning: <encrypted reasoning>"
            return None
        if sub in ("function_call", "custom_tool_call"):
            args = payload.get("arguments") if sub == "function_call" else payload.get("input")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            args = _truncate_bytes(args, TOOL_ARGS_LIMIT_BYTES, _TRUNCATE_ARGS_SUFFIX)
            return f"call {payload.get('name')}({args})"
        if sub in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            if isinstance(output, dict):
                output = output.get("output") or output.get("text") or json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            output = _truncate_bytes(output, TOOL_RESULT_LIMIT_BYTES, _TRUNCATE_RESULT_SUFFIX)
            return f"result: {output}"
    return None


def _render_codex_record(
    record: dict[str, Any], next_record: dict[str, Any] | None = None
) -> list[str]:
    """Codex 渲染器的 ``list[str]`` 外壳——三家渲染器共用一个「0..N 行」契约。

    Codex 的 record 与行是一对一的（历史实现返回 ``str | None``），Claude / Kimi 的一条
    record 可以展开成多行。分派表要能无差别地调用它们，契约就必须统一；单行逻辑原样留在
    ``_render_codex_record_to_single_line`` 里。

    ``next_record`` 是共用契约的第二半：``render_lines`` 对每条记录都带上它的下一条，三家
    渲染器签名一致，只有 Codex 目前真的需要它（信封 ``AgentMessage`` 的孪生去重）。
    """
    line = _render_codex_record_to_single_line(record, next_record)
    return [line] if line is not None else []


# ---------------------------------------------------------------------------
# Claude Code transcript schema 渲染
# ---------------------------------------------------------------------------


def _render_claude_record(
    record: dict[str, Any], next_record: dict[str, Any] | None = None
) -> list[str]:
    """把一条 Claude Code NDJSON 记录压成 0..N 行可读 context。

    一条 ``assistant`` record 可能同时含 ``text`` / ``thinking`` / 多条
    ``tool_use``；一条 ``user`` record 可能含 ``tool_result``，逐 block 展开。

    ``next_record`` 只为对齐三家渲染器的共用签名；Claude transcript 没有「同一条消息写两
    份记录」的形态，这里不需要看邻居。
    """
    rtype = record.get("type")
    if rtype not in ("user", "assistant"):
        # summary / system / file-history-snapshot / permission-mode 等元数据全丢。
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role") or rtype
    content = message.get("content")
    lines: list[str] = []

    # 字符串 content（简单 user 文本）。
    if isinstance(content, str):
        text = content.strip()
        return [f"{role}: {text}"] if text else []

    if not isinstance(content, list):
        return []

    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(f"{role}: {text.strip()}")
        elif bt == "thinking":
            # Claude reasoning 是明文，保留全文。
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                lines.append(f"reasoning: {thinking.strip()}")
        elif bt == "tool_use":
            name = block.get("name") or "<unknown>"
            tool_input = block.get("input")
            args = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
            args = _truncate_bytes(args, TOOL_ARGS_LIMIT_BYTES, _TRUNCATE_ARGS_SUFFIX)
            lines.append(f"call {name}({args})")
        elif bt == "tool_result":
            result_content = block.get("content")
            if isinstance(result_content, list):
                parts: list[str] = []
                for sub in result_content:
                    if isinstance(sub, dict):
                        text = sub.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                result_text = "\n".join(parts)
            elif isinstance(result_content, str):
                result_text = result_content
            else:
                result_text = ""
            result_text = _truncate_bytes(result_text, TOOL_RESULT_LIMIT_BYTES, _TRUNCATE_RESULT_SUFFIX)
            lines.append(f"result: {result_text}")
    return lines


# ---------------------------------------------------------------------------
# Kimi Code CLI wire schema 渲染
# ---------------------------------------------------------------------------


def _render_kimi_record(
    record: dict[str, Any], next_record: dict[str, Any] | None = None
) -> list[str]:
    """把一条 Kimi wire JSONL 记录压成 0..N 行可读 context。

    ``next_record`` 只为对齐三家渲染器的共用签名；Kimi 侧的双份记录（``turn.prompt`` 与
    ``append_message``）已按记录类型正向筛掉，不需要看邻居。

    Kimi 与另两家的**结构性**差异（取证见 ``adapters/kimi/transcript.py``）：助手输出与
    工具调用**都不在** ``context.append_message`` 里，而在 ``context.append_loop_event``
    的 ``event`` 下。只读 ``context.append_message`` 会拿到一份「只有用户在说话」的假 blob。

    两处静默陷阱：

    - ``context.append_message`` 的 ``role`` 实测**全是** ``"user"``，真正区分来源的是
      ``message.origin.kind``。harness 注入的系统提醒（``injection`` 等）在这里丢弃，与
      Codex/Claude 渲染器丢 system/developer boilerplate 同口径——不丢就会把系统提醒当成
      用户发言喂给 child。
    - ``content.part`` 里 ``think`` 的正文在 ``part.think``，**不在** ``part.text``；按
      ``part["text"]`` 一把抓会让所有推理内容静默变成空串。
    """
    rtype = record.get("type")

    if rtype == "context.append_message":
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        origin = message.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "user":
            # 正向判据：只有 origin.kind == "user" 才是真实用户输入。负向判据（「不等于
            # injection」）挡不住后续新增的来源种类，那些会默认落进「用户发言」。
            return []
        text = _kimi_part_text(message.get("content"))
        role = message.get("role") or "user"
        return [f"{role}: {text}"] if text else []

    if rtype != "context.append_loop_event":
        # metadata / turn.prompt / llm.* / usage.* / config.* / tools.* / permission.* 等
        # 不承载会话内容。turn.prompt 与 origin.kind=="user" 的 append_message 是同一条输入
        # 的两次记录，只认后者，避免每条用户输入渲染两遍。
        return []

    event = record.get("event")
    if not isinstance(event, dict):
        return []
    event_type = event.get("type")

    if event_type == "content.part":
        part = event.get("part")
        if not isinstance(part, dict):
            return []
        part_type = part.get("type")
        field = _KIMI_CONTENT_PART_TEXT_FIELD_BY_PART_TYPE.get(part_type)
        if field is None:
            return []
        body = part.get(field)
        if not isinstance(body, str) or not body.strip():
            return []
        if part_type == "think":
            # Kimi 的 reasoning 与 Claude 的 thinking 一样是明文，保留全文。
            return [f"reasoning: {body.strip()}"]
        return [f"assistant: {body.strip()}"]

    if event_type == "tool.call":
        args = event.get("args")
        rendered_args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        rendered_args = _truncate_bytes(
            rendered_args, TOOL_ARGS_LIMIT_BYTES, _TRUNCATE_ARGS_SUFFIX
        )
        return [f"call {event.get('name') or '<unknown>'}({rendered_args})"]

    if event_type == "tool.result":
        result = event.get("result")
        output = result.get("output") if isinstance(result, dict) else None
        if not isinstance(output, str):
            output = "" if result is None else json.dumps(result, ensure_ascii=False)
        return [f"result: {_truncate_bytes(output, TOOL_RESULT_LIMIT_BYTES, _TRUNCATE_RESULT_SUFFIX)}"]

    return []


#: ``content.part`` / ``turn.prompt.input`` 的正文字段名**随 part 类型而变**：
#: ``{"type":"think","think":"…"}`` 的正文在 ``think`` 键下而不是 ``text`` 键下。
_KIMI_CONTENT_PART_TEXT_FIELD_BY_PART_TYPE = {"text": "text", "think": "think"}


def _kimi_part_text(parts: Any) -> str | None:
    """把 part 列表（**不是**字符串）折叠成一段文本。"""
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
        field = _KIMI_CONTENT_PART_TEXT_FIELD_BY_PART_TYPE.get(part.get("type"), "text")
        value = part.get(field)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    joined = "\n".join(chunks).strip()
    return joined or None


# ---------------------------------------------------------------------------
# 总装配
# ---------------------------------------------------------------------------


def _host_record_renderer(host: str | None):
    """按 host 选 record 渲染器；每个渲染器签名为 ``(record, next_record)``、返回
    ``list[str]``（0..N 行）。

    未知 host 兜底 Codex，与 ``detect_transcript_format`` 的「无证据回 None、调用方兜底
    Codex」约定一致。**漏接一个 host 不会抛**，只会让它的 context blob 静默变空，故由
    结构性 gate 逐 host 断言这里各有其人。
    """
    if host == HOST_CLAUDE:
        return _render_claude_record
    if host == HOST_KIMI_CODE:
        return _render_kimi_record
    return _render_codex_record


def _iter_records(transcript_path: Path) -> list[dict[str, Any]]:
    """逐行读 JSONL，跳过空行/解析失败行，返回 dict record 列表。"""
    records: list[dict[str, Any]] = []
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def render_lines(transcript_path: Path, host: str | None = None) -> list[str]:
    """把父 transcript 渲染成 context 行列表（不做总字节裁剪）。

    ``host`` 缺省时用 ``detect_transcript_format`` 探测；探测失败 fallback
    到 Codex 解析，与既有 Codex-only 用例一致。
    """
    if host is None:
        host = detect_transcript_format(transcript_path) or HOST_CODEX
    renderer = _host_record_renderer(host)
    lines: list[str] = []
    records = _iter_records(transcript_path)
    for index, record in enumerate(records):
        # 每条记录都带上它的下一条：Codex 的信封 ``AgentMessage`` 要靠它判断孪生是否存在，
        # 从而在「无孪生就不丢内容」与「有孪生就不重复渲染」之间逐条取舍。
        next_record = records[index + 1] if index + 1 < len(records) else None
        lines.extend(renderer(record, next_record))
    return lines


def render_parent_context(
    parent_thread_path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str | None:
    """渲染父会话对话 context blob；父 transcript 缺失/空 → 返回 None（fail-open）。

    超出 ``max_bytes`` 时**优先保留最近内容**（从尾部往前累加行），并在头部插入
    ``[已截断 …]`` 标注。返回的字符串总字节（UTF-8）不超过 ``max_bytes``（截断
    标注本身计入预算）。
    """
    if parent_thread_path is None or not Path(parent_thread_path).exists():
        return None
    path = Path(parent_thread_path)
    lines = render_lines(path)
    if not lines:
        return None

    full = "\n".join(lines)
    if len(full.encode("utf-8")) <= max_bytes:
        return full

    # 超预算：从尾部往前累加，保留最近内容。
    # notice 文案分两种：丢掉了较早整行（dropped>0）vs 未丢整行、仅把单条超大
    # 记录的内容截断（dropped==0）。两种文案都按最坏情况长度预留预算，确保选用
    # 任一文案后总字节都不超 max_bytes。
    notice_dropped = "[已截断 {dropped} 行较早 context，仅保留最近内容；完整 transcript：{path}]"
    notice_single = "[已截断单条超大 context 内容以适配字节预算；完整 transcript：{path}]"
    notice_dropped_sample = notice_dropped.format(dropped=len(lines), path=str(path))
    notice_single_sample = notice_single.format(path=str(path))
    notice_reserve = max(
        len(notice_dropped_sample.encode("utf-8")),
        len(notice_single_sample.encode("utf-8")),
    )
    budget = max_bytes - notice_reserve - 1  # -1 for newline
    if budget < 0:
        budget = 0

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline join
        if used + line_bytes > budget and kept:
            break
        kept.append(line)
        used += line_bytes
    kept.reverse()
    dropped = len(lines) - len(kept)

    # ``and kept`` 短路会无条件保留最近的第一条记录；若该条本身就 > budget（如单条
    # >max_bytes 的 message），整体仍会超预算。对最旧的（边界/首）保留行按剩余预算
    # 做单条字节截断，恢复「返回 UTF-8 字节 <= max_bytes」不变量。
    truncated_single = False
    if kept and used > budget:
        first = kept[0]
        first_bytes = len(first.encode("utf-8"))
        # 整体溢出全部来自 kept[0] 被整条保留；其余行已在预算内累加。
        rest_bytes = used - (first_bytes + 1)
        allowed_first = budget - rest_bytes - 1  # 该首行可用字节（-1 为其换行）
        if allowed_first < 0:
            allowed_first = 0
        # ``_truncate_bytes`` 的 suffix 不计入它的 limit，需额外按最坏情况预留 suffix
        # 字节（``n`` 最大为该行全长），否则截断结果会再次超出 allowed_first。
        suffix_reserve = len(
            _TRUNCATE_RESULT_SUFFIX.format(n=first_bytes).encode("utf-8")
        )
        head_limit = allowed_first - suffix_reserve
        if head_limit < 0:
            head_limit = 0
        kept[0] = _truncate_bytes(first, head_limit, _TRUNCATE_RESULT_SUFFIX)
        truncated_single = True

    if dropped > 0:
        notice = notice_dropped.format(dropped=dropped, path=str(path))
    elif truncated_single:
        notice = notice_single.format(path=str(path))
    else:
        notice = notice_dropped.format(dropped=dropped, path=str(path))
    blob = "\n".join([notice, *kept])
    # 兜底：极端小 max_bytes（连 notice+suffix 都放不下）时硬截到字节上限，
    # 无条件守住「返回 UTF-8 字节 <= max_bytes」不变量。
    encoded = blob.encode("utf-8")
    if len(encoded) > max_bytes:
        blob = encoded[: max(max_bytes, 0)].decode("utf-8", errors="ignore")
    return blob


def main(argv: list[str] | None = None) -> int:
    """CLI: ``rvf_parent_context.py <transcript_path> [max_bytes]`` → stdout blob。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: rvf_parent_context.py <transcript_path> [max_bytes]", file=sys.stderr)
        return 2
    path = Path(args[0]).expanduser()
    max_bytes = int(args[1]) if len(args) > 1 else DEFAULT_MAX_BYTES
    blob = render_parent_context(path, max_bytes=max_bytes)
    if blob is None:
        print("(no parent conversation context)", file=sys.stderr)
        return 1
    print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
