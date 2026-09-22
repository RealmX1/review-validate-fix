"""Codex rollout ``item_completed`` 信封的解包原语（host-中性、stdlib-only）。

背景：三代 rollout schema 当前同时在跑
------------------------------------------------
Codex 的 rollout JSONL 至今出现过三代记录形态，且**同时在跑**（Codex 支持 resume /
切换模型 / 不同前端，同一天不同会话可能是不同代，同一个会话内也可能前后段不同代）：

* 第一代（~2026-05 前）：``event_msg`` 的 ``exec_command_end``，工具走
  ``function_call`` name=``exec_command``。
* 第二代（gpt-5.6-sol，~2026-07）：工具统一成 ``custom_tool_call`` name=``exec``、
  实际调用以 JS 文本内嵌在 ``input``；编辑真相源是 ``event_msg`` 的 ``patch_apply_end``。
* 第三代（2026-08，``codex-tui`` 0.147.0）：原先散在 ``event_msg`` 的若干子类型被搬进
  统一的 ``event_msg`` / ``payload.type = "item_completed"`` 信封，真正的内容在
  ``payload.item``，由 ``item.type`` 区分（``FileChange`` / ``UserMessage`` /
  ``AgentMessage`` / ``CommandExecution`` / …）。``response_item`` 侧未变。

⚠️ 世代判定只能看 payload 结构，**绝不能看 ``cli_version`` / ``originator``**
-------------------------------------------------------------------------------
全库 1699 个 rollout 的普查实证：同一个 ``cli_version`` 会走不同 schema ——
``Codex Desktop 0.147.0-alpha.6.5`` 仍发 ``patch_apply_end``、不发新 item 类型，而
``codex-tui 0.147.0`` 相反。另外 ``item_completed`` 信封自 2026-04-25 就存在，但当时
**只承载 ``item.type == "Plan"``**；真正新增的是 ``CommandExecution`` / ``FileChange`` /
``AgentMessage`` / ``UserMessage`` 这几类。因此本模块的每个函数都按 **payload / item
结构** 判定，不引用任何版本号；调用方也必须如此。

刻意不提供「整条 record 的宽归一化」
--------------------------------------
本模块**故意不导出**「把 ``item_completed`` 记录翻译回旧 ``event_msg`` 子类型」的通用
函数。原因：读用户/助手消息的十余处站点**早已带 ``response_item/message`` 兜底分支**，
新 schema 下仍能取到全部文本。实测同一 transcript 里每个用户回合都成对出现
（``response_item`` 在前、``item_completed`` 紧随），旧 schema 本来就是**双份**
（``event_msg`` 一份 + ``response_item`` 一份）；新 schema 只是把 ``event_msg`` 那份
搬进信封，对这些站点而言是**去重而非失真**。若为了「形式 parity」把 ``UserMessage`` /
``AgentMessage`` 也翻译回去，只会把旧世代的冗余原样搬来（parent-context 重复渲染、
trajectory 重复切分、``user_messages_containing`` 重复计数）。

真正失明、因而由本模块承接的，只有**没有任何兜底**的那几处：编辑归属
（``FileChange``）、trajectory 的 ``artifact_refs``（``FileChange``）、显式 skill 触发
检测（``UserMessage.content[].text_elements``）、以及 exec 命令归属
（``CommandExecution`` 与第二代内嵌 JS）。
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Iterable

CODEX_ITEM_ENVELOPE_PAYLOAD_TYPE = "item_completed"

# 全库 1699 个 rollout 普查得到的 ``item.type`` 全集。用于 ``unrecognized_codex_item_types``
# 的前向兼容告警：出现集合外的新类型时记 warning，让第四代漂移在**降级前**就被看见，
# 而不是像本次一样先炸 fail-loud 守卫再回溯。
KNOWN_CODEX_ITEM_TYPES = frozenset(
    {
        "AgentMessage",
        "CommandExecution",
        "ContextCompaction",
        "Extension",
        "FileChange",
        "Plan",
        "Reasoning",
        "UserMessage",
    }
)

# 第二代（gpt-5.6-sol）把 shell 调用以 JS 文本内嵌在 ``custom_tool_call`` name=``exec``
# 的 input，形态是 ``const r = await tools.exec_command({...}); text(r.output);``。
# 锚定 ``tools.exec_command(`` 调用形态（容忍空白），与 session_change_manifest 里
# ``_EMBEDDED_APPLY_PATCH_CALL_RE`` 同一口径：避免把 ``rg -n exec_command`` /
# ``ALL_TOOLS.filter(x => x.name === "exec_command")`` 之类只读文本误判为一次执行。
EMBEDDED_EXEC_COMMAND_CALL_RE = re.compile(r"tools\s*\.\s*exec_command\s*\(")

_JS_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

# 常见的 shell 包装形态：``["/bin/zsh", "-lc", "<真实命令>"]``。argv[1] 命中这些
# 「下一个参数是命令字符串」的 shell 标志时取 argv[-1] 作为命令文本，否则退回
# shlex.join(argv)（保守：不丢信息，只是多带 shell 前缀）。
_SHELL_COMMAND_STRING_FLAGS = frozenset({"-c", "-lc", "-ic", "-lic", "-cl", "-il"})


def codex_item_from_envelope_payload(payload: Any) -> dict[str, Any] | None:
    """从一条 record 的 ``payload`` 取出 ``item_completed`` 信封里的 ``item``。

    非信封 payload / ``item`` 非 dict / ``item.type`` 非字符串 → ``None``。
    调用方据此以「返回非 None」作为「这是第三代信封记录」的唯一判据。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != CODEX_ITEM_ENVELOPE_PAYLOAD_TYPE:
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        return None
    return item


def codex_item_of_type(payload: Any, item_type: str) -> dict[str, Any] | None:
    """``codex_item_from_envelope_payload`` 的类型过滤版本，省去调用方两行样板。"""
    item = codex_item_from_envelope_payload(payload)
    if item is None or item.get("type") != item_type:
        return None
    return item


def legacy_patch_apply_end_payload_from_file_change_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """把第三代 ``FileChange`` item 映射成第二代 ``patch_apply_end`` payload 形状。

    实测两者的 ``changes`` 字典**形状完全一致**
    （``{abs_path: {type, unified_diff | content, move_path}}``），差异只在信封字段：

    ======================  ==========================
    ``patch_apply_end``     ``FileChange``
    ======================  ==========================
    ``success: true``       ``status: "completed"``
    ``call_id``             ``id``
    ``changes``             ``changes``（同形）
    ======================  ==========================

    因此这里只做信封层映射，下游 ``synthesize_apply_patch_text_from_patch_apply_end``
    /``parse_apply_patch_details`` 一行不用改——两代 schema 复用同一套解析，避免各写
    一份再次一起过时。

    ``status`` 非 ``"completed"``（用户中断 / 失败）→ ``success: False``，下游合成函数
    会返回 ``None``，该条不产生归属，与第二代失败 ``patch_apply_end`` 行为一致。
    """
    if not isinstance(item, dict) or item.get("type") != "FileChange":
        return None
    changes = item.get("changes")
    if not isinstance(changes, dict):
        return None
    payload: dict[str, Any] = {
        "type": "patch_apply_end",
        "success": item.get("status") == "completed",
        "changes": changes,
    }
    call_id = item.get("id")
    if isinstance(call_id, str) and call_id:
        payload["call_id"] = call_id
    return payload


def _item_content_text_parts(item: dict[str, Any]) -> list[str]:
    """按「有没有 ``text`` 字符串字段」取文本，**不按 ``type`` 判定**。

    实测 ``UserMessage`` 的块是 ``{"type": "text", ...}``（小写）而 ``AgentMessage``
    是 ``{"type": "Text", ...}``（大写）——按 type 判定必然漏一边。
    """
    content = item.get("content")
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return parts


def user_message_text_and_elements_from_user_message_item(
    item: dict[str, Any],
) -> tuple[str | None, list[dict[str, Any]]]:
    """从 ``UserMessage`` item 取回 ``(消息文本, text_elements)``。

    第二代把这两项放在 payload 顶层（``message`` 是裸字符串、``text_elements`` 平铺），
    第三代搬进了 ``item.content[]``：文本在 ``content[].text``，而承载显式 ``$skill``
    调用标记的 ``text_elements`` 挂在**每个 content 块上**，此处按块顺序展平。

    返回的形状刻意与第二代 payload 对齐（``message`` + ``text_elements``），使
    ``codex_invoked_skill`` 这类只认旧形状的消费者能原样复用其匹配逻辑。
    """
    if not isinstance(item, dict) or item.get("type") != "UserMessage":
        return None, []
    parts = _item_content_text_parts(item)
    text = "\n".join(parts) if parts else None
    elements: list[dict[str, Any]] = []
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_elements = block.get("text_elements")
            if isinstance(block_elements, list):
                elements.extend(element for element in block_elements if isinstance(element, dict))
    return text, elements


def agent_message_text_from_item(item: dict[str, Any]) -> str | None:
    """从 ``AgentMessage`` item 取回助手文本。与上者同一取文本口径。"""
    if not isinstance(item, dict) or item.get("type") != "AgentMessage":
        return None
    parts = _item_content_text_parts(item)
    return "\n".join(parts) if parts else None


def _strip_file_uri(value: str) -> str:
    """第三代的 ``cwd`` 是 ``file://`` URI；下游 ``command_workdir`` 只认普通路径。"""
    if value.startswith("file://"):
        return value[len("file://") :] or value
    return value


def shell_command_and_workdir_from_command_execution_item(
    item: dict[str, Any],
) -> tuple[str, str | None] | None:
    """从 ``CommandExecution`` item 还原 ``(命令文本, workdir)``，形状与第一代
    ``exec_command`` 的 ``{"cmd": ..., "workdir": ...}`` 参数对齐。

    ``item.command`` 是 argv 数组，实测普遍是 ``["/bin/zsh", "-lc", "<真实命令>"]``：
    argv[1] 命中 shell 的「命令字符串」标志时取 argv[-1]，否则 ``shlex.join`` 整个
    argv（保守但不丢信息）。``item.cwd`` 剥掉 ``file://`` 前缀。

    命令为空 / ``command`` 非列表 → ``None``（不猜）。
    """
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return None
    argv = item.get("command")
    if not isinstance(argv, list):
        return None
    tokens = [token for token in argv if isinstance(token, str)]
    if not tokens:
        return None
    if len(tokens) >= 3 and tokens[1] in _SHELL_COMMAND_STRING_FLAGS:
        command = tokens[-1]
    elif len(tokens) == 1:
        command = tokens[0]
    else:
        command = shlex.join(tokens)
    if not command.strip():
        return None
    cwd = item.get("cwd")
    workdir = _strip_file_uri(cwd) if isinstance(cwd, str) and cwd else None
    return command, workdir


def _read_js_string_literal(text: str, index: int) -> tuple[str | None, int]:
    """读一个 JS 字符串字面量，返回 ``(解码后的值, 结束位置)``。

    带 ``${}`` 插值的模板字面量返回 ``(None, 越过该字面量的位置)``——值不是静态字面量，
    按「抽不到就放弃、不猜」处理。
    """
    quote = text[index]
    if quote not in "\"'`":
        return None, index
    cursor = index + 1
    pieces: list[str] = []
    interpolated = False
    escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0"}
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            nxt = text[cursor + 1] if cursor + 1 < len(text) else ""
            pieces.append(escapes.get(nxt, nxt))
            cursor += 2
            continue
        if char == quote:
            return (None if interpolated else "".join(pieces)), cursor + 1
        if quote == "`" and char == "$" and text[cursor + 1 : cursor + 2] == "{":
            interpolated = True
        pieces.append(char)
        cursor += 1
    return None, len(text)


def _skip_js_value(text: str, index: int) -> int:
    """跳过一个 JS 值（字符串 / 对象 / 数组 / 其它裸 token），返回其后位置。"""
    char = text[index]
    if char in "\"'`":
        _value, end = _read_js_string_literal(text, index)
        return end
    if char in "{[":
        depth = 0
        cursor = index
        while cursor < len(text):
            current = text[cursor]
            if current in "\"'`":
                _value, cursor = _read_js_string_literal(text, cursor)
                continue
            if current in "{[":
                depth += 1
            elif current in "}]":
                depth -= 1
                if depth == 0:
                    return cursor + 1
            cursor += 1
        return len(text)
    cursor = index
    while cursor < len(text) and text[cursor] not in ",}":
        cursor += 1
    return cursor


def js_object_literal_string_fields(
    text: str, start: int, keys: Iterable[str]
) -> dict[str, str]:
    """从 ``text[start:]`` 处的 JS 对象字面量抽出 ``keys`` 里那些**字符串字面量**字段。

    实测这些内嵌实参**只有约 17% 是严格 JSON**（其余用无引号 key，如
    ``{cmd:"git status", "workdir":"/x"}``），所以 ``json.loads`` 恢复率为 0——这正是
    第二代 exec 归属全盘失效的第二个死因。此处按 JS 对象字面量的语法逐项扫描（引号 key
    与裸标识符 key 同等对待，值按类型跳过），只取需要的字符串字段，不做完整 JS 求值。

    非字符串字面量的值（表达式、变量、带插值的模板串）一律跳过而非猜测。
    """
    wanted = set(keys)
    cursor = start
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor >= len(text) or text[cursor] != "{":
        return {}
    cursor += 1
    fields: dict[str, str] = {}
    while cursor < len(text):
        while cursor < len(text) and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor >= len(text) or text[cursor] == "}":
            break
        if text[cursor] in "\"'":
            key, cursor = _read_js_string_literal(text, cursor)
            if key is None:
                return fields
        else:
            match = _JS_IDENTIFIER_RE.match(text, cursor)
            if match is None:
                return fields
            key = match.group(0)
            cursor = match.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] != ":":
            return fields
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        if key in wanted and text[cursor] in "\"'`":
            value, _end = _read_js_string_literal(text, cursor)
            if value is not None:
                fields[key] = value
        cursor = _skip_js_value(text, cursor)
    return fields


def embedded_exec_command_invocation_from_tool_input(
    tool_input: Any,
) -> tuple[str, str | None] | None:
    """从第二代 ``exec`` 工具 input 的 JS 文本里还原 ``(命令文本, workdir)``。

    只认 ``tools.exec_command(`` 这一调用形态（见 ``EMBEDDED_EXEC_COMMAND_CALL_RE``
    的说明），抽不到 ``cmd`` 字符串字面量就返回 ``None``——保持「不猜」的既有行为。
    """
    if not isinstance(tool_input, str) or not tool_input:
        return None
    match = EMBEDDED_EXEC_COMMAND_CALL_RE.search(tool_input)
    if match is None:
        return None
    fields = js_object_literal_string_fields(tool_input, match.end(), ("cmd", "workdir"))
    command = fields.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return None
    workdir = fields.get("workdir")
    return command, (workdir if isinstance(workdir, str) and workdir else None)


def unrecognized_codex_item_types(records: Iterable[dict[str, Any]]) -> list[str]:
    """扫出 ``KNOWN_CODEX_ITEM_TYPES`` 之外的 ``item.type``（去重、排序）。

    前向兼容信号：第四代漂移新增 item 类型时，让 manifest 的 ``warnings`` 先说话，
    而不是等到归属归零、fail-loud 守卫整体 skip 才被发现。
    """
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        item = codex_item_from_envelope_payload(record.get("payload"))
        if item is None:
            continue
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type not in KNOWN_CODEX_ITEM_TYPES:
            seen.add(item_type)
    return sorted(seen)
