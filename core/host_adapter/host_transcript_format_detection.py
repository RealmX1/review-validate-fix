"""Host-身份常量 + transcript-format → host 探测（组合根，host-中性）。

历史上这是 ``trajectory_distill.py`` facade 的一部分（S1）；S9c 把 host 身份真相源
从那个名为 "distill" 的脚本上提到 ``core/host_adapter/``——这里是 RVF 中**唯一**
允许同时知晓 Codex / Claude / Kimi 三套 transcript record 签名的地方（组合根）。

``detect_transcript_format`` 仅按 transcript 文件首条 record 的 ``type`` 字符串
比对 per-host 签名集合，**不 import 任何 host adapter / 不 spawn 任何 host 二进制**，
故保持 ``core/`` 的 host-中性不变量：业务逻辑模块经注入拿 host 行为，而 host 身份
词汇（``HOST_CODEX`` / ``HOST_CLAUDE`` / ``HOST_KIMI_CODE_CLI``）与探测原语集中住本
``core/host_adapter/`` 注入契约包内（core host-free 退出门须对 ``core/host_adapter/**`` 开 carve-out）。
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

HOST_KIMI_CODE_CLI = "kimi_code_cli"
"""Kimi Code CLI 的 per-agent wire JSONL schema（``metadata`` + 19 种带点命名空间的
事件 type）。由 ``adapters.kimi.transcript`` 解析。

⚠️ 它的 transcript **不是**每会话一个文件，而是
``<sessions_root>/wd_<workdir>_<hash>/session_<uuid>/agents/<agent_id>/wire.jsonl``
——每个 agent 一份。``session_id`` 本身**已经**带 ``session_`` 前缀，拼路径时不要再加。
"""

_FORMAT_DETECT_MAX_LINES = 32
_CODEX_RECORD_TYPES = frozenset(
    {"session_meta", "turn_context", "event_msg", "response_item"}
)
_CLAUDE_RECORD_TYPES = frozenset({"user", "assistant", "summary", "system"})
_KIMI_CODE_CLI_RECORD_TYPES = frozenset(
    {
        "metadata",
        "turn.prompt",
        "turn.cancel",
        "turn.steer",
        "context.append_message",
        "context.append_loop_event",
        "context.undo",
        "context.apply_compaction",
        "llm.request",
        "llm.tools_snapshot",
        "usage.record",
        "config.update",
        "tools.set_active_tools",
        "tools.update_store",
        "permission.set_mode",
        "permission.record_approval_result",
        "swarm_mode.enter",
        "swarm_mode.exit",
        "full_compaction.begin",
        "full_compaction.complete",
    }
)
"""实测得来（本机 42 份 wire.jsonl，0 条非法行，恰好这 20 种），非照抄文档。

**42/42 份文件的首行 type 都是 ``metadata``** ⇒ 首行探测稳定，不必依赖后续行。
"""

KNOWN_HOST_IDENTITIES = frozenset({HOST_CODEX, HOST_CLAUDE, HOST_KIMI_CODE_CLI})
"""本探测器能正向识别的全部 host 身份。

公开出来是给**装配层**（``host_adapter_resolver``）做完备性断言用的：每多一个 host 身份，
resolver 就必须多一条装配分支，否则该 host 会静默落进「未知 → Codex」兜底——那正是
「RVF 在新 host 上零报错地完全失声」的成因。让它在 resolver 的 self-check 里当场炸。
"""

_HOST_RECORD_TYPE_SIGNATURES: dict[str, frozenset[str]] = {
    HOST_CODEX: _CODEX_RECORD_TYPES,
    HOST_CLAUDE: _CLAUDE_RECORD_TYPES,
    HOST_KIMI_CODE_CLI: _KIMI_CODE_CLI_RECORD_TYPES,
}
"""签名总表：**只**供 ``_self_check`` 做不相交与防漂移断言，不参与探测。

探测本体刻意保持显式 ``if`` 链（顺序稳定、可读），代价是 if 链与本表可能漂移——所以
``_self_check`` 用真实探测逐 type 回放，漂移必红。加第五个 host 时若忘了更新本表，
不相交护栏就会静默失效，那正是本表存在的理由。
"""


def detect_transcript_format(path: Path) -> str | None:
    """探测 transcript 的 schema host：``HOST_CODEX`` / ``HOST_CLAUDE`` / ``HOST_KIMI_CODE_CLI`` / None。

    读首 ``_FORMAT_DETECT_MAX_LINES`` 行非空 JSON-decodable record，按 record 的顶层
    ``type`` 字符串比对三套签名集合，首命中即返回。三套集合**两两不相交**（由
    ``_self_check`` 断言），所以 ``if`` 的先后不影响结果——既有顺序保持不动是为了让
    diff 只体现「新增」而非「重排」。

    全部读完仍未命中（空文件 / 异常 schema / 截断）→ 返回 None；调用方应当
    fallback 到 ``HOST_CODEX`` 以保证既有 Codex-only 用例无回归。

    ⚠️ **兜底方向是刻意不对称的**：把非-Codex 误判成 Codex 只是降级（仍收到
    ``decision:block``，能工作）；把 Codex 误判成别的会让它收到 ``hookSpecificOutput``
    而整条 hook 输出 Failed（``additionalProperties:false``）= RVF 在 Codex 上静默停摆。
    所以新 host **只在有正向内容证据时**被选中，无证据一律回 None 让调用方兜底 Codex。
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
                if rtype in _KIMI_CODE_CLI_RECORD_TYPES:
                    return HOST_KIMI_CODE_CLI
    except OSError:
        return None
    return None


def _self_check() -> None:
    """runnable check：签名两两不相交 + 探测行为与签名表不漂移。

    **为什么不相交必须被断言而不是「看上去显然」**：``detect_transcript_format`` 只比对
    顶层 ``type`` 字符串、首命中即返回。两套签名一旦有交集，落在交集里的 transcript 会被
    判给 ``if`` 链里靠前的那个 host——**静默的**，没有任何报错。今天三套确实不相交，但
    加第五个 host 的人不会知道这件事有多脆，所以让它在 self-check 里当场炸。
    """
    import itertools
    import tempfile

    signatures = _HOST_RECORD_TYPE_SIGNATURES
    assert len(signatures) == 3, signatures
    for host, types in signatures.items():
        assert types, f"{host} 的签名集合为空——空集合会让该 host 永不被命中"

    for left, right in itertools.combinations(sorted(signatures), 2):
        overlap = signatures[left] & signatures[right]
        assert not overlap, (
            f"{left} 与 {right} 的 record 签名相交于 {sorted(overlap)}；"
            "落在交集里的 transcript 会被静默判给 if 链里靠前的那个 host。"
        )

    # 防漂移：逐 type 回放真实探测。签名表里有、而 if 链漏了的 type 会在这里返回 None
    # 或错误 host —— 这正是「加了第五个 host 却忘了加 if 分支」的形状。
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "wire.jsonl"
        for host, types in signatures.items():
            for record_type in sorted(types):
                probe.write_text(
                    json.dumps({"type": record_type}) + "\n", encoding="utf-8"
                )
                detected = detect_transcript_format(probe)
                assert detected == host, (
                    f"record type {record_type!r} 登记在 {host} 名下，探测却返回 "
                    f"{detected!r} —— 签名表与 detect_transcript_format 的 if 链已漂移。"
                )

        probe.write_text(
            json.dumps({"type": "definitely-not-a-known-record-type"}) + "\n",
            encoding="utf-8",
        )
        assert detect_transcript_format(probe) is None, "未知 schema 必须回 None 让调用方兜底 Codex"

        probe.write_text("", encoding="utf-8")
        assert detect_transcript_format(probe) is None, "空文件必须回 None"

        probe.write_text("{ not json\n", encoding="utf-8")
        assert detect_transcript_format(probe) is None, "非法 JSON 必须回 None 而不是抛"

        assert detect_transcript_format(Path(tmp) / "does-not-exist.jsonl") is None

        # Kimi 的实测不变量：42/42 份 wire.jsonl 首行都是 metadata ⇒ 首行即可定 host。
        probe.write_text(
            "\n".join(
                json.dumps({"type": rt})
                for rt in ("metadata", "turn.prompt", "llm.request")
            )
            + "\n",
            encoding="utf-8",
        )
        assert detect_transcript_format(probe) == HOST_KIMI_CODE_CLI

    print("host_transcript_format_detection self-check OK")


if __name__ == "__main__":
    _self_check()
