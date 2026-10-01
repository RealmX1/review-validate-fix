#!/usr/bin/env python3
"""带外决策点：铸 token 之后，agent 是否**复工**过（开始过一个不是这条边界标记自己开启的回合）？

## 为什么要问这个

Stop 铸出带外横幅时，候选范围是那一刻的工作树、时机是「agent 刚停下」。边界标记要等 cline-kanban
粘贴进终端才落地；在那之前 agent 可能已经复工：后台任务完成通知唤醒它、开发者发话、别的 Stop hook
让它续跑……此时标记会在**回合中途**以排队消息并入（Claude Code 记成 ``queued_command``），或者排在
一个已经干完别的活的回合之后。两种情况下这份决策点都过时了：范围不对，时机也不对。所以复工 ⇒ 撤回
token、不注入正文，由 Stop 按当时的工作树重新评估。只有「标记在 agent 空闲时开出了自己的回合」那一种
才是正常落地。

## 三态结论（``DECISION_POINT_AGENT_*``）

* ``agent_resumed_work_after_token_mint`` —— 有正向证据表明复工了；
* ``turn_opened_by_decision_point_boundary_marker`` —— 有正向证据表明标记自己开了回合、之前没复工；
* ``no_evidence_either_way`` —— 两样都拿不到。调用方**维持现状**（不撤回、不补发），交给既有的在途闸
  与搁浅对账。理由：Codex rollout 没有 uuid 链，若「无证据 ⇒ 撤回」，UPS 失效的 Codex 会话会无限
  「铸 → 撤」。

任一证据源判复工即判复工；否则任一判「标记开回合」即判标记开回合；否则无证据。只看与 token **同一
会话**的证据，会话对不上一律无证据（同一 worktree 里另开的会话不该撤回本会话的横幅）。

## 证据源

* **E1 · UPS 按会话的到达记录**（本模块落盘，见 ``record_user_prompt_submit_arrival_and_return_previous_arrivals``）。
  铸 token 之后到达过任何**不是这枚 token 的边界标记**的 prompt（开发者发话、host 合成的任务通知、RVF
  自己的机器正文、别的 token 的标记）⇒ 复工。例外：**到达时已作废**的标记（迟到落地、被 UPS 挡下的那种）
  不算，否则一份迟到标记会把下一枚新 token 也判成复工、自我续命。「到达时已作废」按那枚 token 的状态历史
  判（UPS 记录时刻意不读 token，见 ``rvf_user_prompt_submit`` 的到达记录函数）。
* **E2 · transcript 祖先链**（Claude Code 的 uuid 链格式）。标记以 ``queued_command`` 附件出现 ⇒ 复工
  （回合中途并入）。否则看标记之前（标记还没落盘时看 leaf 之前）离它最近的一条 user / assistant 条目：
  时间戳晚于铸出时刻 ⇒ 复工；不晚于 ⇒ 标记在链上时判「标记开回合」，不在链上时无证据；时间戳缺失 ⇒
  无证据。只看「最近一条」就够：铸出之后的任何活动都会比铸出之前的条目更靠近标记。回扫时**透明**跳过
  开发者本地斜杠命令整组（caveat / ``<command-name>`` / ``<local-command-stdout|stderr>``，按正文开头标签认）
  与 compact 摘要（``isCompactSummary``）——开发者空闲时跑 ``/compact``、``/model`` 不是复工；isMeta **不**
  整体透明（``Stop hook feedback:`` 就是复工）。先撞到 ``compact_boundary``：``trigger=auto`` 且晚于铸出 ⇒
  复工（auto-compact 只在模型查询中发生）；manual / auto 不晚于铸出 ⇒ 无证据（不沿 ``logicalParentUuid``
  回溯压缩前的历史）。
* **E3 · host 回合标识**（``HostAdapter.agent_turn_identifier_shared_by_hook_events_of_one_turn``：
  Claude Code ``prompt_id``、Codex ``turn_id``）。当前 hook 事件与铸 token 的那次 Stop 属于**同一个**
  回合 ⇒ 那个回合在铸出之后又被续跑了（Stop 续跑，或标记被并入那个回合）⇒ 复工。

## 2026-10-02 取证（Claude Code 2.1.287，隔离 settings 的 ``claude -p`` stream-json）

* 排队消息的 UPS 在**并入上下文那一刻**才触发（排队后约 15s，紧跟工具结果），不是排队那一刻；
* 触发时 transcript 里已有铸点之后的 assistant 条目，**还没有** ``queued_command``（它在 UPS 之后才写）；
  ⇒ UPS 侧靠 E2 的「最近活动晚于铸出」与 E3 判回合中途，``queued_command`` 只在 Stop 侧作辅证；
* 对排队消息返回 ``decision:block`` **有效**（``queue-operation remove reason=dropped_by_hook``，不写
  ``queued_command``，模型看不到）；``additionalContext`` 也能随排队消息送达；
* 空闲时提交的 prompt，UPS 触发时 transcript 里还没有它自己的条目。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from kanban_followup_trigger_transcript_presence import (  # noqa: E402
    decision_token_literal,
    entry_is_queued_command_attachment,
    entry_text,
    entry_timestamp_epoch_seconds,
    transcript_ancestor_chain_from_current_leaf,
)

DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT = "agent_resumed_work_after_token_mint"
DECISION_POINT_TURN_OPENED_BY_DECISION_POINT_BOUNDARY_MARKER = "turn_opened_by_decision_point_boundary_marker"
DECISION_POINT_AGENT_RESUMED_WORK_NO_EVIDENCE_EITHER_WAY = "no_evidence_either_way"

EVIDENCE_SOURCE_USER_PROMPT_SUBMIT_ARRIVALS_RECORDED_FOR_SESSION = "user_prompt_submit_arrivals_recorded_for_session"
EVIDENCE_SOURCE_TRANSCRIPT_ANCESTOR_CHAIN = "transcript_ancestor_chain"
EVIDENCE_SOURCE_HOST_AGENT_TURN_IDENTIFIER = "host_agent_turn_identifier"

# UPS 到达种类（E1 记录里的 ``user_prompt_submit_arrival_kind``）。
USER_PROMPT_SUBMIT_ARRIVAL_KIND_DEVELOPER_PROMPT = "developer_prompt"
USER_PROMPT_SUBMIT_ARRIVAL_KIND_HOST_SYNTHESIZED_TURN_PREFIX = "host_synthesized_turn:"
USER_PROMPT_SUBMIT_ARRIVAL_KIND_RVF_MACHINE_PROMPT = "rvf_machine_prompt"
USER_PROMPT_SUBMIT_ARRIVAL_KIND_DECISION_POINT_BOUNDARY_MARKER = "decision_point_boundary_marker"

# 边界标记到达时它那枚 token 已处于这些状态 ⇒ 标记作废（UPS 以 block 挡下，不开回合，不算复工）。expired /
# lost 不在其列：它们落进 UPS「未能推进」的出口、不挡，标记照常开一个回合。
DECISION_TOKEN_STATES_THAT_VOID_A_LATER_ARRIVING_BOUNDARY_MARKER = frozenset({"withdrawn", "dispatched", "declined"})

USER_PROMPT_SUBMIT_ARRIVALS_SUBDIRECTORY_NAME = "decision-point-agent-resumed-work-user-prompt-submit-arrivals"
USER_PROMPT_SUBMIT_ARRIVALS_SCHEMA = "rvf-decision-point-agent-resumed-work-user-prompt-submit-arrivals/v1"
# 每个会话只留最近这么多条到达。判据只看「铸出之后」的到达，而带外 token 最多活 1200s，几十条绰绰有余；
# 上限只为不让长会话的记录文件无界增长。
USER_PROMPT_SUBMIT_ARRIVALS_KEPT_PER_SESSION = 64


@dataclass(frozen=True)
class DecisionPointAgentResumedWorkAfterTokenMintVerdict:
    """一次判定的结论 + 可落 ledger 的证据摘要。"""

    verdict: str
    evidence_source: str | None = None
    evidence_detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def agent_resumed_work_after_token_mint(self) -> bool:
        return self.verdict == DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT

    @property
    def turn_opened_by_decision_point_boundary_marker(self) -> bool:
        return self.verdict == DECISION_POINT_TURN_OPENED_BY_DECISION_POINT_BOUNDARY_MARKER

    def as_ledger_fields(self) -> dict[str, Any]:
        return {
            "decision_point_agent_resumed_work_verdict": self.verdict,
            "decision_point_agent_resumed_work_evidence_source": self.evidence_source,
            "decision_point_agent_resumed_work_evidence_detail": dict(self.evidence_detail),
        }


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp_epoch_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ── E1：UPS 按会话的到达记录 ──────────────────────────────────────────────────────────────


def _user_prompt_submit_arrivals_paths(session_id: str, state_directory: Path | None) -> tuple[Path, Path]:
    if state_directory is None:
        from core.rvf_user_local_data_paths import resolved_rvf_session_hook_state_dir  # noqa: PLC0415

        state_directory = resolved_rvf_session_hook_state_dir()
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    base = Path(state_directory) / USER_PROMPT_SUBMIT_ARRIVALS_SUBDIRECTORY_NAME
    return base / f"{session_digest}.json", base / f"{session_digest}.lock"


def _read_user_prompt_submit_arrivals_document(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    arrivals = document.get("user_prompt_submit_arrivals") if isinstance(document, dict) else None
    if not isinstance(arrivals, list):
        return []
    return [arrival for arrival in arrivals if isinstance(arrival, dict)]


def record_user_prompt_submit_arrival_and_return_previous_arrivals(
    *,
    session_id: str | None,
    user_prompt_submit_arrival_kind: str,
    decision_token: str | None,
    agent_turn_identifier: str | None,
    state_directory: Path | None = None,
    arrived_at: str | None = None,
) -> list[dict[str, Any]]:
    """UPS 侧：在会话的到达记录里追加这一条，返回**追加之前**的全部到达（供本次判定用）。

    读旧值 + 写新值在同一把 sidecar flock 内完成（同一会话的两次 UPS 可能并发：排队消息并入的那一刻，
    后台任务通知也可能刚好到达）。best-effort：任何 I/O 失败都吞掉、返回已读到的旧值——少记一条只会让
    之后的判定少一份证据（落到「无证据 ⇒ 维持现状」），绝不阻断这条 prompt。
    """
    if not (isinstance(session_id, str) and session_id.strip()):
        return []
    try:
        arrivals_path, lock_path = _user_prompt_submit_arrivals_paths(session_id.strip(), state_directory)
        arrivals_path.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except Exception:  # noqa: BLE001
        return []
    with os.fdopen(lock_descriptor, "w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        previous_arrivals = _read_user_prompt_submit_arrivals_document(arrivals_path)
        new_arrival = {
            "arrived_at": arrived_at or _utc_now_timestamp(),
            "user_prompt_submit_arrival_kind": user_prompt_submit_arrival_kind,
            "decision_token": decision_token,
            "agent_turn_identifier": agent_turn_identifier,
        }
        kept = [*previous_arrivals, new_arrival][-USER_PROMPT_SUBMIT_ARRIVALS_KEPT_PER_SESSION:]
        try:
            temporary_path = arrivals_path.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps(
                    {
                        "schema": USER_PROMPT_SUBMIT_ARRIVALS_SCHEMA,
                        "user_prompt_submit_arrivals": kept,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary_path.replace(arrivals_path)
        except OSError:
            pass
        return previous_arrivals


def read_user_prompt_submit_arrivals_recorded_for_session(
    *, session_id: str | None, state_directory: Path | None = None
) -> list[dict[str, Any]]:
    """Stop 侧：读会话的到达记录（只读，不取锁——原子替换写，读到的总是某一份完整快照）。"""
    if not (isinstance(session_id, str) and session_id.strip()):
        return []
    try:
        arrivals_path, _lock_path = _user_prompt_submit_arrivals_paths(session_id.strip(), state_directory)
    except Exception:  # noqa: BLE001
        return []
    return _read_user_prompt_submit_arrivals_document(arrivals_path)


# ── 判定 ─────────────────────────────────────────────────────────────────────────────────


def _boundary_marker_arrival_was_void_on_arrival(arrival: Mapping[str, Any], arrived_at: float) -> bool:
    """这条边界标记到达时，它那枚 token 是否**已经**撤回 / 已决策（按 token 的状态历史判，读不到即 False）。

    「已经」要按历史时刻比，不能按现在的状态比：一份到达时还活着、之后才被决策掉的标记，当时确实开了回合。
    """
    marker_token = arrival.get("decision_token")
    if not (isinstance(marker_token, str) and marker_token):
        return False
    try:
        import rvf_decision_token_store  # noqa: PLC0415 - stdlib-only

        payload = rvf_decision_token_store.read_decision_token(marker_token).payload
    except Exception:  # noqa: BLE001 - 读不到就当它当时还活着（多判一次复工只是多问一次）
        return False
    history = payload.get("history") if isinstance(payload, dict) else None
    for transition in history if isinstance(history, list) else ():
        if not isinstance(transition, dict):
            continue
        if transition.get("to") not in DECISION_TOKEN_STATES_THAT_VOID_A_LATER_ARRIVING_BOUNDARY_MARKER:
            continue
        voided_at = _parse_timestamp_epoch_seconds(transition.get("at"))
        return voided_at is not None and voided_at <= arrived_at
    return False


def _verdict_from_user_prompt_submit_arrivals(
    *,
    decision_token: str,
    minted_at_epoch_seconds: float,
    arrivals: Sequence[Mapping[str, Any]],
) -> DecisionPointAgentResumedWorkAfterTokenMintVerdict | None:
    for arrival in arrivals:
        arrived_at = _parse_timestamp_epoch_seconds(arrival.get("arrived_at"))
        if arrived_at is None or arrived_at <= minted_at_epoch_seconds:
            continue
        kind = arrival.get("user_prompt_submit_arrival_kind")
        if kind == USER_PROMPT_SUBMIT_ARRIVAL_KIND_DECISION_POINT_BOUNDARY_MARKER and (
            arrival.get("decision_token") == decision_token
            or _boundary_marker_arrival_was_void_on_arrival(arrival, arrived_at)
        ):
            continue
        return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
            verdict=DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT,
            evidence_source=EVIDENCE_SOURCE_USER_PROMPT_SUBMIT_ARRIVALS_RECORDED_FOR_SESSION,
            evidence_detail={
                "user_prompt_submit_arrival_kind": kind,
                "user_prompt_submit_arrived_at": arrival.get("arrived_at"),
                "user_prompt_submit_arrival_decision_token": arrival.get("decision_token"),
            },
        )
    return None


# 开发者本地斜杠命令（``/compact``、``/model``……）在 Claude Code transcript 里落成的 type:user 条目，按正文开头
# 的标签识别。它们不驱动 agent，E2 回扫时透明。prompt 型命令（skill / 自定义命令）若同样以 ``<command-name>``
# 开头也会被跳过——可以接受：其后的 assistant 条目与 E1 到达记录仍能兜住。认不出的标签照旧算活动（判复工，安全方向）。
DEVELOPER_LOCAL_SLASH_COMMAND_ENTRY_TEXT_LEADING_TAGS = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)


def _transcript_entry_is_transparent_to_agent_resumed_work_scan(entry: Mapping[str, Any]) -> bool:
    """E2 回扫时跳过：开发者本地斜杠命令整组 + compact 摘要。**不**整体跳过 isMeta（如 ``Stop hook feedback:``
    表示别的 Stop hook 让 agent 续跑，本身就是复工信号）。"""
    if entry.get("isCompactSummary") is True:
        return True
    return entry_text(dict(entry)).lstrip().startswith(DEVELOPER_LOCAL_SLASH_COMMAND_ENTRY_TEXT_LEADING_TAGS)


def _agent_resumed_work_verdict_from_activity_entry_after_token_mint(
    entry: Mapping[str, Any], *, decision_point_boundary_marker_on_ancestor_chain: bool
) -> DecisionPointAgentResumedWorkAfterTokenMintVerdict:
    return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
        verdict=DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT,
        evidence_source=EVIDENCE_SOURCE_TRANSCRIPT_ANCESTOR_CHAIN,
        evidence_detail={
            "activity_entry_after_token_mint_type": entry.get("type"),
            "activity_entry_after_token_mint_subtype": entry.get("subtype"),
            "activity_entry_after_token_mint_uuid": entry.get("uuid"),
            "activity_entry_after_token_mint_timestamp": entry.get("timestamp"),
            "decision_point_boundary_marker_on_ancestor_chain": decision_point_boundary_marker_on_ancestor_chain,
        },
    )


def _verdict_from_transcript_ancestor_chain(
    *,
    decision_token: str,
    minted_at_epoch_seconds: float,
    transcript_path: str | Path | None,
) -> DecisionPointAgentResumedWorkAfterTokenMintVerdict | None:
    chain = transcript_ancestor_chain_from_current_leaf(transcript_path=transcript_path)
    if chain.error is not None:
        return None
    entries = chain.entries_from_root_to_leaf
    literal = decision_token_literal(decision_token)
    marker_index: int | None = None
    for index, entry in enumerate(entries):
        is_prompt_entry = entry.get("type") == "user" or entry_is_queued_command_attachment(entry)
        if is_prompt_entry and literal in entry_text(entry):
            marker_index = index
            break  # 最早那一次落地决定它是不是回合中途并入的
    if marker_index is not None and entry_is_queued_command_attachment(entries[marker_index]):
        return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
            verdict=DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT,
            evidence_source=EVIDENCE_SOURCE_TRANSCRIPT_ANCESTOR_CHAIN,
            evidence_detail={
                "decision_point_boundary_marker_absorbed_mid_turn_as_queued_command": True,
                "decision_point_boundary_marker_entry_uuid": entries[marker_index].get("uuid"),
            },
        )
    entries_before_marker = entries[:marker_index] if marker_index is not None else entries
    for entry in reversed(entries_before_marker):
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            # 祖先链在边界处截断（parentUuid=null），刻意不沿 logicalParentUuid 回溯压缩前的历史。auto-compact
            # 只在模型查询中发生 ⇒ 晚于铸出即复工；manual（开发者空闲时手动压缩）/ auto 不晚于铸出 ⇒ 无证据。
            # 必须显式返回，不能落到下面的「标记开回合」。
            compact_metadata = entry.get("compactMetadata")
            boundary_epoch_seconds = entry_timestamp_epoch_seconds(entry)
            if (
                isinstance(compact_metadata, dict)
                and compact_metadata.get("trigger") == "auto"
                and boundary_epoch_seconds is not None
                and boundary_epoch_seconds > minted_at_epoch_seconds
            ):
                return _agent_resumed_work_verdict_from_activity_entry_after_token_mint(
                    entry, decision_point_boundary_marker_on_ancestor_chain=marker_index is not None
                )
            return None
        if entry.get("type") not in ("user", "assistant"):
            continue
        if _transcript_entry_is_transparent_to_agent_resumed_work_scan(entry):
            continue
        activity_epoch_seconds = entry_timestamp_epoch_seconds(entry)
        if activity_epoch_seconds is None:
            return None
        if activity_epoch_seconds > minted_at_epoch_seconds:
            return _agent_resumed_work_verdict_from_activity_entry_after_token_mint(
                entry, decision_point_boundary_marker_on_ancestor_chain=marker_index is not None
            )
        break
    if marker_index is None:
        return None
    return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
        verdict=DECISION_POINT_TURN_OPENED_BY_DECISION_POINT_BOUNDARY_MARKER,
        evidence_source=EVIDENCE_SOURCE_TRANSCRIPT_ANCESTOR_CHAIN,
        evidence_detail={
            "decision_point_boundary_marker_entry_uuid": entries[marker_index].get("uuid"),
        },
    )


def classify_whether_agent_resumed_work_after_decision_token_mint(
    *,
    decision_token: str,
    decision_token_payload: Mapping[str, Any],
    current_hook_event_session_id: str | None,
    current_hook_event_agent_turn_identifier: str | None,
    agent_turn_identifier_of_the_stop_event_that_minted_this_token: str | None,
    transcript_path: str | Path | None,
    user_prompt_submit_arrivals_recorded_for_session: Sequence[Mapping[str, Any]] | None,
) -> DecisionPointAgentResumedWorkAfterTokenMintVerdict:
    """按模块 docstring 的三个证据源给出三态结论。纯判定、只读，绝不抛。"""
    token_session_id = decision_token_payload.get("session_id")
    if not (
        isinstance(token_session_id, str)
        and token_session_id
        and isinstance(current_hook_event_session_id, str)
        and current_hook_event_session_id.strip() == token_session_id
    ):
        return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
            verdict=DECISION_POINT_AGENT_RESUMED_WORK_NO_EVIDENCE_EITHER_WAY,
            evidence_detail={"decision_token_session_does_not_match_current_hook_event_session": True},
        )
    minted_at_epoch_seconds = _parse_timestamp_epoch_seconds(decision_token_payload.get("minted_at"))
    if minted_at_epoch_seconds is None:
        return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
            verdict=DECISION_POINT_AGENT_RESUMED_WORK_NO_EVIDENCE_EITHER_WAY,
            evidence_detail={"decision_token_minted_at_unparseable": True},
        )

    if (
        isinstance(agent_turn_identifier_of_the_stop_event_that_minted_this_token, str)
        and agent_turn_identifier_of_the_stop_event_that_minted_this_token
        and current_hook_event_agent_turn_identifier
        == agent_turn_identifier_of_the_stop_event_that_minted_this_token
    ):
        return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
            verdict=DECISION_POINT_AGENT_RESUMED_WORK_AFTER_TOKEN_MINT,
            evidence_source=EVIDENCE_SOURCE_HOST_AGENT_TURN_IDENTIFIER,
            evidence_detail={
                "agent_turn_identifier_shared_with_the_stop_event_that_minted_this_token": (
                    current_hook_event_agent_turn_identifier
                ),
            },
        )

    from_arrivals = _verdict_from_user_prompt_submit_arrivals(
        decision_token=decision_token,
        minted_at_epoch_seconds=minted_at_epoch_seconds,
        arrivals=user_prompt_submit_arrivals_recorded_for_session or (),
    )
    if from_arrivals is not None:
        return from_arrivals

    from_transcript = _verdict_from_transcript_ancestor_chain(
        decision_token=decision_token,
        minted_at_epoch_seconds=minted_at_epoch_seconds,
        transcript_path=transcript_path,
    )
    if from_transcript is not None:
        return from_transcript
    return DecisionPointAgentResumedWorkAfterTokenMintVerdict(
        verdict=DECISION_POINT_AGENT_RESUMED_WORK_NO_EVIDENCE_EITHER_WAY,
    )
