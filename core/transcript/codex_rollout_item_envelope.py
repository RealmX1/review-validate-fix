"""Codex rollout ``item_completed`` 信封的解包原语（host-中性、stdlib-only）。

背景：多种 rollout 记录形态当前同时在跑
------------------------------------------------
Codex 的 rollout JSONL 至今出现过几种并存的记录形态，**它们不是按时间先后替换的「世代」，
而是按前端 / 工具配置并存**（Codex 支持 resume、切换模型、不同前端，同一天不同会话可能不同
形态，同一个会话内也可能前后段不同）：

* ``function_call`` name=``exec_command``：命令以 JSON 实参投递，配套
  ``event_msg`` 的 ``exec_command_end``（该事件 2026-05-09 起改走
  ``response_item.function_call_output``）。
* ``custom_tool_call`` name=``exec``：命令与 ``apply_patch`` 都以 **JS 文本**内嵌在
  ``input``（``const r = await tools.exec_command({...})``）。
* ``event_msg`` / ``payload.type = "item_completed"`` 信封：内容在 ``payload.item``、
  由 ``item.type`` 区分（``FileChange`` / ``CommandExecution`` / ``UserMessage`` /
  ``AgentMessage`` / …）。``response_item`` 侧不受影响。

⚠️ 形态判定只能看 payload 结构，**绝不能看 ``cli_version`` / 日期 / ``originator``**
-------------------------------------------------------------------------------
本机语料 1955 个 rollout 的实测（2026-09-23 复查，**推翻了此前基于较小语料的几个判断**）：

* ``item_completed`` 信封承载 ``FileChange`` / ``CommandExecution`` / ``UserMessage`` /
  ``AgentMessage`` **最早见于 2026-04-02**（``codex-tui 0.118.0``），并非某次版本升级新增；
  含 ``FileChange`` 的文件有 705 个。按日期或版本号判形态必然误判。
* ``patch_apply_end`` 在这 1955 个文件里**从未作为 ``payload.type`` 出现过**。裸 grep 命中的
  102 个文件全部是该字面量出现在工具输出/消息正文里（agent 自己读 RVF 源码读进去的）。
  legacy 分支因此在本机语料上是死路径，保留只为兼容其它 host / 更早存档。
* 500 个文件**同时**含 ``apply_patch`` 工具调用与 ``FileChange`` item（1:1 对应同一次编辑）。
  这正是既有「路径级去重」存在的理由：两侧都摄入会把同一次编辑登记两次。

刻意不提供「整条 record 的宽归一化」
--------------------------------------
本模块**故意不导出**「把 ``item_completed`` 记录翻译回旧 ``event_msg`` 子类型」的通用
函数。原因：读用户消息的十余处站点**早已带 ``response_item/message`` 兜底分支**，新
schema 下仍能取到全部文本。实测同一 transcript 里每个用户回合都成对出现（``response_item``
与 ``item_completed`` 相邻），旧 schema 本来就是**双份**（``event_msg`` 一份 +
``response_item`` 一份）；新 schema 只是把 ``event_msg`` 那份搬进信封，对这些站点而言是
**去重而非失真**。若为了「形式 parity」把 ``UserMessage`` 无条件翻译回去，只会把另一形态
里的冗余原样搬来（trajectory 重复切分、``user_messages_containing`` 重复计数）。

⚠️ **「有兜底」是逐类结论，不能推广到 ``AgentMessage``**
--------------------------------------------------------
1955 个 rollout 的逐条比对（2026-09-23）：``UserMessage`` 确有孪生兜底（5484/5486），但
``AgentMessage`` 的 63921 条里有 **4731 条完全没有 ``response_item`` 孪生**（同文件全量比
对，也不与 reasoning summary 重合），全是助手的过程说明（``phase == "commentary"``）。
更要命的是另一端：310 条孪生比信封正文**更长**（``response_item`` 那份还带着被 UI 剥掉的
``<proposed_plan>`` 等正文），所以「按正文逐字相等去重」两头都错——会漏掉 4731 条、又把
这 310 条渲染两遍。

结论是**去重必须逐条判定孪生是否存在，而不是按 item 类型一刀切**。
``rvf_parent_context`` 已按这个口径处理 ``AgentMessage``（孪生规律与判据取证写在
``_codex_agent_message_text_superseded_by_adjacent_response_item_twin``）。其余消费者若要
读助手消息，同样不能假设「``response_item`` 侧一定有」。

真正失明、因而由本模块承接的，只有**没有任何兜底**的那几处：编辑归属
（``FileChange``）、trajectory 的 ``artifact_refs``（``FileChange``）、显式 skill 触发
检测（``UserMessage.content[].text_elements``）、以及 exec 命令归属
（``CommandExecution`` 与 ``exec`` 内嵌 JS）。
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

CODEX_ITEM_ENVELOPE_PAYLOAD_TYPE = "item_completed"

# 本机语料 1955 个 rollout 普查（2026-09-23）实测出现过的全部 ``item.type``。
# ``unrecognized_codex_item_types`` 据此做前向兼容告警：**语义是「从未见过的新类型」**，
# 所以凡是已普查过的类型都必须列在这里（哪怕刻意不做归属），否则正常会话就会刷警告、
# 真正的新类型反而被淹没。
#
# 参与归属的：FileChange（编辑真相源）、CommandExecution（命令归属）、
# UserMessage（显式 $skill 触发检测）。
# 已普查但**刻意不归属**的：Reasoning / AgentMessage / Plan / ContextCompaction / Extension
# （纯文本或标记）、WebSearch / ImageView（只读）、McpToolCall / DynamicToolCall
# （带 namespace 的插件工具，无结构化 diff 或 argv，归属只能靠猜）、
# SubAgentActivity / CollabAgentToolCall / FunctionCallOutput / HookPrompt（代理间消息与指针）、
# EnteredReviewMode / ExitedReviewMode（Codex 自带 review 模式的进出标记）。
#
# ⚠️ 「刻意不归属」**不等于「不可能改动仓库」**，这两件事必须分开记账
# ------------------------------------------------------------------
# ``McpToolCall`` / ``DynamicToolCall`` 携带结构化 ``arguments``，其中 ``node_repl`` /
# ``cua_repl`` 的 ``js`` 工具（682 条里占 449 条）执行的是**任意 JS**。实测 682 条里有 7 条
# （同一个 2026-07-02 会话）的 ``arguments.code`` 含 ``mkdirSync`` / ``writeFileSync`` 真实
# 文件写入——只是恰好全部落在 ``.gitignore`` 掉的产物目录，至今未造成漏审。
#
# 这类写入仍然**不做归属**：实测 7 条里有 4 条把目标路径算进 JS 变量
# （``pathMod.join(shotDir, …)``），没有可静态还原的路径，按本模块「抽不到就放弃、不猜」
# 的一贯口径只能放弃。但它同时**也逃过 fail-loud 守卫**——``schema_drift_suspected`` 只认
# ``patch_apply_end`` / 内嵌 ``apply_patch`` 作为「编辑证据」——于是「插件工具写了仓库文件
# → 零归属 → 零告警」是一条完整的静默漏审通道。
#
# 因此由 ``plugin_tool_filesystem_write_signatures`` **另发一条独立告警**承接该通道，而不是
# 把这两个类型从本集合里摘掉：摘掉会让 115/1955（5.9%）的正常会话误报「从未见过的新类型」，
# 其中 114 个会话根本没写过任何文件，正是本集合存在的意义所要避免的噪声。
KNOWN_CODEX_ITEM_TYPES = frozenset(
    {
        "AgentMessage",
        "CollabAgentToolCall",
        "CommandExecution",
        "ContextCompaction",
        "DynamicToolCall",
        "EnteredReviewMode",
        "ExitedReviewMode",
        "Extension",
        "FileChange",
        "FunctionCallOutput",
        "HookPrompt",
        "ImageView",
        "McpToolCall",
        "Plan",
        "Reasoning",
        "SubAgentActivity",
        "UserMessage",
        "WebSearch",
    }
)

# 带结构化 ``arguments`` 的插件工具 item：``arguments`` 里可能是任意可执行代码，
# 因而**可以写仓库文件**，却没有 ``changes`` diff 或 argv 可供归属（见上文 ⚠️ 段）。
PLUGIN_TOOL_ITEM_TYPES_WITH_STRUCTURED_ARGUMENTS = frozenset({"DynamicToolCall", "McpToolCall"})

# Node 文件系统写入 API 的**调用形态**（sync 家族 + ``fs.promises`` 家族）。
# 与 ``EMBEDDED_EXEC_COMMAND_CALL_RE`` 同一口径锚定 ``name(``：只出现名字而不是调用
# （例如某次 mcp 搜索的实参正好是字符串 ``"writeFileSync"``）不算一次写入，避免把只读
# 检索误报成文件改动。本机语料上该正则命中 7 条 / 1 个文件，与裸子串同召回、但不误报。
PLUGIN_TOOL_FILESYSTEM_WRITE_CALL_RE = re.compile(
    r"\b(?:writeFileSync|appendFileSync|mkdirSync|unlinkSync|renameSync|rmSync|rmdirSync"
    r"|copyFileSync|cpSync|truncateSync|createWriteStream)\s*\("
    r"|\.\s*promises\s*\.\s*(?:writeFile|appendFile|mkdir|unlink|rename|rm|rmdir|copyFile"
    r"|cp|truncate)\s*\("
)


# ``custom_tool_call`` name=``exec`` 形态把 shell 调用以 JS 文本内嵌在
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
    调用方据此以「返回非 None」作为「这是 ``item_completed`` 信封记录」的唯一判据。
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
    """把信封里的 ``FileChange`` item 映射成 ``patch_apply_end`` payload 形状。

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
    /``parse_apply_patch_details`` 一行不用改——两种形态复用同一套解析，避免各写
    一份再次一起过时。

    ``status`` 非 ``"completed"``（用户中断 / 失败）→ ``success: False``，下游合成函数
    会返回 ``None``，该条不产生归属，与失败的 ``patch_apply_end`` 事件行为一致。
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

    ``user_message`` 形态把这两项放在 payload 顶层（``message`` 是裸字符串、
    ``text_elements`` 平铺）；信封形态搬进了 ``item.content[]``：文本在 ``content[].text``，
    而承载显式 ``$skill`` 调用标记的 ``text_elements`` 挂在**每个 content 块上**，
    此处按块顺序展平。

    返回的形状刻意与 ``user_message`` payload 对齐（``message`` + ``text_elements``），使
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
    """把 ``cwd`` 的 ``file://`` URI 还原成普通文件系统路径。

    **必须 percent-decode**：实测带空格的仓库路径会写成
    ``file:///Users/x/Project%20Theseus``。只剥前缀不解码会同时造成两处损坏——

    1. 去重键与 ``exec_command`` 工具调用侧的 ``workdir``（那边是未编码的字面路径）
       不匹配，同一次执行被两种记录形态各登记一次；
    2. ``command_workdir`` 解析不出 ``%20`` 路径 → 返回 ``None`` → 该次执行的相对路径
       候选被整体丢弃，归属反而比不修更差。

    非 ``file://`` 值、以及带非本机 host 的 URI 原样返回（不猜）。
    """
    if not value.startswith("file://"):
        return value
    parsed = urlparse(value)
    if parsed.netloc not in ("", "localhost"):
        return value
    path = unquote(parsed.path)
    return path or value


def shell_command_and_workdir_from_command_execution_item(
    item: dict[str, Any],
) -> tuple[str, str | None] | None:
    """从 ``CommandExecution`` item 还原 ``(命令文本, workdir)``，形状与
    ``exec_command`` 工具调用的 ``{"cmd": ..., "workdir": ...}`` 实参对齐。

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
    ``exec`` 形态归属全盘失效的第二个死因。此处按 JS 对象字面量的语法逐项扫描（引号 key
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
    """从 ``exec`` 工具 input 的 JS 文本里还原 ``(命令文本, workdir)``。

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

    前向兼容信号：**下一次**形态漂移新增 item 类型时，让 manifest 的 ``warnings`` 先说话，
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


def _plugin_tool_call_signature(item: dict[str, Any]) -> str:
    """把一条插件工具 item 压成 ``server.tool`` 形态的可读签名（如 ``node_repl.js``）。

    ``DynamicToolCall`` 实测没有 ``server``，退回单独的工具名；两者都取不到时退回
    ``item.type``，保证告警里永远有个能让人回头去查的锚点。
    """
    parts = [
        value
        for value in (item.get("server"), item.get("tool") or item.get("name"))
        if isinstance(value, str) and value
    ]
    return ".".join(parts) if parts else str(item.get("type"))


def plugin_tool_filesystem_write_signatures(records: Iterable[dict[str, Any]]) -> list[str]:
    """扫出 ``arguments`` 里含文件系统写入调用的插件工具 item 签名（去重、排序）。

    存在的理由见 ``KNOWN_CODEX_ITEM_TYPES`` 上方的 ⚠️ 段：这些 item **已登记为已知**
    （不该再触发「从未见过的新类型」告警），也**刻意不做归属**（没有可静态还原的路径），
    但它们能改仓库文件，而 ``schema_drift_suspected`` 只认 ``patch_apply_end`` /
    内嵌 ``apply_patch``，所以这条通道既无归属也无告警。此函数就是补上的那条告警信号。

    刻意**只看 ``arguments``**、不看工具输出：输出里出现 ``writeFileSync`` 多半是该工具
    读到了别处的源码，与本次会话有没有写文件无关。

    刻意**不按 ``status`` 过滤**：``failed`` 的调用可能已经写了一部分才失败，对「疑似漏审」
    这种告警而言漏报比误报贵。
    """
    signatures: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        item = codex_item_from_envelope_payload(record.get("payload"))
        if item is None or item.get("type") not in PLUGIN_TOOL_ITEM_TYPES_WITH_STRUCTURED_ARGUMENTS:
            continue
        arguments = item.get("arguments")
        if arguments is None:
            continue
        try:
            serialized = json.dumps(arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            continue
        if PLUGIN_TOOL_FILESYSTEM_WRITE_CALL_RE.search(serialized):
            signatures.add(_plugin_tool_call_signature(item))
    return sorted(signatures)
