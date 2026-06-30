"""Host-身份常量 + transcript-format → host 探测（组合根，host-中性）。

历史上这是 ``trajectory_distill.py`` facade 的一部分（S1）；S9c 把 host 身份真相源
从那个名为 "distill" 的脚本上提到 ``core/host_adapter/``——这里是 RVF 中**唯一**
允许同时知晓 Codex / Claude 两套 transcript record 签名的地方（组合根）。

``detect_transcript_format`` 仅按 transcript 文件首条 record 的 ``type`` 字符串
比对 per-host 签名集合，**不 import 任何 host adapter / 不 spawn 任何 host 二进制**，
故保持 ``core/`` 的 host-中性不变量：业务逻辑模块经注入拿 host 行为，而 host 身份
词汇（``HOST_CODEX`` / ``HOST_CLAUDE``）与探测原语集中住本 ``core/host_adapter/``
注入契约包内（core host-free 退出门须对 ``core/host_adapter/**`` 开 carve-out）。
"""

from __future__ import annotations

import json
from pathlib import Path

HOST_CODEX = "codex"
"""Codex rollout JSONL schema (``session_meta`` / ``turn_context`` / ``event_msg``
/ ``response_item`` record types)。由 ``adapters.codex.transcript`` 解析。"""

HOST_CLAUDE = "claude_code"
"""Claude Code transcript NDJSON schema (``user`` / ``assistant`` / ``summary``
record types，无 ``payload`` 包裹)。由 ``adapters.claude_code.transcript`` 解析。"""

_FORMAT_DETECT_MAX_LINES = 32
_CODEX_RECORD_TYPES = frozenset(
    {"session_meta", "turn_context", "event_msg", "response_item"}
)
_CLAUDE_RECORD_TYPES = frozenset({"user", "assistant", "summary", "system"})


def detect_transcript_format(path: Path) -> str | None:
    """探测 transcript 文件的 schema host：``HOST_CODEX`` / ``HOST_CLAUDE`` / None。

    读首 ``_FORMAT_DETECT_MAX_LINES`` 行非空 JSON-decodable record；命中 Codex
    record types (``session_meta`` / ``turn_context`` / ``event_msg`` /
    ``response_item``) 立即返回 ``HOST_CODEX``，命中 Claude record types
    (``user`` / ``assistant`` / ``summary`` / ``system``) 立即返回 ``HOST_CLAUDE``。
    全部读完仍未命中（空文件 / 异常 schema / 截断）→ 返回 None；调用方应当
    fallback 到 ``HOST_CODEX`` 以保证既有 Codex-only 用例无回归。
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            scanned = 0
            for raw in handle:
                if scanned >= _FORMAT_DETECT_MAX_LINES:
                    break
                scanned += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                if rtype in _CODEX_RECORD_TYPES:
                    return HOST_CODEX
                if rtype in _CLAUDE_RECORD_TYPES:
                    return HOST_CLAUDE
    except OSError:
        return None
    return None
