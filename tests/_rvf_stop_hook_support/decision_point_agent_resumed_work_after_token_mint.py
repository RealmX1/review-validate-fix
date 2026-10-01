#!/usr/bin/env python3
"""带外决策点「铸 token 之后 agent 复工」测试簇（task 8d3ac：复工即撤回 + M13 拆成两支 + 作废标记）。

从 tests/test_stop_review_validate_fix.py 有界拆出的兄弟子模块（与既有子模块同款：模块级 ``inject()``
推入共享 helper，扁平 ``tests=[...]`` 注册表按裸名解析）。带外投递的通用 helper 直接借用
``decision_point_out_of_band_delivery`` 子模块（同一个 aggregator 已先给它 inject 过）。

钉的是开发者 2026-10-01 的设计修正：标记在 agent 回合中途到达 = 铸 token 之后 agent 已复工 = 这份决策点的
范围与时机都过时 ⇒ 撤回、不注入正文、同一次 Stop 按当时的工作树重新评估；只有「标记在空闲时开出了自己的
回合、正文没注入」才补发。每条用例的 docstring 写明它在修复前为什么会红。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _rvf_stop_hook_support.decision_point_out_of_band_delivery import (
    _decision_point_boundary_marker_rendered_by_production,
    _delivery_intents,
    _effective_mint_request_charges,
    _invoke_user_prompt_submit_hook,
    _out_of_band_env,
    _stop_event,
    _write_stub_client,
)

__all__ = [
    'test_decision_point_boundary_marker_absorbed_mid_turn_is_withdrawn_and_the_same_stop_remints_for_the_current_worktree',
    'test_late_boundary_marker_of_a_withdrawn_decision_point_is_blocked_at_user_prompt_submit_without_the_body',
    'test_developer_prompt_quoting_a_void_decision_point_boundary_marker_alongside_its_own_text_is_delivered_untouched_at_user_prompt_submit',
    'test_developer_prompt_quoting_the_boundary_marker_of_an_injected_decision_point_alongside_its_own_text_is_not_withdrawn_as_a_rewind_resubmission',
    'test_developer_prompt_quoting_a_minted_decision_point_boundary_marker_alongside_its_own_text_neither_injects_nor_withdraws_and_counts_as_agent_resumed_work',
    'test_boundary_marker_that_opened_its_own_turn_while_agent_was_idle_is_still_reinjected_when_user_prompt_submit_never_handled_it',
    'test_user_prompt_submit_withdraws_decision_point_when_a_task_notification_arrived_between_mint_and_marker',
    'test_user_prompt_submit_withdraws_decision_point_when_marker_shares_the_agent_turn_of_the_minting_stop',
    'test_codex_stop_continuation_of_the_minting_turn_withdraws_decision_point_but_no_evidence_keeps_it_in_flight',
    'test_consecutive_agent_resumed_work_withdrawals_fall_back_to_self_injection_then_return_to_out_of_band',
    'test_consecutive_agent_resumed_work_withdrawals_spread_across_two_worktrees_of_one_session_still_fall_back_to_self_injection',
    'test_boundary_marker_reinjection_by_stop_loses_to_a_user_prompt_submit_that_already_claimed_the_token',
    'test_agent_resumed_work_withdrawal_compare_and_set_refuses_a_token_already_injected',
    'test_rewind_resubmission_withdrawal_compare_and_set_refuses_a_token_the_agent_already_started',
    'test_agent_resumed_work_evidence_from_another_session_never_withdraws_this_session_decision_point',
    'test_developer_local_slash_command_after_token_mint_is_transparent_to_transcript_ancestor_chain_evidence',
    'test_manual_compact_after_token_mint_yields_no_evidence_rather_than_agent_resumed_work_or_turn_opened_by_marker',
    'test_auto_compact_after_token_mint_still_counts_as_agent_resumed_work_in_transcript_ancestor_chain_evidence',
    'test_is_meta_stop_hook_feedback_after_token_mint_still_counts_as_agent_resumed_work_in_transcript_ancestor_chain_evidence',
    'test_host_adapters_report_agent_turn_identifier_shared_by_hook_events_of_one_turn',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals。"""
    globals().update(deps)


# ── fixtures ──────────────────────────────────────────────────────────────────────────────

AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE = "agent_resumed_work_before_decision_point_delivery_landed"


def _timestamp_offset_from(timestamp: str, seconds: float) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _user_prompt_entry(text: str, timestamp: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": timestamp}


def _assistant_entry(text: str, timestamp: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": timestamp,
    }


def _queued_command_entry(text: str, timestamp: str) -> dict[str, object]:
    """Claude Code 把回合中途并入的排队消息落成 ``queued_command`` 附件（2026-10-02 取证形态）。"""
    return {
        "type": "attachment",
        "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt"},
        "timestamp": timestamp,
    }


def _write_claude_transcript_chain(path: Path, entries: list[dict[str, object]]) -> Path:
    """按顺序串成一条 uuid / parentUuid 链（首条是 ``user`` ⇒ host 探测判 Claude Code）。"""
    lines = []
    parent: str | None = None
    for index, entry in enumerate(entries):
        uuid = f"entry-{index:02d}"
        lines.append(json.dumps({"uuid": uuid, "parentUuid": parent, **entry}, ensure_ascii=False))
        parent = uuid
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _token_records_by_token(state: Path) -> dict[str, dict[str, object]]:
    return {str(record["token"]): record for record in decision_token_records(state)}  # noqa: F821


def _mint_out_of_band_decision_point(
    case: Path, *, task_id: str, session_id: str, **stop_event_fields: object
) -> tuple[Path, Path, dict[str, str], dict[str, object]]:
    """一次真实 Stop：脏 repo → 铸 token + 带外投出边界标记（意图 TTL 默认，即仍在途）。"""
    repo = init_repo(case / "repo", dirty=True)  # noqa: F821
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id=task_id)
    invoke(_stop_event(repo, session_id=session_id, **stop_event_fields), state_dir=state, extra_env=env)  # noqa: F821
    minted = decision_token_records(state)  # noqa: F821
    assert len(minted) == 1 and minted[0]["state"] == "minted", minted
    assert minted[0]["decision_point_delivery_transport"] == "cline_kanban_task_chat_session", minted[0]
    return repo, state, env, minted[0]


def _stop_payload_agent_directive_body(payload: dict[str, object]) -> str:
    return str(
        payload.get("reason")
        or (payload.get("hookSpecificOutput") or {}).get("additionalContext")  # type: ignore[union-attr]
        or ""
    )


@contextmanager
def _isolated_in_process_environment(state: Path, **extra: str):
    """进程内直接调 hook 函数时用：把落盘根指向隔离 state，退出时**逐字**还原（含 KANBAN_*）。"""
    overrides = {
        "RVF_HOME": str(state / "rvf-home"),
        "CODEX_RVF_STATE_DIR": str(state),
        "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(state / "kanban-followup-in-progress"),
        **extra,
    }
    saved = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _load_user_prompt_submit_module_in_process():
    load_hook_module()  # noqa: F821 - 先让 scripts 目录与 pyroot 上 sys.path
    return importlib.import_module("rvf_user_prompt_submit")


# ── 用例 ──────────────────────────────────────────────────────────────────────────────────


def test_decision_point_boundary_marker_absorbed_mid_turn_is_withdrawn_and_the_same_stop_remints_for_the_current_worktree(
    tmp: Path,
) -> None:
    """回合中途到达 ⇒ 撤回 + 不补发 + **同一次** Stop 按当前工作树重铸（也钉住计划 §0.1 的在途闸结论）。

    标记以 ``queued_command`` 并入一个被任务通知唤醒的回合：agent 只看到「未随附即忽略」的一行，而这份
    决策点承载的是铸 token 那一刻的工作树。修复前（M13 原版）下一次 Stop 以同一枚 token 补发过时正文；
    现在必须撤回（新成因、冲销熔断额度），并在同一次 Stop 里按新指纹铸出新 token 走带外——在途闸不区分
    指纹，正是靠「先撤回」才放行的。
    """
    case = tmp / "absorbed-mid-turn"
    repo, state, env, first = _mint_out_of_band_decision_point(
        case, task_id="task-absorbed-mid-turn", session_id="sess-absorbed-mid-turn"
    )
    first_token = str(first["token"])
    minted_at = str(first["minted_at"])
    (repo / "more-work-after-mint.txt").write_text("agent kept working\n", encoding="utf-8")
    marker = _decision_point_boundary_marker_rendered_by_production(first_token)
    transcript = _write_claude_transcript_chain(
        case / "session.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了，先停一下。", _timestamp_offset_from(minted_at, -60)),
            _user_prompt_entry(
                "<task-notification>\n<task-id>bg</task-id>\n</task-notification>",
                _timestamp_offset_from(minted_at, 5),
            ),
            _assistant_entry("后台测试跑完了，继续修。", _timestamp_offset_from(minted_at, 6)),
            _queued_command_entry(marker, _timestamp_offset_from(minted_at, 7)),
            _assistant_entry("（标记未随附正文，忽略）接着改。", _timestamp_offset_from(minted_at, 8)),
        ],
    )
    stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-absorbed-mid-turn", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    payload = parse_json(stdout)  # noqa: F821
    assert "decision" not in payload and "hookSpecificOutput" not in payload, (
        "复工后的过时决策点绝不能再以自注入补发",
        payload,
    )
    records = _token_records_by_token(state)
    assert records[first_token]["state"] == "withdrawn", records[first_token]
    assert records[first_token]["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE, records[first_token]
    remints = [record for token, record in records.items() if token != first_token]
    assert len(remints) == 1, records
    second = remints[0]
    assert second["state"] == "minted", second
    assert second["decision_point_delivery_transport"] == "cline_kanban_task_chat_session", second
    assert second["candidate_fingerprint"] != first["candidate_fingerprint"], "必须按当前工作树重新算候选"
    summary = latest_summary(state)  # noqa: F821
    assert summary["reason_code"] == "decision_point_delivered_out_of_band", summary
    # 撤回冲销了第一枚的额度，只剩新铸那一枚占着熔断。
    charges = _effective_mint_request_charges(state)
    assert len(charges) == 1, charges
    intents = _delivery_intents(state)
    assert [intent["decision_token"] for intent in intents] == [second["token"]], intents
    names = [event.get("event") for event in latest_events(state)]  # noqa: F821
    assert (
        "decision_point_out_of_band_delivery_withdrawn_because_agent_resumed_work_after_token_mint" in names
    ), names
    assert "decision_point_boundary_marker_landed_without_banner_body_reinjected_by_stop" not in names, names


def test_late_boundary_marker_of_a_withdrawn_decision_point_is_blocked_at_user_prompt_submit_without_the_body(
    tmp: Path,
) -> None:
    """复工撤回之后，旧标记迟到落地 ⇒ UPS 走作废出口：block + 「已作废、无需处理」，**不含正文**。

    且这条作废标记的到达不算新 token 的复工（否则一份迟到标记会把下一枚也撤掉、自我续命）：随后新 token
    的标记在空闲时到达，照常注入正文。修复前 UPS 对已撤回 token 只出一句 systemMessage、不挡提交。
    """
    case = tmp / "late-void-marker"
    repo, state, env, first = _mint_out_of_band_decision_point(
        case, task_id="task-late-void-marker", session_id="sess-late-void-marker"
    )
    first_token = str(first["token"])
    minted_at = str(first["minted_at"])
    (repo / "more-work-after-mint.txt").write_text("agent kept working\n", encoding="utf-8")
    resumed_transcript = _write_claude_transcript_chain(
        case / "resumed.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -60)),
            _user_prompt_entry("再顺手改一下文档", _timestamp_offset_from(minted_at, 3)),
            _assistant_entry("改好了。", _timestamp_offset_from(minted_at, 4)),
        ],
    )
    invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-late-void-marker", transcript_path=str(resumed_transcript)),
        state_dir=state,
        extra_env=env,
    )
    records = _token_records_by_token(state)
    assert records[first_token]["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE, records
    second_token = next(token for token in records if token != first_token)
    second_minted_at = str(records[second_token]["minted_at"])

    idle_transcript = _write_claude_transcript_chain(
        case / "idle.jsonl",
        [
            _user_prompt_entry("再顺手改一下文档", _timestamp_offset_from(second_minted_at, -30)),
            _assistant_entry("改好了。", _timestamp_offset_from(second_minted_at, -20)),
        ],
    )
    late = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-late-void-marker",
            "hook_event_name": "UserPromptSubmit",
            "prompt": _decision_point_boundary_marker_rendered_by_production(first_token),
            "prompt_id": "prompt-late-void-marker",
            "transcript_path": str(idle_transcript),
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-late-void-marker"},
    )
    assert late.get("decision") == "block", late
    void_context = str((late.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    assert "已作废" in void_context and first_token in void_context, void_context
    stop_hook = load_hook_module()  # noqa: F821
    assert stop_hook.DECISION_POINT_BANNER_TITLE_LINE not in void_context, "作废出口绝不带决策正文"
    assert "已作废" in str(late.get("systemMessage") or ""), late

    on_time = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-late-void-marker",
            "hook_event_name": "UserPromptSubmit",
            "prompt": _decision_point_boundary_marker_rendered_by_production(second_token),
            "prompt_id": "prompt-second-marker-opens-its-own-turn",
            "transcript_path": str(idle_transcript),
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-late-void-marker"},
    )
    body = str((on_time.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), on_time
    assert "decision" not in on_time, on_time
    assert _token_records_by_token(state)[second_token]["state"] == "injected"


def _claude_code_pasted_content_envelope(pasted_text: str, envelope_identifier: str) -> str:
    """cline-kanban 粘贴进 Claude Code（≥2.1.278）后 UPS ``prompt`` 的**真实形态**。

    取证（2026-10-02）：本机 round-baseline 记录里 UPS 收到的 kanban 投递 prompt 逐字以
    ``\\n\\n<pasted_content id="…">\\n`` 起头、以 ``\\n</pasted_content id="…">\\n`` 收尾；单行边界标记的
    A/B transcript 是同一形态。机器投递的边界标记到 UPS 时就长这样，判定必须认得它。
    """
    return (
        f'\n\n<pasted_content id="{envelope_identifier}">\n{pasted_text}\n'
        f'</pasted_content id="{envelope_identifier}">\n'
    )


def _user_prompt_submit_event_for_out_of_band_case(
    repo: Path, *, session_id: str, prompt: str, prompt_id: str, transcript_path: Path
) -> dict[str, object]:
    return {
        "cwd": str(repo),
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "prompt_id": prompt_id,
        "transcript_path": str(transcript_path),
    }


def test_developer_prompt_quoting_a_void_decision_point_boundary_marker_alongside_its_own_text_is_delivered_untouched_at_user_prompt_submit(
    tmp: Path,
) -> None:
    """RVF-001：只有「整条 prompt 就是生产渲染的那一行标记」（容许 host 粘贴包裹）才走作废出口。

    开发者粘一段含旧标记的日志 / handoff、同一条里再写自己的指令 ⇒ 按开发者 prompt 原样放行：不 block、
    不注入「已作废」、不碰 token。修复前 UPS 在整段 prompt 里子串搜 ``RVF_DECISION_TOKEN=<token>``，
    token 已撤回 ⇒ ``decision:block`` 整条丢掉，agent 只看到「已作废」。阳性对照：机器投递的同一行标记
    被 Claude Code 包进 ``<pasted_content>`` 后，仍须走作废出口。
    """
    case = tmp / "developer-prompt-quoting-void-marker"
    session_id = "sess-developer-prompt-quoting-void-marker"
    repo, state, env, first = _mint_out_of_band_decision_point(
        case, task_id="task-developer-prompt-quoting-void-marker", session_id=session_id
    )
    first_token = str(first["token"])
    minted_at = str(first["minted_at"])
    (repo / "more-work-after-mint.txt").write_text("agent kept working\n", encoding="utf-8")
    resumed_transcript = _write_claude_transcript_chain(
        case / "resumed.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -60)),
            _user_prompt_entry("再顺手改一下文档", _timestamp_offset_from(minted_at, 3)),
            _assistant_entry("改好了。", _timestamp_offset_from(minted_at, 4)),
        ],
    )
    invoke(  # noqa: F821
        _stop_event(repo, session_id=session_id, transcript_path=str(resumed_transcript)),
        state_dir=state,
        extra_env=env,
    )
    first_record_before = _token_records_by_token(state)[first_token]
    assert first_record_before["state"] == "withdrawn", first_record_before

    idle_transcript = _write_claude_transcript_chain(
        case / "idle.jsonl",
        [
            _user_prompt_entry("再顺手改一下文档", _timestamp_offset_from(minted_at, 10)),
            _assistant_entry("改好了。", _timestamp_offset_from(minted_at, 11)),
        ],
    )
    marker = _decision_point_boundary_marker_rendered_by_production(first_token)
    developer_prompts_quoting_the_marker = (
        f"这行旧标记是什么意思？顺便把 README 里的错别字改掉。\n{marker}",
        f"{marker}\n上面这行是之前粘过来的日志，不用管它，继续把测试补齐。",
        "帮我看看这段粘贴：" + _claude_code_pasted_content_envelope(marker, "a1b2"),
    )
    for index, developer_prompt in enumerate(developer_prompts_quoting_the_marker):
        result = _invoke_user_prompt_submit_hook(
            _user_prompt_submit_event_for_out_of_band_case(
                repo,
                session_id=session_id,
                prompt=developer_prompt,
                prompt_id=f"prompt-developer-quotes-void-marker-{index}",
                transcript_path=idle_transcript,
            ),
            state_dir=state,
            extra_env={"KANBAN_TASK_ID": "task-developer-prompt-quoting-void-marker"},
        )
        assert "decision" not in result, ("开发者 prompt 绝不能被作废出口整条挡掉", developer_prompt, result)
        additional_context = str((result.get("hookSpecificOutput") or {}).get("additionalContext") or "")
        assert "已作废" not in additional_context, (developer_prompt, result)
        assert _token_records_by_token(state)[first_token] == first_record_before, "开发者 prompt 不得改动 token"

    machine_delivered = _invoke_user_prompt_submit_hook(
        _user_prompt_submit_event_for_out_of_band_case(
            repo,
            session_id=session_id,
            prompt=_claude_code_pasted_content_envelope(marker, "3f14"),
            prompt_id="prompt-late-machine-delivered-void-marker",
            transcript_path=idle_transcript,
        ),
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-developer-prompt-quoting-void-marker"},
    )
    assert machine_delivered.get("decision") == "block", (
        "被 host 粘贴包裹的机器投递标记仍是一条独立标记，作废出口照挡",
        machine_delivered,
    )


def test_developer_prompt_quoting_the_boundary_marker_of_an_injected_decision_point_alongside_its_own_text_is_not_withdrawn_as_a_rewind_resubmission(
    tmp: Path,
) -> None:
    """RVF-001 · M10 变体：token 停在 injected、首次注入凭证在场，开发者引用它的标记并附带自己的话 ⇒ 不撤回。

    修复前子串命中就进「rewind 重交」分支，把一份 agent 正在斟酌的有效征询 ``mark_withdrawn(rewound_by_user)``。
    同时钉住两件事：机器投递的标记被 Claude Code 包进 ``<pasted_content>`` 后照常首次注入正文；真正的
    rewind 重交（整条只有那一行）照旧撤回——证明本用例确实走到了 M10 的前提。
    """
    case = tmp / "developer-prompt-quoting-injected-marker"
    session_id = "sess-developer-prompt-quoting-injected-marker"
    task_id = "task-developer-prompt-quoting-injected-marker"
    repo, state, _env, minted = _mint_out_of_band_decision_point(case, task_id=task_id, session_id=session_id)
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    idle_transcript = _write_claude_transcript_chain(
        case / "idle.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -30)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -20)),
        ],
    )
    marker = _decision_point_boundary_marker_rendered_by_production(token)

    def submit(prompt: str, prompt_id: str) -> dict[str, object]:
        return _invoke_user_prompt_submit_hook(
            _user_prompt_submit_event_for_out_of_band_case(
                repo, session_id=session_id, prompt=prompt, prompt_id=prompt_id, transcript_path=idle_transcript
            ),
            state_dir=state,
            extra_env={"KANBAN_TASK_ID": task_id},
        )

    delivered = submit(_claude_code_pasted_content_envelope(marker, "3f14"), "prompt-marker-opens-its-own-turn")
    stop_hook = load_hook_module()  # noqa: F821
    body = str((delivered.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), ("包裹形态的机器投递照常注入正文", delivered)
    assert _token_records_by_token(state)[token]["state"] == "injected"

    quoting = submit(
        f"刚才那条决策点我看到了：\n{marker}\n你先别管它，把失败的测试修好。",
        "prompt-developer-quotes-injected-marker",
    )
    assert "decision" not in quoting and "hookSpecificOutput" not in quoting, quoting
    assert _token_records_by_token(state)[token]["state"] == "injected", (
        "开发者引用标记不是 rewind 重交，不得撤回一份有效征询"
    )

    submit(marker, "prompt-marker-resubmitted-after-rewind")
    record = _token_records_by_token(state)[token]
    assert record["state"] == "withdrawn", record
    assert record["withdrawal_cause"] == "rewound_by_user", record


def test_developer_prompt_quoting_a_minted_decision_point_boundary_marker_alongside_its_own_text_neither_injects_nor_withdraws_and_counts_as_agent_resumed_work(
    tmp: Path,
) -> None:
    """RVF-001 · 复工撤回 + E1 变体：token 仍是 minted，开发者引用它的标记并附带自己的话。

    这条是开发者发话（真实复工），不是标记落地：不注入正文、不撤回，并以 ``developer_prompt`` 记进 E1。
    修复前它被当成「本 token 的标记」：正文被注入进一条开发者 prompt，E1 也把它记成本 token 自己的标记、
    判据跳过它。于是随后真正的标记落地时，复工证据缺席。修复后那一行标记到达即按「铸出之后 agent 已复工」
    撤回并走作废出口。
    """
    case = tmp / "developer-prompt-quoting-minted-marker"
    session_id = "sess-developer-prompt-quoting-minted-marker"
    task_id = "task-developer-prompt-quoting-minted-marker"
    repo, state, _env, minted = _mint_out_of_band_decision_point(case, task_id=task_id, session_id=session_id)
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    idle_transcript = _write_claude_transcript_chain(
        case / "idle.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -30)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -20)),
        ],
    )
    marker = _decision_point_boundary_marker_rendered_by_production(token)

    def submit(prompt: str, prompt_id: str) -> dict[str, object]:
        return _invoke_user_prompt_submit_hook(
            _user_prompt_submit_event_for_out_of_band_case(
                repo, session_id=session_id, prompt=prompt, prompt_id=prompt_id, transcript_path=idle_transcript
            ),
            state_dir=state,
            extra_env={"KANBAN_TASK_ID": task_id},
        )

    quoting = submit(f"{marker}\n这行是什么？先解释一下再继续。", "prompt-developer-quotes-minted-marker")
    assert "decision" not in quoting and "hookSpecificOutput" not in quoting, (
        "开发者 prompt 里既不能被注入决策正文，也不能被挡",
        quoting,
    )
    assert _token_records_by_token(state)[token]["state"] == "minted"

    delivered = submit(marker, "prompt-marker-after-developer-spoke")
    assert delivered.get("decision") == "block", delivered
    record = _token_records_by_token(state)[token]
    assert record["state"] == "withdrawn", record
    assert record["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE, record


def test_boundary_marker_that_opened_its_own_turn_while_agent_was_idle_is_still_reinjected_when_user_prompt_submit_never_handled_it(
    tmp: Path,
) -> None:
    """M13 保留支（反例：标记自己开启的回合**不得**误判为复工）。

    标记在 agent 空闲时开出了自己的回合（链上它之前最近的活动早于铸出时刻），UPS 没处理它 ⇒ 下一次 Stop
    以同一枚 token 补发完整正文，不撤回、不重铸、不动熔断额度。
    """
    case = tmp / "marker-opened-its-own-turn"
    repo, state, env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-marker-opened-turn", session_id="sess-marker-opened-turn"
    )
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    marker = _decision_point_boundary_marker_rendered_by_production(token)
    transcript = _write_claude_transcript_chain(
        case / "session.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了，先停一下。", _timestamp_offset_from(minted_at, -60)),
            _user_prompt_entry(marker, _timestamp_offset_from(minted_at, 30)),
            _assistant_entry("（标记未随附正文，按说明忽略）", _timestamp_offset_from(minted_at, 31)),
        ],
    )
    stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-marker-opened-turn", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    payload = parse_json(stdout)  # noqa: F821
    body = _stop_payload_agent_directive_body(payload)
    stop_hook = load_hook_module()  # noqa: F821
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), payload
    assert f"RVF_DECISION_TOKEN={token}" in body, "补发的必须是同一枚 token 的完整正文"
    records = decision_token_records(state)  # noqa: F821
    assert [record["token"] for record in records] == [token], "不得另铸新 token"
    assert records[0]["state"] == "injected", records[0]
    assert _delivery_intents(state) == [], "补发之后这份投递意图已无对账价值"
    assert len(_effective_mint_request_charges(state)) == 1, "不冲销、也不新增熔断额度"
    reinjected = [
        event
        for event in latest_events(state)  # noqa: F821
        if event.get("event") == "decision_point_boundary_marker_landed_without_banner_body_reinjected_by_stop"
    ]
    assert len(reinjected) == 1, latest_events(state)  # noqa: F821
    assert reinjected[0]["decision_point_agent_resumed_work_verdict"] == (
        "turn_opened_by_decision_point_boundary_marker"
    ), reinjected[0]


def test_user_prompt_submit_withdraws_decision_point_when_a_task_notification_arrived_between_mint_and_marker(
    tmp: Path,
) -> None:
    """UPS 侧（E1）：铸出之后先到了一条后台任务通知（agent 被唤醒复工），标记随后到达 ⇒ 撤回、不注入。

    这条刻意让 transcript 里**没有**铸点之后的活动，只靠 UPS 的到达记录判：任务通知是合成回合、会在 UPS 里
    整段短路，到达记录必须写在短路之前。修复前标记首次到达一律注入正文。
    """
    case = tmp / "task-notification-then-marker"
    repo, state, _env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-notification-then-marker", session_id="sess-notification-then-marker"
    )
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    quiet_transcript = _write_claude_transcript_chain(
        case / "session.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -60)),
        ],
    )
    ups_env = {"KANBAN_TASK_ID": "task-notification-then-marker"}
    notification = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-notification-then-marker",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "<task-notification>\n<task-id>bg-tests</task-id>\n</task-notification>",
            "prompt_id": "prompt-woken-by-task-notification",
            "transcript_path": str(quiet_transcript),
        },
        state_dir=state,
        extra_env=ups_env,
    )
    assert "decision" not in notification, notification
    arrived = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-notification-then-marker",
            "hook_event_name": "UserPromptSubmit",
            "prompt": _decision_point_boundary_marker_rendered_by_production(token),
            "prompt_id": "prompt-woken-by-task-notification",
            "transcript_path": str(quiet_transcript),
        },
        state_dir=state,
        extra_env=ups_env,
    )
    assert arrived.get("decision") == "block", arrived
    void_context = str((arrived.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    stop_hook = load_hook_module()  # noqa: F821
    assert stop_hook.DECISION_POINT_BANNER_TITLE_LINE not in void_context, "复工后绝不注入决策正文"
    assert "已作废" in void_context, void_context
    record = _token_records_by_token(state)[token]
    assert record["state"] == "withdrawn", record
    assert record["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE, record
    assert _delivery_intents(state) == [], "撤回之后投递意图已无对账价值"
    assert _effective_mint_request_charges(state) == [], "复工撤回冲销熔断额度"


def test_user_prompt_submit_withdraws_decision_point_when_marker_shares_the_agent_turn_of_the_minting_stop(
    tmp: Path,
) -> None:
    """UPS 侧（E3）：标记的 UPS 与铸 token 的那次 Stop 属于**同一个回合**（Claude Code ``prompt_id`` 相同）。

    这就是「铸 token 之后那个回合被续跑（别的 Stop hook block 了它），标记在续跑中途并入」——没有任何新的
    UPS 到达，transcript 也故意不给证据，只剩回合标识能认出来。修复前照常注入正文。
    """
    case = tmp / "marker-shares-minting-turn"
    claude_transcript_at_mint = case / "mint-turn.jsonl"
    case.mkdir(parents=True, exist_ok=True)
    earlier = _utc_now_timestamp()
    _write_claude_transcript_chain(
        claude_transcript_at_mint,
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(earlier, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(earlier, -60)),
        ],
    )
    repo, state, _env, minted = _mint_out_of_band_decision_point(
        case,
        task_id="task-marker-shares-minting-turn",
        session_id="sess-marker-shares-minting-turn",
        prompt_id="prompt-minting-turn",
        transcript_path=str(claude_transcript_at_mint),
    )
    token = str(minted["token"])
    assert minted["agent_turn_identifier_of_the_stop_event_that_minted_this_token"] == "prompt-minting-turn", minted
    arrived = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-marker-shares-minting-turn",
            "hook_event_name": "UserPromptSubmit",
            "prompt": _decision_point_boundary_marker_rendered_by_production(token),
            "prompt_id": "prompt-minting-turn",
            "transcript_path": str(claude_transcript_at_mint),
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-marker-shares-minting-turn"},
    )
    assert arrived.get("decision") == "block", arrived
    assert "证据=host_agent_turn_identifier" in str(arrived.get("systemMessage") or ""), arrived
    assert _token_records_by_token(state)[token]["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE


def test_codex_stop_continuation_of_the_minting_turn_withdraws_decision_point_but_no_evidence_keeps_it_in_flight(
    tmp: Path,
) -> None:
    """Codex（E3）：续跑 Stop 与铸 token 的 Stop 共享 ``turn_id`` ⇒ 撤回；拿不到证据 ⇒ 维持在途。

    Codex 的 Stop 续跑不触发 UPS、rollout 也没有 uuid 链，回合标识是唯一证据，而续跑 Stop 带
    ``stop_hook_active=true``、会被递归短路——所以撤回必须发生在短路分支里。修复前短路分支什么都不做。
    无证据那一半钉住「无证据 ⇒ 维持现状」：否则 UPS 失效的 Codex 会话会无限「铸 → 撤」。
    """
    case = tmp / "codex-continuation"
    repo, state, env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-codex-continuation", session_id="sess-codex-continuation", turn_id="turn-minting"
    )
    token = str(minted["token"])
    assert minted["agent_turn_identifier_of_the_stop_event_that_minted_this_token"] == "turn-minting", minted

    no_evidence_stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-codex-continuation", turn_id="turn-unrelated"),
        state_dir=state,
        extra_env=env,
    )
    assert latest_summary(state)["reason_code"] == "decision_point_out_of_band_delivery_in_flight", (  # noqa: F821
        no_evidence_stdout
    )
    assert _token_records_by_token(state)[token]["state"] == "minted"

    continuation_stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-codex-continuation", turn_id="turn-minting", stop_hook_active=True),
        state_dir=state,
        extra_env=env,
    )
    assert latest_summary(state)["reason_code"] == "stop_hook_active", continuation_stdout  # noqa: F821
    record = _token_records_by_token(state)[token]
    assert record["state"] == "withdrawn", record
    assert record["withdrawal_cause"] == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE, record
    assert "decision" not in parse_json(continuation_stdout), "递归短路分支只撤回、绝不驱动 agent"  # noqa: F821
    assert _delivery_intents(state) == []


def test_consecutive_agent_resumed_work_withdrawals_fall_back_to_self_injection_then_return_to_out_of_band(
    tmp: Path,
) -> None:
    """空转上界：连续 3 份带外横幅都因复工被撤回 ⇒ 第 4 份改走同步自注入；自注入之后恢复带外。

    修复前根本没有复工撤回（在途闸把每次 Stop 静默挡住），这条也就无从发生；有了撤回而没有上界时，一个
    总在横幅落地前复工的会话会无限「铸 → 撤 → 重铸」（冲销让熔断也兜不住）。
    """
    case = tmp / "idle-cap"
    repo, state, env, _first = _mint_out_of_band_decision_point(
        case, task_id="task-idle-cap", session_id="sess-idle-cap"
    )
    stop_hook = load_hook_module()  # noqa: F821
    threshold = stop_hook.CONSECUTIVE_AGENT_RESUMED_WORK_WITHDRAWALS_BEFORE_FALLING_BACK_TO_STOP_HOOK_SELF_INJECTION
    assert threshold == 3

    def stop_after_agent_resumed_work(round_index: int) -> dict[str, object]:
        transcript = _write_claude_transcript_chain(
            case / f"session-{round_index}.jsonl",
            [_assistant_entry(f"第 {round_index} 次复工", _utc_now_timestamp())],
        )
        stdout, _stderr = invoke(  # noqa: F821
            _stop_event(repo, session_id="sess-idle-cap", transcript_path=str(transcript)),
            state_dir=state,
            extra_env=env,
        )
        return parse_json(stdout)  # noqa: F821

    for round_index in range(1, threshold):
        payload = stop_after_agent_resumed_work(round_index)
        assert "hookSpecificOutput" not in payload and "decision" not in payload, (round_index, payload)
        assert latest_summary(state)["reason_code"] == "decision_point_delivered_out_of_band", round_index  # noqa: F821

    payload = stop_after_agent_resumed_work(threshold)
    body = _stop_payload_agent_directive_body(payload)
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), payload
    assert "空转上界" in str(payload.get("systemMessage") or ""), payload
    records = decision_token_records(state)  # noqa: F821
    withdrawn = [record for record in records if record.get("withdrawal_cause") == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE]
    assert len(withdrawn) == threshold, records
    self_injected = [
        record for record in records if record["decision_point_delivery_transport"] == "stop_hook_self_injection"
    ]
    assert len(self_injected) == 1 and self_injected[0]["state"] == "injected", records

    (repo / "after-self-injection.txt").write_text("new work\n", encoding="utf-8")
    payload = stop_after_agent_resumed_work(threshold + 1)
    assert "hookSpecificOutput" not in payload and "decision" not in payload, payload
    assert latest_summary(state)["reason_code"] == "decision_point_delivered_out_of_band"  # noqa: F821


def test_consecutive_agent_resumed_work_withdrawals_spread_across_two_worktrees_of_one_session_still_fall_back_to_self_injection(
    tmp: Path,
) -> None:
    """空转上界按**会话**计数：同一会话的连续 Stop 轮流落在 A / B 两个 worktree 上，仍在第 3 份撤回后改走自注入。

    复工撤回本来就按会话跨 worktree 枚举；修复前空转上界却只数当前 worktree 的 token，交替落点会把撤回
    序列拆成每个 worktree 各 1–2 枚，永远到不了阈值 ⇒ 同步自注入这道兜底被无限绕过（这条在修复前红在
    「第 3 次撤回后仍走带外」）。后半段同时钉住：另一个 worktree 里自注入铸出的 token（minted_at 最新）
    照样打断本会话的连续计数，之后恢复带外。
    """
    case = tmp / "idle-cap-across-worktrees"
    session_id = "sess-idle-cap-across-worktrees"
    repo_a, state, env, _first = _mint_out_of_band_decision_point(
        case, task_id="task-idle-cap-across-worktrees", session_id=session_id
    )
    repo_b = init_repo(case / "repo-b", dirty=True)  # noqa: F821
    stop_hook = load_hook_module()  # noqa: F821
    threshold = stop_hook.CONSECUTIVE_AGENT_RESUMED_WORK_WITHDRAWALS_BEFORE_FALLING_BACK_TO_STOP_HOOK_SELF_INJECTION
    assert threshold == 3

    def stop_after_agent_resumed_work(repo: Path, round_index: int) -> dict[str, object]:
        transcript = _write_claude_transcript_chain(
            case / f"session-{round_index}.jsonl",
            [_assistant_entry(f"第 {round_index} 次复工", _utc_now_timestamp())],
        )
        stdout, _stderr = invoke(  # noqa: F821
            _stop_event(repo, session_id=session_id, transcript_path=str(transcript)),
            state_dir=state,
            extra_env=env,
        )
        return parse_json(stdout)  # noqa: F821

    # 第 1 枚铸在 A；之后 B → A 交替复工：每次撤回上一枚（跨 worktree）、再在本 worktree 铸新的一枚。
    for round_index, repo in enumerate([repo_b, repo_a][: threshold - 1], start=1):
        payload = stop_after_agent_resumed_work(repo, round_index)
        assert "hookSpecificOutput" not in payload and "decision" not in payload, (round_index, payload)
        assert latest_summary(state)["reason_code"] == "decision_point_delivered_out_of_band", round_index  # noqa: F821

    payload = stop_after_agent_resumed_work(repo_b, threshold)
    body = _stop_payload_agent_directive_body(payload)
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), payload
    assert "空转上界" in str(payload.get("systemMessage") or ""), payload
    records = decision_token_records(state)  # noqa: F821
    withdrawn = [record for record in records if record.get("withdrawal_cause") == AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE]
    assert len(withdrawn) == threshold, records
    assert len({record.get("worktree") for record in withdrawn}) == 2, withdrawn
    self_injected = [
        record for record in records if record["decision_point_delivery_transport"] == "stop_hook_self_injection"
    ]
    assert len(self_injected) == 1 and self_injected[0]["state"] == "injected", records

    (repo_a / "after-self-injection.txt").write_text("new work\n", encoding="utf-8")
    payload = stop_after_agent_resumed_work(repo_a, threshold + 1)
    assert "hookSpecificOutput" not in payload and "decision" not in payload, payload
    assert latest_summary(state)["reason_code"] == "decision_point_delivered_out_of_band"  # noqa: F821


def test_boundary_marker_reinjection_by_stop_loses_to_a_user_prompt_submit_that_already_claimed_the_token(
    tmp: Path,
) -> None:
    """M13 补发用锁内首次迁移的 CAS：UPS 已 claim 并把正文放进输出、还没写首次到达记录时，Stop 不再注入第二份。

    进程内复现那个窗口：先让 M13 的查找函数在 token 还是 minted 时选中它，再模拟 UPS 抢先
    ``minted → injected``，然后跑补发。修复前补发走幂等的 ``mark_injected``，照样把正文再注入一次。
    """
    case = tmp / "reinjection-loses-to-ups"
    repo, state, _env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-reinjection-loses", session_id="sess-reinjection-loses"
    )
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    transcript = _write_claude_transcript_chain(
        case / "session.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -60)),
            _user_prompt_entry(
                _decision_point_boundary_marker_rendered_by_production(token),
                _timestamp_offset_from(minted_at, 30),
            ),
        ],
    )
    event = {
        **_stop_event(repo, session_id="sess-reinjection-loses", transcript_path=str(transcript)),
        "kanban_task_id": "task-reinjection-loses",
    }
    with _isolated_in_process_environment(state):
        stop_hook = load_hook_module()  # noqa: F821
        landed = stop_hook.find_out_of_band_decision_point_boundary_marker_landed_without_banner_body_injection(
            event, cwd=str(repo), worktree=str(repo)
        )
        assert landed is not None, "前提：查找时 token 仍是 minted、标记开了自己的回合"
        claim = stop_hook.decision_token_store.claim_first_minted_to_injected_transition_exclusively(token)
        assert claim.this_call_won_first_minted_to_injected_transition, "模拟 UPS 抢先推进"
        decision = stop_hook._decision_point_reinject_banner_body_after_boundary_marker_landed_without_injection(
            landed,
            event=event,
            ledger=stop_hook.start_stop_hook_ledger(event),
            repo=str(repo),
            cwd=str(repo),
            gate_status="DIRTY",
            fork_context=None,
        )
    assert decision.agent_directive is None, "UPS 已注入正文 ⇒ Stop 绝不再注入第二份"
    assert decision.reason_code == (
        "decision_point_self_injection_skipped_because_token_already_advanced_or_unadvanceable"
    ), decision


def test_agent_resumed_work_withdrawal_compare_and_set_refuses_a_token_already_injected(tmp: Path) -> None:
    """复工撤回是 CAS：锁外读到 minted、锁内已被 UPS 推成 injected ⇒ 不撤（agent 已拿到正文）。"""
    case = tmp / "withdrawal-cas"
    _repo, state, _env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-withdrawal-cas", session_id="sess-withdrawal-cas"
    )
    token = str(minted["token"])
    with _isolated_in_process_environment(state):
        store = load_hook_module().decision_token_store  # noqa: F821
        store.claim_first_minted_to_injected_transition_exclusively(token)
        try:
            store.mark_withdrawn(
                token,
                reason="agent_resumed_work_after_decision_token_mint",
                withdrawal_cause=AGENT_RESUMED_WORK_WITHDRAWAL_CAUSE,
                withdraw_only_if_state_inside_lock_is=frozenset({store.STATE_MINTED}),
            )
        except store.DecisionTokenWithdrawalPreconditionNotMetError:
            pass
        else:
            raise AssertionError("锁内已是 injected，复工撤回必须拒绝")
    assert _token_records_by_token(state)[token]["state"] == "injected"


def test_rewind_resubmission_withdrawal_compare_and_set_refuses_a_token_the_agent_already_started(
    tmp: Path,
) -> None:
    """M10 改 CAS：UPS 锁外读到 injected 判为「rewind 后重交」，进锁时 agent 已 ``start`` ⇒ 不得撤回。

    ``acknowledged → withdrawn`` 在状态机上合法（用户可以在 start 之后 rewind），所以只校验迁移合法性挡不住
    这个窗口。修复前这里会把一轮已被接受的 run 撤掉、``rvf start`` 之后的推进全部硬失败。
    """
    case = tmp / "rewind-resubmission-cas"
    repo, state, _env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-rewind-cas", session_id="sess-rewind-cas"
    )
    token = str(minted["token"])
    with _isolated_in_process_environment(state):
        ups = _load_user_prompt_submit_module_in_process()
        store = importlib.import_module("rvf_decision_token_store")
        store.claim_first_minted_to_injected_transition_exclusively(token)
        stale_lookup_outside_lock = store.read_decision_token(token)
        assert stale_lookup_outside_lock.state == store.STATE_INJECTED
        assert ups._record_decision_point_banner_first_injection_handoff_completed_credential(store, token)
        store.acknowledge_decision_token(token)  # agent 在两次读之间 start 了
        real_read_decision_token = store.read_decision_token
        calls = {"count": 0}

        def read_returning_stale_snapshot_first(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return stale_lookup_outside_lock
            return real_read_decision_token(*args, **kwargs)

        store.read_decision_token = read_returning_stale_snapshot_first
        try:
            result = ups._observe_delivered_decision_point_banner(
                {"cwd": str(repo), "session_id": "sess-rewind-cas", "kanban_task_id": "task-rewind-cas"},
                token,
                payload={"continue": True},
            )
        finally:
            store.read_decision_token = real_read_decision_token
    assert "decision_token_withdrawal_after_marker_resubmission_error" in result, result
    assert "PreconditionNotMet" in str(result["decision_token_withdrawal_after_marker_resubmission_error"]), result
    assert _token_records_by_token(state)[token]["state"] == "acknowledged", "一轮已接受的 run 绝不能被撤回"


def test_agent_resumed_work_evidence_from_another_session_never_withdraws_this_session_decision_point(
    tmp: Path,
) -> None:
    """跨会话：同一 worktree / task 里另一个会话复工了，不撤回本会话的横幅；它的到达也不算本会话的复工。"""
    case = tmp / "cross-session"
    repo, state, env, minted = _mint_out_of_band_decision_point(
        case, task_id="task-cross-session", session_id="sess-owner"
    )
    token = str(minted["token"])
    minted_at = str(minted["minted_at"])
    other_session_transcript = _write_claude_transcript_chain(
        case / "other-session.jsonl",
        [
            _user_prompt_entry("另一个会话里的活", _timestamp_offset_from(minted_at, 2)),
            _assistant_entry("在干活", _timestamp_offset_from(minted_at, 3)),
        ],
    )
    _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-other",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "另一个会话里的活",
            "prompt_id": "prompt-other-session",
            "transcript_path": str(other_session_transcript),
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-cross-session"},
    )
    invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-other", transcript_path=str(other_session_transcript)),
        state_dir=state,
        extra_env=env,
    )
    assert _token_records_by_token(state)[token]["state"] == "minted", "别的会话复工不撤回本会话的横幅"

    owner_transcript = _write_claude_transcript_chain(
        case / "owner-session.jsonl",
        [
            _user_prompt_entry("实现一下", _timestamp_offset_from(minted_at, -120)),
            _assistant_entry("做完了。", _timestamp_offset_from(minted_at, -60)),
        ],
    )
    arrived = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-owner",
            "hook_event_name": "UserPromptSubmit",
            "prompt": _decision_point_boundary_marker_rendered_by_production(token),
            "prompt_id": "prompt-owner-marker",
            "transcript_path": str(owner_transcript),
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-cross-session"},
    )
    stop_hook = load_hook_module()  # noqa: F821
    body = str((arrived.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), arrived
    assert _token_records_by_token(state)[token]["state"] == "injected"


# ── E2 回扫：开发者本地斜杠命令 / compact 边界（RVF-002）──────────────────────────────────────
# 形态取自 2026-10-01 本机 Claude Code transcript：Stop 铸出后 60ms 开发者执行了 ``/compact``。

E2_FIXTURE_DECISION_TOKEN_MINTED_AT = "2026-10-01T19:39:17.558Z"
E2_FIXTURE_DECISION_TOKEN = "e2fixturetoken01"
E2_FIXTURE_SESSION_ID = "sess-e2-transcript-ancestor-chain"


def _e2_minted_at_offset(seconds: float) -> str:
    return _timestamp_offset_from(E2_FIXTURE_DECISION_TOKEN_MINTED_AT, seconds)


def _is_meta_user_entry(text: str, timestamp: str) -> dict[str, object]:
    return {**_user_prompt_entry(text, timestamp), "isMeta": True}


def _compact_boundary_entry(trigger: str, timestamp: str) -> dict[str, object]:
    """真实形态：``parentUuid=null``（祖先链在此截断）+ ``logicalParentUuid`` 指向压缩前的 leaf。"""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "parentUuid": None,
        "logicalParentUuid": "entry-01",
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": trigger},
        "timestamp": timestamp,
    }


def _compact_summary_entry(timestamp: str) -> dict[str, object]:
    return {
        **_user_prompt_entry("This session is being continued from a previous conversation.", timestamp),
        "isCompactSummary": True,
    }


def _pre_compact_history_ending_before_token_mint() -> list[dict[str, object]]:
    return [
        _user_prompt_entry("实现一下", _e2_minted_at_offset(-120)),
        _assistant_entry("做完了，先停一下。", _e2_minted_at_offset(-60)),
    ]


def _e2_verdicts_at_user_prompt_submit_and_at_stop(case: Path, entries_before_marker: list[dict[str, object]]):
    """同一条链各判一次：UPS 时刻（标记尚未落盘，leaf 在标记之前）与 Stop 时刻（标记 + 回复已在链上）。

    只给 E2：不传 UPS 到达记录、不传回合标识，结论完全取决于 transcript 祖先链。
    """
    evidence = load_hook_module().decision_point_agent_resumed_work_evidence  # noqa: F821
    marker = _decision_point_boundary_marker_rendered_by_production(E2_FIXTURE_DECISION_TOKEN)
    case.mkdir(parents=True, exist_ok=True)
    chains = {
        "at-user-prompt-submit": list(entries_before_marker),
        "at-stop": [
            *entries_before_marker,
            _user_prompt_entry(marker, _e2_minted_at_offset(200)),
            _assistant_entry("（标记未随附正文，按说明忽略）", _e2_minted_at_offset(201)),
        ],
    }
    verdicts = []
    for name, entries in chains.items():
        transcript = _write_claude_transcript_chain(case / f"{name}.jsonl", entries)
        verdicts.append(
            evidence.classify_whether_agent_resumed_work_after_decision_token_mint(
                decision_token=E2_FIXTURE_DECISION_TOKEN,
                decision_token_payload={
                    "session_id": E2_FIXTURE_SESSION_ID,
                    "minted_at": E2_FIXTURE_DECISION_TOKEN_MINTED_AT,
                },
                current_hook_event_session_id=E2_FIXTURE_SESSION_ID,
                current_hook_event_agent_turn_identifier=None,
                agent_turn_identifier_of_the_stop_event_that_minted_this_token=None,
                transcript_path=str(transcript),
                user_prompt_submit_arrivals_recorded_for_session=(),
            )
        )
    return verdicts[0], verdicts[1]


def test_developer_local_slash_command_after_token_mint_is_transparent_to_transcript_ancestor_chain_evidence(
    tmp: Path,
) -> None:
    """(L) 铸出后开发者空闲时跑了 ``/model``：命令条目是 type:user 但不是复工。

    修复前 E2 取到 ``<local-command-stdout>`` 的时间戳（晚于铸点）⇒ 判复工 ⇒ 撤回 token、block 掉标记。
    现在命令整组透明：UPS 侧回扫到铸点前的 assistant ⇒ 无证据；Stop 侧 ⇒ 标记开回合。
    """
    at_user_prompt_submit, at_stop = _e2_verdicts_at_user_prompt_submit_and_at_stop(
        tmp / "local-slash-command",
        [
            *_pre_compact_history_ending_before_token_mint(),
            _user_prompt_entry(
                "<command-name>/model</command-name>\n            <command-message>model</command-message>\n"
                "            <command-args></command-args>",
                _e2_minted_at_offset(5),
            ),
            _user_prompt_entry(
                "<local-command-stdout>Set model to opus</local-command-stdout>", _e2_minted_at_offset(5.05)
            ),
        ],
    )
    assert at_user_prompt_submit.verdict == "no_evidence_either_way", at_user_prompt_submit
    assert at_stop.verdict == "turn_opened_by_decision_point_boundary_marker", at_stop


def test_manual_compact_after_token_mint_yields_no_evidence_rather_than_agent_resumed_work_or_turn_opened_by_marker(
    tmp: Path,
) -> None:
    """(M) 铸出后 60ms 开发者手动 ``/compact``：边界 → 摘要 → caveat → command-name → stdout。

    修复前 stdout / command-name 的时间戳晚于铸点 ⇒ 判复工；compact 本身不触发 Stop，重新征询要拖到下一个
    agent 回合结束。现在命令组与摘要透明，撞到 manual 边界即无证据（两侧都是）——不沿 ``logicalParentUuid``
    回溯压缩前的历史，也不落到「标记开回合」。
    """
    at_user_prompt_submit, at_stop = _e2_verdicts_at_user_prompt_submit_and_at_stop(
        tmp / "manual-compact",
        [
            *_pre_compact_history_ending_before_token_mint(),
            _compact_boundary_entry("manual", _e2_minted_at_offset(104.9)),
            _compact_summary_entry(_e2_minted_at_offset(102.3)),
            _is_meta_user_entry(
                "<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a "
                "request.</local-command-caveat>",
                _e2_minted_at_offset(0.06),
            ),
            _user_prompt_entry(
                "<command-name>/compact</command-name>\n            <command-message>compact</command-message>\n"
                "            <command-args></command-args>",
                _e2_minted_at_offset(0.06),
            ),
            _user_prompt_entry(
                "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>",
                _e2_minted_at_offset(105.0),
            ),
        ],
    )
    assert at_user_prompt_submit.verdict == "no_evidence_either_way", at_user_prompt_submit
    assert at_stop.verdict == "no_evidence_either_way", at_stop


def test_auto_compact_after_token_mint_still_counts_as_agent_resumed_work_in_transcript_ancestor_chain_evidence(
    tmp: Path,
) -> None:
    """(R2) 正向守卫：auto-compact 只在模型查询中发生 ⇒ 晚于铸点的 auto 边界本身就是复工证据。

    第二条链 leaf 停在摘要（auto 边界之后还没有 assistant），钉住「边界本身」这一支；第三条钉住 auto 边界
    不晚于铸点时保守判无证据。
    """
    after_mint = [
        _compact_boundary_entry("auto", _e2_minted_at_offset(50)),
        _compact_summary_entry(_e2_minted_at_offset(48)),
    ]
    for case_name, chain in (
        ("auto-compact-then-assistant", [*after_mint, _assistant_entry("接着改。", _e2_minted_at_offset(55))]),
        ("auto-compact-leaf-at-summary", after_mint),
    ):
        at_user_prompt_submit, at_stop = _e2_verdicts_at_user_prompt_submit_and_at_stop(
            tmp / case_name, [*_pre_compact_history_ending_before_token_mint(), *chain]
        )
        assert at_user_prompt_submit.verdict == "agent_resumed_work_after_token_mint", (case_name, at_user_prompt_submit)
        assert at_stop.verdict == "agent_resumed_work_after_token_mint", (case_name, at_stop)
    at_user_prompt_submit, _at_stop = _e2_verdicts_at_user_prompt_submit_and_at_stop(
        tmp / "auto-compact-before-mint",
        [
            _compact_boundary_entry("auto", _e2_minted_at_offset(-30)),
            _compact_summary_entry(_e2_minted_at_offset(-31)),
        ],
    )
    assert at_user_prompt_submit.verdict == "no_evidence_either_way", at_user_prompt_submit


def test_is_meta_stop_hook_feedback_after_token_mint_still_counts_as_agent_resumed_work_in_transcript_ancestor_chain_evidence(
    tmp: Path,
) -> None:
    """(isMeta 对照) 正向守卫：透明的是本地命令组与摘要，**不是** isMeta 整体。

    isMeta 的 ``Stop hook feedback:`` 表示别的 Stop hook 让 agent 续跑，本身就是复工信号。
    """
    at_user_prompt_submit, at_stop = _e2_verdicts_at_user_prompt_submit_and_at_stop(
        tmp / "is-meta-stop-hook-feedback",
        [
            *_pre_compact_history_ending_before_token_mint(),
            _is_meta_user_entry("Stop hook feedback:\n还有未完成的检查，继续。", _e2_minted_at_offset(2)),
        ],
    )
    assert at_user_prompt_submit.verdict == "agent_resumed_work_after_token_mint", at_user_prompt_submit
    assert at_stop.verdict == "agent_resumed_work_after_token_mint", at_stop


def test_host_adapters_report_agent_turn_identifier_shared_by_hook_events_of_one_turn(tmp: Path) -> None:
    """HostAdapter 新方法：Claude Code 取 ``prompt_id``、Codex 取 ``turn_id``、Kimi 恒 None；协议自检含它。"""
    load_hook_module()  # noqa: F821 - pyroot 上 sys.path
    from adapters.claude_code.claude_code_host_adapter import ClaudeCodeHostAdapter
    from adapters.codex.codex_host_adapter import CodexHostAdapter
    from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter
    from core.host_adapter import HostAdapter
    from core.host_adapter.host_adapter_protocol import _self_check

    claude = ClaudeCodeHostAdapter()
    codex = CodexHostAdapter()
    kimi = KimiCodeCliHostAdapter()
    for adapter in (claude, codex, kimi):
        assert isinstance(adapter, HostAdapter), adapter
    event = {"prompt_id": "prompt-1", "turn_id": "turn-1"}
    assert claude.agent_turn_identifier_shared_by_hook_events_of_one_turn(event) == "prompt-1"
    assert codex.agent_turn_identifier_shared_by_hook_events_of_one_turn(event) == "turn-1"
    assert kimi.agent_turn_identifier_shared_by_hook_events_of_one_turn(event) is None
    assert claude.agent_turn_identifier_shared_by_hook_events_of_one_turn({"prompt_id": "  "}) is None
    assert codex.agent_turn_identifier_shared_by_hook_events_of_one_turn({}) is None
    _self_check()
