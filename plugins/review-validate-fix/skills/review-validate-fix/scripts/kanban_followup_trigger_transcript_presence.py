#!/usr/bin/env python3
"""判定「某条已派发的 RVF follow-up trigger 现在还在不在当前会话主链上」。

这是 kanban-followup 锁读侧的对账内核。锁本身只知道「UPS 曾经为某个 dispatch token 上过锁」，
但**提交 ≠ 落地成回合 ≠ 仍在主链上**：

- 注入的 trigger 可能在同一回合被中断冲掉（``queue-operation`` enqueue→dequeue），从未成为
  transcript 条目；
- 也可能落地成了真实回合，但随后被用户 **rewind** 掉（用户重发的新 prompt 把 ``parentUuid``
  重新挂到更早的条目上，那条 trigger 及其后续从此不在主链祖先链上）。

两种情况下继续把锁当 ``in_progress``，读侧就会对一个**从未接受过这轮 RVF** 的 agent 发出
「继续收尾这一轮、输出 handoff」的 re-engage nudge——实测 run
``rvf-20260808T090747Z-stop-hook-ea36796d`` 正是如此被强制续跑的。

判定锚点是那一轮的 token 在正文里的字面形态：kanban 终端投递走 ``RVF_DISPATCH=token=<16 hex>``，
agent 在**发起决策点横幅**上显式接受的就地 run 走 ``RVF_DECISION_TOKEN=<token>``（见
``trigger_needle_for_lock_marker``）。两者都唯一、且必然随注入正文一起进 transcript，不依赖
任何在 arm 时刻拿不到的 message uuid。

四种结论（见 ``TRIGGER_PRESENCE_*``）刻意区分「不在主链」与「整份 transcript 里都没有」：
前者是确凿的 rewind 证据，后者可能只是我们读错了文件（会话不匹配 / 路径不可读），必须保守
处理，绝不据此清一把可能真在跑的锁。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# trigger 落地成回合、且该回合仍在当前 leaf 的祖先链上 → agent 这一轮真的收到了它。
TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN = "on_ancestor_chain"
# trigger 在 transcript 里存在，但不在当前 leaf 的祖先链上 → 被 rewind 掉了（确凿）。
TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND = "off_ancestor_chain_rewound"
# 整份 transcript 里都找不到该 token → 从未落地成回合（被中断冲掉 / 投递静默失败）。
TRIGGER_PRESENCE_ABSENT_FROM_TRANSCRIPT = "absent_from_transcript"
# transcript 不可读 / 无路径 → 无结论，调用方必须保守（维持既有行为，不得据此清锁）。
TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE = "unknown_transcript_unreadable"

# 单份 transcript 的扫描上限（行）。长会话 jsonl 可达数万行；这是 Stop hook 的热路径，必须
# 有界。超限时只保留最后 N 行——祖先链回溯只需要 leaf 附近的连续片段，而 rewind 判定本身也
# 只在「token 在文件里出现过」时才生效（超限截断丢掉的是更早的历史，见 ``_scan``）。
DEFAULT_MAX_SCANNED_LINES = 20000

# 尾部反向读取的块大小（字节）。取值只影响「为了凑够 N 行最少要读多少」这一步的粒度：
# 大到一次通常就够（Claude Code 的 transcript 条目动辄数百字节到几 KB），小到不会因为一次
# 多读而抵消掉有界的意义。
TAIL_SCAN_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True)
class TriggerPresence:
    """一次对账的结论 + 可落 ledger 的诊断字段。"""

    presence: str
    dispatch_token: str | None = None
    transcript_path: str | None = None
    trigger_entry_uuid: str | None = None
    leaf_entry_uuid: str | None = None
    scanned_entry_count: int = 0
    truncated: bool = False
    error: str | None = None

    def as_ledger_fields(self) -> dict[str, Any]:
        return {
            "kanban_followup_trigger_presence": self.presence,
            "kanban_followup_trigger_dispatch_token": self.dispatch_token,
            "kanban_followup_trigger_transcript_path": self.transcript_path,
            "kanban_followup_trigger_entry_uuid": self.trigger_entry_uuid,
            "kanban_followup_transcript_leaf_entry_uuid": self.leaf_entry_uuid,
            "kanban_followup_transcript_scanned_entry_count": self.scanned_entry_count,
            "kanban_followup_transcript_scan_truncated": self.truncated,
            "kanban_followup_trigger_presence_error": self.error,
        }


def dispatch_token_literal(dispatch_token: str) -> str:
    """token 在注入 prompt 正文里的字面形态（与 UPS 的 ``DISPATCH_TOKEN_RE`` 同源）。"""
    return f"RVF_DISPATCH=token={dispatch_token}"


def decision_token_literal(decision_token: str) -> str:
    """S3 决策 token 在**发起决策点横幅**正文里的字面形态。

    与 ``stop_review_validate_fix._render_decision_point_injection_prompt`` 里那一行
    ``RVF_DECISION_TOKEN=<token>`` 同源，也与 ``_decision_token_transcript_trace_present``
    的投递回执探针共用同一个 needle。
    """
    return f"RVF_DECISION_TOKEN={decision_token}"


def trigger_needle_for_lock_marker(marker: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """从锁 marker 推出「这一轮是被什么勾起来的」以及它在 transcript 里的 needle。

    返回 ``(token, needle)``；两者同时为 ``None`` 表示这把锁没记任何可对账的锚点。

    锁有两个来源，needle 形态不同，**不能只认 dispatch token**：

    * ``dispatch_token``：cline-kanban 终端投递那条 trigger（``RVF_DISPATCH=token=…``）；
    * ``decision_token``：agent 在发起决策点横幅上显式 ``rvf-agent start`` 接受的就地 run
      （``RVF_DECISION_TOKEN=…``）。P1 之后这才是**默认**来源。

    dispatch 优先：一条真的被投递过的 trigger 是更强的「agent 这一轮看到了什么」证据；
    同时存在只可能是 legacy 混写，取前者不会误判。
    """
    if not isinstance(marker, Mapping):
        return None, None
    dispatch_token = marker.get("dispatch_token")
    if isinstance(dispatch_token, str) and dispatch_token.strip():
        token = dispatch_token.strip()
        return token, dispatch_token_literal(token)
    decision_token = marker.get("decision_token")
    if isinstance(decision_token, str) and decision_token.strip():
        token = decision_token.strip()
        return token, decision_token_literal(token)
    return None, None


def _entry_timestamp(entry: dict[str, Any]) -> float | None:
    raw = entry.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _entry_is_explicit_human_prompt(entry: dict[str, Any]) -> bool:
    """只认**显式**的人类输入信号，不做「不像机器注入就算人类」的反向推断。

    Claude Code 给用户手打的 prompt 打 ``promptSource: "typed"`` 与 ``origin.kind: "human"``；
    两个字段都没有时（别的 harness / 老格式）一律返回 False——宁可漏判也不误判：误判会把一次
    合法的迟到投递当成「会话已走远」而丢掉整轮 review。
    """
    if entry.get("type") != "user":
        return False
    if entry.get("promptSource") == "typed":
        return True
    origin = entry.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def has_explicit_human_prompt_after(
    *,
    transcript_path: str | Path | None,
    after_epoch_seconds: float | None,
    max_scanned_lines: int = DEFAULT_MAX_SCANNED_LINES,
) -> tuple[bool, str | None]:
    """``after_epoch_seconds`` 之后，这份 transcript 里出现过显式的人类 prompt 吗？

    返回 ``(found, error)``；``error`` 非空表示无结论（路径缺失 / 不可读），调用方须当作
    「没有否决证据」处理。绝不抛。
    """
    if after_epoch_seconds is None:
        return False, "no_reference_timestamp"
    if not transcript_path:
        return False, "no_transcript_path"
    try:
        path = Path(transcript_path).expanduser()
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    entries, _truncated, error = _scan(path, max_scanned_lines=max_scanned_lines)
    if error is not None:
        return False, error
    for entry in entries:
        if not _entry_is_explicit_human_prompt(entry):
            continue
        stamp = _entry_timestamp(entry)
        if stamp is not None and stamp > after_epoch_seconds:
            return True, None
    return False, None


def _entry_text(entry: dict[str, Any]) -> str:
    """把一条 transcript 条目里**可能承载 prompt 正文**的部分摊平成一段文本。

    只取 ``message.content``（str 或 block 列表的 ``text``）。刻意不 ``json.dumps`` 整条条目：
    那会把 hook 诊断、工具参数等**引用**了 token 的地方也算成「trigger 在场」，而我们要找的是
    这条 trigger 本身作为一个 user prompt 落地。

    唯一的例外是 Claude Code 的 ``queued_command`` 附件：agent 回合进行中送达的消息以它落盘，
    正文在 ``attachment.prompt``、没有 ``message``。它**就是**一条 user prompt 落地（审计 M13：
    Claude Code < 2.1.260 上它还不触发 UserPromptSubmit），不认它就会把「标记在链上」判成「从未落地」。
    """
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        queued_prompt = attachment.get("prompt")
        return queued_prompt if isinstance(queued_prompt, str) else ""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _read_tail_lines_within_scan_bound(
    path: Path,
    *,
    max_scanned_lines: int,
) -> tuple[list[str], bool]:
    """从文件**尾部**反向按块读，只把最后 ``max_scanned_lines`` 行取进内存。

    返回 ``(lines, truncated)``；``truncated`` 表示文件里还有更早的行没被读到。可能抛 OSError
    等，由唯一调用方 ``_scan`` 统一兜住。

    为什么不能是 ``readlines()[-N:]``（这正是本函数替换掉的原实现）：那样声明的上界只作用在
    **返回值**上，内存与耗时仍按整份 transcript 走。而这里是 Stop hook 锁对账 + UPS 陈旧 prep
    否决的同步热路径，长会话 jsonl 几万行、几十 MB，每次 Stop 都整份读一遍——注释写着「必须
    有界」，实现却没有界。
    为什么不能是 ``deque(handle, maxlen=N)``：内存有界了，但整份文件仍要逐行解码一遍，耗时依旧
    随会话长度线性增长。反向按块读则内存与 IO 都只随 N 走。

    与原实现逐行等价（含 ``truncated``），只有一处刻意的行为差异见下面的 ``max_scanned_lines
    <= 0`` 分支。CRLF 结尾不特殊处理：调用方对每行做 ``strip()``，行尾的 ``\\r`` 自然被吃掉。
    """
    if max_scanned_lines <= 0:
        # 退化输入。原实现在这里因为 ``lines[-0:] is lines`` 会**读回整份文件**——恰好是本次
        # 要消灭的无界行为，故不予保留：要求扫 0 行就一行都不扫，并如实报 truncated。
        return [], True
    chunks: list[bytes] = []
    newline_count = 0
    reached_file_start = True
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        unread_byte_offset = handle.tell()
        while unread_byte_offset > 0:
            chunk_size = min(TAIL_SCAN_CHUNK_BYTES, unread_byte_offset)
            unread_byte_offset -= chunk_size
            handle.seek(unread_byte_offset)
            chunks.append(handle.read(chunk_size))
            newline_count += chunks[-1].count(b"\n")
            # 要 max_scanned_lines+1 个换行才能确认「最后 N 行都是完整的」：多出来的那个是第 N
            # 行的**起始**边界，它之前的那截是块边界上的半行，下面会丢掉。
            if newline_count > max_scanned_lines:
                reached_file_start = unread_byte_offset == 0
                break
    # 只在拼接完成后整体解码：多字节字符唯一可能被切断的位置是最前面那截，而那截要么整份文件
    # 都在（``reached_file_start``），要么本来就是要丢弃的半行。
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        # 文件以换行结尾时 ``split`` 会多出一个空尾项，``readlines()`` 不会；对齐掉，否则
        # ``truncated`` 的行数比较会整体偏移一位。
        lines.pop()
    if reached_file_start:
        truncated = len(lines) > max_scanned_lines
    else:
        truncated = True
        if lines:
            lines.pop(0)  # 块边界上的半行
    if len(lines) > max_scanned_lines:
        lines = lines[-max_scanned_lines:]
    return lines, truncated


def _scan(
    path: Path,
    *,
    max_scanned_lines: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """读出 transcript 条目（尾部有界），返回 ``(entries, truncated, error)``。绝不抛。

    「有界」是**读取**层面的界，不只是返回值的界——见
    ``_read_tail_lines_within_scan_bound``。
    """
    try:
        lines, truncated = _read_tail_lines_within_scan_bound(
            path, max_scanned_lines=max_scanned_lines
        )
    except (OSError, UnicodeError) as exc:
        return [], False, f"{type(exc).__name__}: {exc}"
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries, truncated, None


def classify_dispatch_trigger_presence(
    *,
    transcript_path: str | Path | None,
    dispatch_token: str | None,
    trigger_literal: str | None = None,
    max_scanned_lines: int = DEFAULT_MAX_SCANNED_LINES,
) -> TriggerPresence:
    """判定 ``dispatch_token`` 那条 trigger 相对当前 leaf 的位置。绝不抛。

    ``trigger_literal`` 缺省 = ``dispatch_token_literal(dispatch_token)``（kanban 终端投递那条
    trigger）。agent 在发起决策点横幅上显式接受的就地 run 用的是另一种字面形态，调用方经
    ``trigger_needle_for_lock_marker(marker)`` 一次拿到 ``(token, needle)`` 并显式传入。

    祖先链 = 从**文件里最后一条带 uuid 的条目**（当前 leaf）沿 ``parentUuid`` 一路回溯得到的
    uuid 集合。rewind 之后用户重发的 prompt 会把 ``parentUuid`` 挂回更早的条目，被撤销的那段
    （含注入的 trigger）自然落在该集合之外。
    """
    if not (isinstance(dispatch_token, str) and dispatch_token.strip()):
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE,
            transcript_path=str(transcript_path) if transcript_path else None,
            error="no_dispatch_token",
        )
    token = dispatch_token.strip()
    if not transcript_path:
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE,
            dispatch_token=token,
            error="no_transcript_path",
        )
    try:
        path = Path(transcript_path).expanduser()
    except (OSError, ValueError) as exc:
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE,
            dispatch_token=token,
            transcript_path=str(transcript_path),
            error=f"{type(exc).__name__}: {exc}",
        )
    entries, truncated, error = _scan(path, max_scanned_lines=max_scanned_lines)
    if error is not None or not entries:
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE,
            dispatch_token=token,
            transcript_path=str(path),
            scanned_entry_count=len(entries),
            truncated=truncated,
            error=error or "empty_transcript",
        )

    literal = (
        trigger_literal
        if isinstance(trigger_literal, str) and trigger_literal.strip()
        else dispatch_token_literal(token)
    )
    by_uuid: dict[str, dict[str, Any]] = {}
    trigger_uuids: list[str] = []
    leaf_uuid: str | None = None
    for entry in entries:
        uuid = entry.get("uuid")
        if isinstance(uuid, str) and uuid:
            by_uuid[uuid] = entry
            leaf_uuid = uuid
        if literal in _entry_text(entry) and isinstance(uuid, str) and uuid:
            trigger_uuids.append(uuid)

    if not trigger_uuids:
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_ABSENT_FROM_TRANSCRIPT,
            dispatch_token=token,
            transcript_path=str(path),
            leaf_entry_uuid=leaf_uuid,
            scanned_entry_count=len(entries),
            truncated=truncated,
        )

    ancestors: set[str] = set()
    cursor = leaf_uuid
    # 环形 parentUuid（不该出现，但坏文件不得把 Stop hook 挂死）：靠 ancestors 去重 + 条目数上界。
    while isinstance(cursor, str) and cursor and cursor not in ancestors:
        ancestors.add(cursor)
        entry = by_uuid.get(cursor)
        if entry is None:
            break
        parent = entry.get("parentUuid")
        cursor = parent if isinstance(parent, str) and parent else None

    on_chain = next((uuid for uuid in trigger_uuids if uuid in ancestors), None)
    if on_chain is not None:
        return TriggerPresence(
            presence=TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN,
            dispatch_token=token,
            transcript_path=str(path),
            trigger_entry_uuid=on_chain,
            leaf_entry_uuid=leaf_uuid,
            scanned_entry_count=len(entries),
            truncated=truncated,
        )
    return TriggerPresence(
        presence=TRIGGER_PRESENCE_OFF_ANCESTOR_CHAIN_REWOUND,
        dispatch_token=token,
        transcript_path=str(path),
        trigger_entry_uuid=trigger_uuids[-1],
        leaf_entry_uuid=leaf_uuid,
        scanned_entry_count=len(entries),
        truncated=truncated,
    )
