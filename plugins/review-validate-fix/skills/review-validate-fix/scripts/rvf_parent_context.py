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
复用 ``trajectory_distill.detect_transcript_format`` 判定父 transcript 格式：
``HOST_CODEX`` 走 Codex rollout schema，``HOST_CLAUDE`` 走 Claude transcript
schema；探测失败（空文件/异常 schema）fallback 到 Codex 解析，与既有
Codex-only 用例保持一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trajectory_distill import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    detect_transcript_format,
)

# ---------------------------------------------------------------------------
# 压缩规则常量（集中、注释清楚，便于调参）
# ---------------------------------------------------------------------------

#: tool_call 参数渲染上限（字节，UTF-8）。超出截断并标注 ``…[+NB]``。
TOOL_ARGS_LIMIT_BYTES = 800
#: tool_result / exec 输出渲染上限（字节，UTF-8）。超出截断并标注。
TOOL_RESULT_LIMIT_BYTES = 400
#: 默认总字节预算（与 codex_stop hook 的 CODEX_RVF_PARENT_CONTEXT_MAX_BYTES 默认对齐）。
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


def _render_codex_record(record: dict[str, Any]) -> str | None:
    """把一条 raw Codex rollout 记录压成一行可读 context；返回 None=丢弃。"""
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


# ---------------------------------------------------------------------------
# Claude Code transcript schema 渲染
# ---------------------------------------------------------------------------


def _render_claude_record(record: dict[str, Any]) -> list[str]:
    """把一条 Claude Code NDJSON 记录压成 0..N 行可读 context。

    一条 ``assistant`` record 可能同时含 ``text`` / ``thinking`` / 多条
    ``tool_use``；一条 ``user`` record 可能含 ``tool_result``，逐 block 展开。
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
# 总装配
# ---------------------------------------------------------------------------


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
    records = _iter_records(transcript_path)
    lines: list[str] = []
    if host == HOST_CLAUDE:
        for record in records:
            lines.extend(_render_claude_record(record))
    else:  # HOST_CODEX（含 fallback）
        for record in records:
            rendered = _render_codex_record(record)
            if rendered is not None:
                lines.append(rendered)
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
