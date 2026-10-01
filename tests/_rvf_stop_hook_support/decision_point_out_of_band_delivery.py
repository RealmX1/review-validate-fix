#!/usr/bin/env python3
"""发起决策点横幅的**带外投递** 测试簇（横幅成为一条真实 user message ⇒ 可 rewind）。

从 tests/test_stop_review_validate_fix.py 有界拆出的兄弟子模块（与既有子模块同款：模块级
``inject()`` 推入共享 helper，扁平 ``tests=[...]`` 注册表按裸名解析）。

本簇钉的是「同步 → 异步」这个转换独有的那些时序假设：四态回执各自的处置、闸/在途复检/铸币的
**次序**、崩溃窗口、TTL、以及两条撤回路径（用户 rewind / 投递搁浅）。stdout 侧的 INV-1/INV-2
由 ``tests/test_review_initiation_decision_point_is_sole_self_injection.py`` 那道结构性 gate 负责。
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

# 由 aggregator（tests/test_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_decision_point_out_of_band_delivered_receipts_keep_token_minted',
    'test_decision_point_out_of_band_uses_longer_ttl_than_self_injection',
    'test_decision_point_out_of_band_delivery_never_started_falls_back_to_self_injection',
    'test_decision_point_out_of_band_unparseable_receipt_after_delivery_never_self_injects',
    'test_decision_point_unknown_delivery_outcome_is_reconciled_not_redelivered',
    'test_decision_point_transport_override_without_task_id_falls_back_honestly',
    'test_decision_point_out_of_band_in_flight_blocks_remint_without_charging_breaker',
    'test_decision_point_out_of_band_single_flight_busy_does_not_mint',
    'test_decision_point_stranded_delivery_intent_is_withdrawn_and_reasked_once',
    'test_stranded_delivery_intent_from_another_worktree_does_not_suppress_this_worktree_decision_point',
    'test_stranded_banner_whose_marker_arrived_after_ttl_keeps_mint_request_charge_because_body_was_never_injected',
    'test_stranded_banner_whose_marker_became_a_turn_without_user_prompt_submit_ever_handling_it_backs_off_and_says_so',
    'test_stranded_banner_reverses_mint_request_charge_only_on_positive_evidence_the_body_was_injected',
    'test_decision_point_banner_body_text_persist_failure_falls_back_to_self_injection_and_says_why',
    'test_decision_point_banner_command_wording_matches_the_rendered_command_prefix',
    'test_decision_point_boundary_marker_that_arrived_as_queued_command_without_user_prompt_submit_is_reinjected_by_the_next_stop',
    'test_decision_point_rewound_banner_is_withdrawn_and_reasked_once',
    'test_decision_point_self_injected_token_is_not_withdrawn_as_rewound',
    'test_decision_point_crash_between_send_and_receipt_is_reconciled_from_intent',
    'test_decision_point_declined_fingerprint_is_not_delivered_out_of_band_again',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals。"""
    globals().update(deps)


_STUB_CLIENT_SOURCE = '''#!/usr/bin/env python3
"""测试替身：冒充 ``cline_kanban_client.py message``，回一份可控的终端投递回执。"""
import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("subcommand")
    parser.add_argument("--repo")
    parser.add_argument("--task-cmd")
    parser.add_argument("--task-id")
    parser.add_argument("--prompt-file")
    parser.add_argument("--source")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--attempt-id")
    parser.add_argument("--wait-for-terminal-status-timeout-ms")
    args, _unknown = parser.parse_known_args()
    capture = os.environ.get("RVF_TEST_DELIVERED_PROMPT_CAPTURE_PATH")
    if capture and args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as handle:
            body = handle.read()
        with open(capture, "a", encoding="utf-8") as handle:
            handle.write(body)
            handle.write("\\n<<<RVF_TEST_DELIVERY_BOUNDARY>>>\\n")
    # 「先把横幅投出去、再返回坏响应」——真实 client 就是这个顺序：先跑完 ``kanban task message``
    # 子进程（外部副作用此刻已经发生），再解析 JSON、再校验 message_id。所以证据必须在返回之前落盘，
    # 否则用例根本区分不了「确定没投」与「投了但回执读不懂」。
    evidence = os.environ.get("RVF_TEST_STUB_CLIENT_DELIVERY_EVIDENCE_PATH")
    if evidence and args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as handle:
            delivered_body = handle.read()
        with open(evidence, "a", encoding="utf-8") as handle:
            handle.write(delivered_body)
            handle.write("\\n<<<RVF_TEST_BANNER_ACTUALLY_DELIVERED>>>\\n")
    bad_response = os.environ.get(
        "RVF_TEST_STUB_CLIENT_RESPONSE_MODE_AFTER_DELIVERING_BANNER", ""
    )
    if bad_response == "unparseable_json":
        # CLI 版本漂移：投递成功，但 stdout 多了一行非 JSON 的告警。
        sys.stdout.write("kanban cli: warning: deprecated flag --source\\n{not json}")
        return 0
    if bad_response == "receipt_without_message_id":
        sys.stdout.write(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "idempotency_key": args.idempotency_key,
                    "terminal_delivery_status": os.environ[
                        "RVF_TEST_TERMINAL_DELIVERY_STATUS"
                    ],
                }
            )
        )
        return 0
    if bad_response == "nonzero_exit_after_delivering":
        # 投递成功但进程随后非零退出（例如收尾阶段炸了）。
        sys.stderr.write("stub cline-kanban client died after delivering the banner\\n")
        return 3
    receipt = {
        "message_id": f"terminal:{args.task_id}:{args.idempotency_key}",
        "task_id": args.task_id,
        "idempotency_key": args.idempotency_key,
        "terminal_delivery_status": os.environ["RVF_TEST_TERMINAL_DELIVERY_STATUS"],
    }
    reason = os.environ.get("RVF_TEST_TERMINAL_DELIVERY_FAILURE_REASON")
    if reason:
        receipt["terminal_delivery_failure_reason"] = reason
    sys.stdout.write(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_stub_client(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB_CLIENT_SOURCE, encoding="utf-8")
    return path


def _out_of_band_env(
    *,
    stub_client: Path,
    task_id: str,
    terminal_delivery_status: str = "accepted_pending_submit_confirmation",
    **extra: str,
) -> dict[str, str]:
    env = {
        "KANBAN_TASK_ID": task_id,
        "CODEX_RVF_CLINE_KANBAN_CLIENT": str(stub_client),
        # 撤回路径会 best-effort 物理取消一条投递；指向一个必然失败的命令，让它快失败而不是
        # 去碰真实的 kanban CLI。
        "CODEX_RVF_CLINE_KANBAN_TASK_CMD": str(stub_client.parent / "no-such-kanban-cli"),
        "RVF_TEST_TERMINAL_DELIVERY_STATUS": terminal_delivery_status,
    }
    env.update(extra)
    return env


def _delivery_events(state: Path) -> list[dict[str, object]]:
    return [
        event
        for event in latest_events(state)
        if event.get("event") == "decision_point_delivered_out_of_band"
    ]


def _mint_request_records(state: Path) -> list[dict[str, object]]:
    """铸币请求 ledger（熔断的纯计数依据）。闸挡住的那些 Stop **不该**在这里留痕。"""
    base = rvf_home_dir(state) / "agentic-initiation-circuit-breaker-ledger" / "mint-requests"
    if not base.is_dir():
        return []
    records: list[dict[str, object]] = []
    for path in sorted(base.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _effective_mint_request_charges(state: Path) -> list[dict[str, object]]:
    """**仍然占着熔断额度**的那些铸币请求（= 熔断计数真正看的东西）。

    ledger 是 append-only 的，撤回一枚 token 不会删掉它当初那条铸造请求，而是**追加一条冲销
    记录**（``reverses_mint_request_record_id``）。所以「撤回不计熔断」这条不变量不能拿原始行数去
    验——那样既会把冲销记录本身算成一次铸造，也会把已被冲销的那次继续算着。这里按 ledger 自己的
    语义还原有效额度：剔除冲销记录本身，再剔除被它点名冲销掉的那次铸造。
    """
    records = _mint_request_records(state)
    reversed_ids = {
        record.get("reverses_mint_request_record_id")
        for record in records
        if record.get("reverses_mint_request_record_id")
    }
    return [
        record
        for record in records
        if not record.get("reverses_mint_request_record_id")
        and record.get("mint_request_record_id") not in reversed_ids
    ]


def _delivery_intents(state: Path) -> list[dict[str, object]]:
    base = (
        state
        / "kanban-followup-in-progress"
        / "kanban-decision-point-delivery-intent"
    )
    if not base.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(base.glob("task-*.json"))
    ]


def _stop_event(repo: Path, *, session_id: str = "oob-sess", **extra: object) -> dict[str, object]:
    event: dict[str, object] = {
        "cwd": str(repo),
        "stop_hook_active": False,
        "session_id": session_id,
        "last_user_message": "实现完了，先停一下。",
    }
    event.update(extra)
    return event


def test_decision_point_out_of_band_delivered_receipts_keep_token_minted(tmp: Path) -> None:
    """两个 ``delivered_*`` 终态都**不推进 token**——推进权交给 UPS。

    这条钉的是 D4 的核心改动：``injected`` 的语义统一成「agent 确实拿到了这份正文」。投递回执只
    回答「消息送到没送到」，它落在正交字段 ``out_of_band_delivery_status`` 上，绝不参与 lifecycle。
    ``accepted_pending_submit_confirmation``（默认路径）由结构性 gate 覆盖，这里补两个终态。
    """
    for status in (
        "delivered_and_submit_confirmed",
        "delivered_queued_behind_active_agent_turn",
    ):
        case = tmp / status
        repo = init_repo(case / "repo", dirty=True)
        state = case / "state"
        stub = _write_stub_client(case / "stub-client.py")
        stdout, _stderr = invoke(
            _stop_event(repo),
            state_dir=state,
            extra_env=_out_of_band_env(
                stub_client=stub,
                task_id=f"task-{status}",
                terminal_delivery_status=status,
            ),
        )
        payload = parse_json(stdout)
        assert "decision" not in payload, payload
        assert "hookSpecificOutput" not in payload, payload

        tokens = decision_token_records(state)
        assert len(tokens) == 1, tokens
        assert tokens[0]["state"] == "minted", tokens[0]
        assert tokens[0]["out_of_band_delivery_status"] == status, tokens[0]
        assert (
            tokens[0]["decision_point_delivery_transport"] == "cline_kanban_task_chat_session"
        ), tokens[0]

        assert len(_delivery_events(state)) == 1, latest_events(state)
        ledger = injection_attempt_records(state)
        assert len(ledger) == 1, ledger
        assert ledger[0]["mint_outcome"] == "delivered_out_of_band", ledger[0]
        # ``injected=False`` 是**如实**的：这一刻 agent 什么也没拿到。记 True 就是谎报，
        # canary 的分子会被一批「投出去了但可能根本没落地」的记录污染。
        assert ledger[0]["injected"] is False, ledger[0]

        intents = _delivery_intents(state)
        assert len(intents) == 1, intents
        assert intents[0]["state"] == "sent_awaiting_agent_receipt", intents[0]
        assert intents[0]["decision_token"] == tokens[0]["token"], intents[0]

        # 决不写 follow-up 的 pending marker：那一族背后有 run / lease / prep 要善后，而决策点
        # 背后只有一枚 token。写错族会让「15 分钟没成为 turn 就撤回」那套逻辑对着一轮**根本
        # 没开始**的 review 做善后（放 lease、清锁），也就是 ⑦ 要防的事。
        pending_dir = state / "kanban-followup-in-progress" / "kanban-followup-dispatched"
        pending = sorted(pending_dir.glob("task-*.json")) if pending_dir.is_dir() else []
        assert pending == [], pending


def test_decision_point_out_of_band_uses_longer_ttl_than_self_injection(tmp: Path) -> None:
    """带外 token 用 1200s TTL，同步自注入的 900s **逐字不动**。

    下界由两条契约共同决定：> 200s（对方的投递状态收敛预算）**且** > 900s（投递意图 TTL）。
    token 若先于意图死掉，撤回逻辑接手时已无 token 可对账。
    """
    hook = load_hook_module()
    store = sys.modules["rvf_decision_token_store"]
    assert store.DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY == 1200
    assert store.DEFAULT_DECISION_TOKEN_TTL_SECONDS == 900
    lock_module = sys.modules["kanban_followup_lock"]
    assert (
        store.DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY
        > lock_module.DEFAULT_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS
    ), "带外 token TTL 必须严格大于投递意图 TTL，否则撤回接手时已无 token 可对账"
    assert store.DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY > 200

    case = tmp / "ttl"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    invoke(
        _stop_event(repo),
        state_dir=state,
        extra_env=_out_of_band_env(stub_client=stub, task_id="task-ttl"),
    )
    record = decision_token_records(state)[0]
    created = store.parse_timestamp(str(record["created_at"]))
    expires = store.parse_timestamp(str(record["expires_at"]))
    assert (expires - created).total_seconds() == 1200, record

    # 对照：task 外走自注入，仍是 900s。
    self_state = case / "self-state"
    self_repo = init_repo(case / "self-repo", dirty=True)
    invoke(_stop_event(self_repo, session_id="oob-self"), state_dir=self_state)
    self_record = decision_token_records(self_state)[0]
    self_created = store.parse_timestamp(str(self_record["created_at"]))
    self_expires = store.parse_timestamp(str(self_record["expires_at"]))
    assert (self_expires - self_created).total_seconds() == 900, self_record
    assert hook is not None


def test_decision_point_out_of_band_delivery_never_started_falls_back_to_self_injection(
    tmp: Path,
) -> None:
    """客户端脚本**根本不存在** = 投递子进程没起来 = 确定失败 ⇒ 回落自注入，并清掉投递意图。

    与结构性 gate 里那条 ``delivery_failed`` 回执用例互补：一个是「对方明说失败了」，一个是
    「我们根本没把消息交出去」。**只有这两类**属于 INV-B 的「确定失败」那一半。

    ⚠️ 本条曾经用「客户端非零退出」来验回落，那是错的：子进程一旦跑起来，横幅就可能已经投出去，
    非零退出属于**结论未知**（见 ``…unparseable_receipt_after_delivery…`` 那条）。用未知场景去验
    「只在确定失败时才该发生」的行为，测的是错的行为。
    """
    case = tmp / "delivery-never-started"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    stdout, _stderr = invoke(
        _stop_event(repo),
        state_dir=state,
        extra_env={
            **_out_of_band_env(stub_client=stub, task_id="task-delivery-never-started"),
            # 客户端路径指向一个不存在的文件 ⇒ 外部副作用确定没发生。
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(case / "no-such-client.py"),
        },
    )
    _payload, token = assert_decision_point_injection(stdout)
    assert _delivery_events(state) == []
    assert _delivery_intents(state) == []
    records = decision_token_records(state)
    assert len(records) == 1 and records[0]["token"] == token, records
    assert records[0]["state"] == "injected", records[0]
    ledger = injection_attempt_records(state)
    assert len(ledger) == 1 and ledger[0]["mint_outcome"] == "injected", ledger
    assert ledger[0]["note"] == "fell_back_from_out_of_band_delivery", ledger[0]
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_out_of_band_delivery_failed" in names, names


def test_decision_point_out_of_band_unparseable_receipt_after_delivery_never_self_injects(
    tmp: Path,
) -> None:
    """**拿不到可解析回执 ≠ 投递失败**：横幅可能已经在 agent 面前了 ⇒ 本轮静默停、保留投递意图。

    真实 client 的顺序是「先跑完投递子进程（横幅此时可能已经投出去），再解析 JSON、再校验
    ``message_id``」——后两步任一失败都会抛异常。历史实现把 ``message_payload is None`` 当「确定
    失败」，于是这三种真实场景都会**清掉投递意图 + 当场自注入**，造出「一条带外 user message +
    一条 stdout 正文」，同时违反 INV-1（一次 Stop 至多一条发起正文）与 INV-B（结论未知不回落）。

    三种模式都必须归入未知：坏 JSON（CLI 版本漂移多打一行）、回执缺 ``message_id``、
    投递成功但进程随后非零退出。
    """
    for mode in (
        "unparseable_json",
        "receipt_without_message_id",
        "nonzero_exit_after_delivering",
    ):
        case = tmp / mode
        repo = init_repo(case / "repo", dirty=True)
        state = case / "state"
        stub = _write_stub_client(case / "stub-client.py")
        evidence = case / "banner-actually-delivered.md"
        stdout, _stderr = invoke(
            _stop_event(repo),
            state_dir=state,
            extra_env=_out_of_band_env(
                stub_client=stub,
                task_id=f"task-{mode.replace('_', '-')}",
                RVF_TEST_STUB_CLIENT_DELIVERY_EVIDENCE_PATH=str(evidence),
                RVF_TEST_STUB_CLIENT_RESPONSE_MODE_AFTER_DELIVERING_BANNER=mode,
            ),
        )

        # 前提：横幅**确实**已经投出去了（否则本用例退化成在验「确定失败」）。
        delivered = evidence.read_text(encoding="utf-8")
        assert "RVF_DECISION_TOKEN=" in delivered, (mode, delivered[:200])
        assert "<<<RVF_TEST_BANNER_ACTUALLY_DELIVERED>>>" in delivered, mode

        # stdout 里没有任何面向 agent 的正文（既不 block，也不塞 additionalContext）。
        payload = parse_json(stdout)
        assert "decision" not in payload, (mode, payload)
        assert "hookSpecificOutput" not in payload, (mode, payload)
        summary = latest_summary(state)
        assert (
            summary["reason_code"] == "decision_point_out_of_band_delivery_outcome_unknown"
        ), (mode, summary)

        # token 停在 minted：既没被推进（那会谎报 agent 拿到了），也没被当失败作废。
        records = decision_token_records(state)
        assert len(records) == 1, (mode, records)
        assert records[0]["state"] == "minted", (mode, records[0])

        # 投递意图**保留**：它是后续对账唯一的抓手（落地则 UPS 接手，搁浅则到期撤回重问）。
        intents = _delivery_intents(state)
        assert len(intents) == 1, (mode, intents)
        assert intents[0]["state"] == "recorded_before_send", (mode, intents[0])
        assert intents[0]["out_of_band_delivery_outcome_unknown"] is True, (mode, intents[0])

        names = [event.get("event") for event in latest_events(state)]
        assert "decision_point_out_of_band_delivery_outcome_unknown" in names, (mode, names)
        # 既不宣称送达，也不宣称失败。
        assert _delivery_events(state) == [], (mode, latest_events(state))
        assert "decision_point_out_of_band_delivery_failed" not in names, (mode, names)

        # canary 记账如实：mint_outcome 是「结论未知」，且绝不是自注入的 ``injected``。
        ledger = injection_attempt_records(state)
        assert len(ledger) == 1, (mode, ledger)
        assert ledger[0]["mint_outcome"] == "out_of_band_delivery_outcome_unknown", (
            mode,
            ledger[0],
        )
        assert ledger[0]["injected"] is False, (mode, ledger[0])


def test_decision_point_unknown_delivery_outcome_is_reconciled_not_redelivered(
    tmp: Path,
) -> None:
    """结论未知的下一次 Stop：**不重投**（意图仍在途），到期后由搁浅对账撤回并重新征询一次。

    这条钉的是「保留意图」这个选择的收益——没有它，未知分支就只是「本轮不注入」，下一次 Stop
    照样再投一条，先到的那条被判 superseded，agent 眼前仍会出现两份征询。
    """
    case = tmp / "unknown-reconciled"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    evidence = case / "banner-actually-delivered.md"
    env = _out_of_band_env(
        stub_client=stub,
        task_id="task-unknown-reconciled",
        RVF_TEST_STUB_CLIENT_DELIVERY_EVIDENCE_PATH=str(evidence),
        RVF_TEST_STUB_CLIENT_RESPONSE_MODE_AFTER_DELIVERING_BANNER="unparseable_json",
    )
    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    token_before = decision_token_records(state)[0]["token"]

    # ① 指纹变了也不许重投——「不知道送没送到」按「可能已经送到」处置。
    (repo / "more.txt").write_text("still dirty\n", encoding="utf-8")
    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    assert "decision" not in parse_json(stdout), stdout
    summary = latest_summary(state)
    assert summary["reason_code"] == "decision_point_out_of_band_delivery_in_flight", summary
    tokens = decision_token_records(state)
    assert len(tokens) == 1 and tokens[0]["token"] == token_before, tokens

    # ② 把意图 TTL 推到过去 = 横幅始终没成为 turn ⇒ 搁浅对账撤回它。
    #    撤回那一次 Stop **就地结束**（C-①），再下一次 Stop 才重新征询一次。
    lock_module = load_kanban_followup_lock_module()
    intents_dir = (
        state / "kanban-followup-in-progress" / "kanban-decision-point-delivery-intent"
    )
    intent_path = sorted(intents_dir.glob("task-*.json"))[0]
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["expires_at"] = "2000-01-01T00:00:00+00:00"
    intent_path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
    assert lock_module is not None

    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_out_of_band_delivery_stranded_withdrawn" in names, names
    after = {record["token"]: record for record in decision_token_records(state)}
    assert after[token_before]["state"] == "withdrawn", after[token_before]
    # C-①：撤回的那一次 Stop 不重铸——「刚撤掉一枚」正是「这条通道此刻不通」的证据。
    assert list(after) == [token_before], after
    assert latest_summary(state)["reason_code"] == (
        "decision_point_stranded_delivery_withdrawn_this_stop"
    ), latest_summary(state)

    # ③ 再下一次 Stop：这时才重新征询，且恰好一枚。
    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    reasked = {record["token"]: record for record in decision_token_records(state)}
    assert len(reasked) == 2, reasked
    fresh = [token for token in reasked if token != token_before]
    assert len(fresh) == 1 and reasked[fresh[0]]["state"] == "minted", reasked


def test_decision_point_transport_override_without_task_id_falls_back_honestly(
    tmp: Path,
) -> None:
    """逃生口被强制指向带外、但根本不在 task 内：如实回落自注入并留痕，绝不静默改道。"""
    case = tmp / "override-no-task"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stdout, _stderr = invoke(
        _stop_event(repo),
        state_dir=state,
        extra_env={
            "RVF_DECISION_POINT_DELIVERY_TRANSPORT": "cline_kanban_task_chat_session",
        },
    )
    assert_decision_point_injection(stdout)
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_out_of_band_transport_lost_task_id" in names, names
    assert _delivery_events(state) == []


def test_decision_point_out_of_band_in_flight_blocks_remint_without_charging_breaker(
    tmp: Path,
) -> None:
    """迟到窗口里指纹变了也不得重铸——而且**不该烧熔断额度**。

    这是 D2 第 2 步的存在理由：横幅投出去到落地之间可能过好几次 Stop，其间 agent 还在改文件，
    于是候选指纹会变、mint 的去重闸拦不住。没有这道在途复检，连着三次 Stop 就会把 mint-request
    计数打到熔断阈值，把整个会话静默一小时。
    """
    case = tmp / "in-flight"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id="task-in-flight")

    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    assert len(decision_token_records(state)) == 1
    assert len(_mint_request_records(state)) == 1

    # 连着两次指纹都变（agent 在等横幅落地期间还在改文件）——去重闸拦不住，只有在途复检能拦。
    # 两次刚好把 mint-request 计数推到熔断阈值（默认 3 次/3600s），所以这两轮正是「会不会把
    # 整个会话静默一小时」的分水岭。
    for index, name in enumerate(("another.txt", "yet-another.txt")):
        (repo / name).write_text(f"more dirty {index}\n", encoding="utf-8")
        stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
        payload = parse_json(stdout)
        assert "decision" not in payload, payload
        summary = latest_summary(state)
        assert (
            summary["reason_code"] == "decision_point_out_of_band_delivery_in_flight"
        ), summary
        assert len(decision_token_records(state)) == 1, decision_token_records(state)
        assert len(_mint_request_records(state)) == 1, _mint_request_records(state)


def test_decision_point_out_of_band_single_flight_busy_does_not_mint(tmp: Path) -> None:
    """闸抢不到 ⇒ 静默停，**不 mint、不计熔断**。闸挡住不是「重复铸币」，不该烧额度。"""
    case = tmp / "single-flight"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    lock_module = load_kanban_followup_lock_module()
    lock_root = state / "kanban-followup-in-progress"
    task_id = "task-single-flight"

    with lock_module.dispatch_single_flight_for_task(task_id, root=lock_root) as acquired:
        assert acquired is True
        stdout, _stderr = invoke(
            _stop_event(repo),
            state_dir=state,
            extra_env=_out_of_band_env(stub_client=stub, task_id=task_id),
        )
    payload = parse_json(stdout)
    assert "decision" not in payload, payload
    summary = latest_summary(state)
    assert (
        summary["reason_code"] == "decision_point_out_of_band_delivery_single_flight_busy"
    ), summary
    assert decision_token_records(state) == []
    assert _mint_request_records(state) == []
    assert injection_attempt_records(state) == []
    assert fcntl is not None


def test_decision_point_stranded_delivery_intent_is_withdrawn_and_reasked_once(
    tmp: Path,
) -> None:
    """D6 + C-①：搁浅横幅到期后必须真的被撤回，且**当次 Stop 不重铸**，下一次 Stop 才重新征询。

    没有 D6 那一半，一轮被判「已撤回」的横幅 20 分钟后落地照样能跑出一轮无 lease、无 scope 契约的
    review——F2a 的 tombstone 对横幅是空的（横幅不带 ``RVF_DISPATCH=token=`` needle）。

    C-① 收紧了后一半：撤回与重铸原本挨在同一条控制流上（对账只返回 notices、不返回决策），于是
    撤回后当次就落回铸造路径，在**同一次 Stop** 里再铸一枚。实测（2026-08-21）由此出现一个
    session 内连铸三枚、间隔 6~7 秒、全程 ``minted → withdrawn`` 从未 ``injected`` 的空转环。
    「刚撤掉一枚」本身就是「这条通道此刻不通」的证据，当次重铸的那枚多半照样空转。
    """
    case = tmp / "stranded"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    # 意图 TTL 归零 ⇒ 它**写下来就是 stale 的**，等价于「TTL 到了都没成为 turn」。
    # （TTL 是在写入时烘进 ``expires_at`` 的，事后改 env 动不了既有那一份。）
    env = _out_of_band_env(
        stub_client=stub,
        task_id="task-stranded",
        RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS="0",
    )

    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    first = decision_token_records(state)
    assert len(first) == 1 and first[0]["state"] == "minted", first
    stranded_token = first[0]["token"]

    # ---- 第二次 Stop：撤回，且**当次不重铸**（C-①）----
    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_out_of_band_delivery_stranded_withdrawn" in names, names

    records = {record["token"]: record for record in decision_token_records(state)}
    assert records[stranded_token]["state"] == "withdrawn", records[stranded_token]
    assert records[stranded_token]["withdrawal_cause"] == (
        "out_of_band_delivery_never_became_a_turn"
    ), records[stranded_token]
    # 当次 Stop 就地结束：token 总数没涨，且**这一次 run 里一次投递都没有**。
    # （``_delivery_events`` 读的是 ``latest_events`` = 当次 run，不是跨 run 累计。）
    assert list(records) == [stranded_token], records
    assert _delivery_events(state) == [], latest_events(state)
    withdrawal_stop_summary = latest_summary(state)
    assert withdrawal_stop_summary["reason_code"] == (
        "decision_point_stranded_delivery_withdrawn_this_stop"
    ), withdrawal_stop_summary
    # 受众纪律：这是降级诊断，**不得**驱动 agent 再跑一回合。
    withdrawal_stop_payload = parse_json(stdout)
    assert withdrawal_stop_payload is not None
    assert "decision" not in withdrawal_stop_payload, withdrawal_stop_payload
    assert "hookSpecificOutput" not in withdrawal_stop_payload, withdrawal_stop_payload

    # ---- 第三次 Stop：这时才重新征询，且**恰好一次** ----
    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    records = {record["token"]: record for record in decision_token_records(state)}
    fresh = [token for token in records if token != stranded_token]
    assert len(fresh) == 1, records
    assert records[fresh[0]]["state"] == "minted", records[fresh[0]]
    # 这一次 run 里恰好投递一次（不是累计计数，见上）。
    assert len(_delivery_events(state)) == 1, latest_events(state)
    assert parse_json(stdout) is not None


def test_stranded_delivery_intent_from_another_worktree_does_not_suppress_this_worktree_decision_point(
    tmp: Path,
) -> None:
    """另一个 worktree/task 遗留的搁浅意图，**不得**让本 worktree 这一轮的发起被整轮压掉。

    ``iter_decision_point_delivery_intents`` 是**跨 task 全局枚举**（其 docstring 逐字写着
    「枚举全部投递意图（跨 task）」），而投递意图落在全局唯一的 ``$RVF_HOME`` 根下——两个仓的
    Stop 天然共用同一份意图目录，所以本用例让两个 worktree 共用同一个 ``state_dir``。

    C-① 把「本次撤回了任意一枚」变成当次早退判据之后，这个枚举面就成了漏审源：task A 留下的
    一枚 stale intent 会让 repo B 的 Stop 直接返回
    ``decision_point_stranded_delivery_withdrawn_this_stop``，B 的脏改动**连候选摘要都不算**。
    早退的立论「刚撤掉一枚 = 这条投递通道此刻不通」只对**同一条通道**成立。

    两个变体都钉：B 走带外投递、B 走内联自注入（后者根本不经投递意图这条通道，被挡尤其荒谬）。
    跨 task 垃圾回收**必须保留**——它本来就该做，少清一份迟到落地的横幅照样能跑出一轮无 lease 的
    review。
    """
    for variant, this_stop_task_id, expected_reason_code in (
        ("out-of-band", "task-B-out-of-band", "decision_point_delivered_out_of_band"),
        ("inline-self-injection", None, "decision_point_injected"),
    ):
        case = tmp / variant
        state = case / "state"
        stub = _write_stub_client(case / "stub-client.py")

        # ---- 另一个 worktree（task A）：投一枚横幅，意图 TTL=0 ⇒ 写下来就是 stale ----
        other_worktree = init_repo(case / "repo-other-worktree", dirty=True)
        other_env = _out_of_band_env(
            stub_client=stub,
            task_id="task-A-other-worktree",
            RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS="0",
        )
        invoke(
            _stop_event(other_worktree, session_id="sess-other-worktree"),
            state_dir=state,
            extra_env=other_env,
        )
        stranded = decision_token_records(state)
        assert len(stranded) == 1 and stranded[0]["state"] == "minted", stranded
        stranded_token = stranded[0]["token"]
        assert len(_delivery_intents(state)) == 1, _delivery_intents(state)

        # ---- 本轮这个 worktree：不同的仓 / session / task，有自己的脏改动 ----
        this_worktree = init_repo(case / "repo-this-worktree", dirty=True)
        if this_stop_task_id is None:
            # 内联自注入：不进 kanban task（没有 KANBAN_TASK_ID 就没有带外通道）。
            this_env = {
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": str(stub.parent / "no-such-kanban-cli"),
                "RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS": "0",
            }
        else:
            this_env = _out_of_band_env(
                stub_client=stub,
                task_id=this_stop_task_id,
                RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS="0",
            )
        stdout, _stderr = invoke(
            _stop_event(this_worktree, session_id="sess-this-worktree"),
            state_dir=state,
            extra_env=this_env,
        )

        summary = latest_summary(state)
        assert summary["reason_code"] != (
            "decision_point_stranded_delivery_withdrawn_this_stop"
        ), (variant, summary)
        assert summary["reason_code"] == expected_reason_code, (variant, summary)

        records = {record["token"]: record for record in decision_token_records(state)}
        fresh = [token for token in records if token != stranded_token]
        assert len(fresh) == 1, (variant, records)
        assert records[fresh[0]]["state"] in ("minted", "injected"), (
            variant,
            records[fresh[0]],
        )

        # 跨 task 垃圾回收仍然做了：另一个 worktree 那枚被撤回、它的意图被清掉。
        assert records[stranded_token]["state"] == "withdrawn", (
            variant,
            records[stranded_token],
        )
        names = [event.get("event") for event in latest_events(state)]
        assert "decision_point_out_of_band_delivery_stranded_withdrawn" in names, (
            variant,
            names,
        )
        garbage_collected = [
            event
            for event in latest_events(state)
            if event.get("event") == "decision_point_out_of_band_delivery_stranded_withdrawn"
        ]
        assert len(garbage_collected) == 1, (variant, garbage_collected)
        # ledger 必须如实记下「这条不是本轮这条通道的」——它正是早退判据的取数口径。
        assert garbage_collected[0][
            "stranded_delivery_intent_belongs_to_this_stop_delivery_channel"
        ] is False, (variant, garbage_collected[0])
        remaining_intent_task_ids = sorted(
            str(intent.get("kanban_task_id")) for intent in _delivery_intents(state)
        )
        assert "task-A-other-worktree" not in remaining_intent_task_ids, (
            variant,
            remaining_intent_task_ids,
        )
        if this_stop_task_id is not None:
            assert remaining_intent_task_ids == [this_stop_task_id], (
                variant,
                remaining_intent_task_ids,
            )
            assert parse_json(stdout) is not None
        else:
            # 内联路径根本不写投递意图。
            assert remaining_intent_task_ids == [], (variant, remaining_intent_task_ids)
            assert parse_json(stdout).get("decision") == "block", (variant, stdout)


def _invoke_user_prompt_submit_hook(
    event: dict[str, object], *, state_dir: Path, extra_env: dict[str, str] | None = None
) -> dict[str, object]:
    """跑**真实**的 UserPromptSubmit hook，落盘根与 ``invoke()`` 指向同一个隔离 state。

    刻意驱动真 hook 而不是手工摆 token 状态：本用例的前提（「横幅落地了但 ``mark_injected``
    失败、且**刻意不清**投递意图」）是 UPS 自己的一条分支，摆状态就摆掉了它是否仍然可达这件事。
    """
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("CODEX_RVF_", "RVF_", "KANBAN_", "CLINE_KANBAN_")):
            env.pop(name, None)
    env.pop("CODEX_THREAD_ID", None)
    env["CODEX_RVF_STATE_DIR"] = str(state_dir)
    env["RVF_HOME"] = str(state_dir / "rvf-home")
    env["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(state_dir / "kanban-followup-in-progress")
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, str(Path(SCRIPT).with_name("rvf_user_prompt_submit.py"))],  # noqa: F821
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def _decision_point_boundary_marker_rendered_by_production(token: str) -> str:
    """落地成 user message 的那一行边界标记——**调生产渲染函数**，绝不在测试里手抄正文。"""
    return load_hook_module()._render_decision_point_out_of_band_boundary_marker(token=token)  # noqa: F821


def _write_transcript_with_banner_marker_on_ancestor_chain(path: Path, marker: str) -> Path:
    path.write_text(
        "\n".join(
            json.dumps(entry, ensure_ascii=False)
            for entry in [
                {
                    "uuid": "entry-root",
                    "parentUuid": None,
                    "type": "user",
                    "message": {"role": "user", "content": "开始干活"},
                },
                {
                    "uuid": "entry-banner",
                    "parentUuid": "entry-root",
                    "type": "user",
                    "message": {"role": "user", "content": marker},
                },
                {
                    "uuid": "entry-after-banner",
                    "parentUuid": "entry-banner",
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "先把手头的事做完"},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _mint_out_of_band_banner_that_goes_stale(case: Path, *, task_id: str, session_id: str):
    """Stop 铸 token + 带外投出边界标记；投递意图 TTL=0 ⇒ 下一次 Stop 必判搁浅。"""
    repo = init_repo(case / "repo", dirty=True)  # noqa: F821
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(
        stub_client=stub, task_id=task_id, RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS="0"
    )
    invoke(_stop_event(repo, session_id=session_id), state_dir=state, extra_env=env)  # noqa: F821
    minted = decision_token_records(state)  # noqa: F821
    assert len(minted) == 1 and minted[0]["state"] == "minted", minted
    assert len(_effective_mint_request_charges(state)) == 1, _mint_request_records(state)
    return repo, state, env, minted[0]["token"]


def test_stranded_banner_whose_marker_arrived_after_ttl_keeps_mint_request_charge_because_body_was_never_injected(
    tmp: Path,
) -> None:
    """M9：边界标记**确实**成了一条 turn，但 UPS 到达时 token 已过 TTL、没能推进 ⇒ 正文从未注入。

    59ffd 之后「标记在祖先链上」≠「征询到达了 agent 眼前」：标记本身不含任何指令，agent 只看到
    「未随附即忽略」那一行。冲销白名单的判据（有正向证据表明征询确实到达过）在这里不成立——每次
    都是「铸 → 空转一回合 → 撤回」，必须计入熔断退避，并以 developer_only 如实说明 UPS 那一侧的原因。
    """
    case = tmp / "marker-arrived-after-ttl"
    repo, state, env, token = _mint_out_of_band_banner_that_goes_stale(
        case, task_id="task-marker-after-ttl", session_id="sess-marker-after-ttl"
    )
    token_path = next((rvf_home_dir(state) / "agentic-initiation-decision-tokens").glob("*.json"))  # noqa: F821
    expired_payload = json.loads(token_path.read_text(encoding="utf-8"))
    expired_payload["expires_at"] = "1970-01-01T00:00:00Z"
    token_path.write_text(json.dumps(expired_payload), encoding="utf-8")

    marker = _decision_point_boundary_marker_rendered_by_production(token)
    ups_payload = _invoke_user_prompt_submit_hook(
        {
            "cwd": str(repo),
            "session_id": "sess-marker-after-ttl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": marker,
        },
        state_dir=state,
        extra_env={"KANBAN_TASK_ID": "task-marker-after-ttl"},
    )
    assert "决策 token 未能推进" in str(ups_payload.get("systemMessage", "")), ups_payload
    assert "hookSpecificOutput" not in ups_payload, "未推进就绝不注入正文"
    intents = _delivery_intents(state)
    assert len(intents) == 1, intents
    observation = intents[0]["user_prompt_submit_first_arrival_observation"]
    assert observation["decision_point_banner_body_text_injected"] is False, observation
    assert "DecisionTokenExpiredError" in observation["outcome"], observation

    transcript = _write_transcript_with_banner_marker_on_ancestor_chain(case / "session.jsonl", marker)
    invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-marker-after-ttl", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    reconciled = {record["token"]: record for record in decision_token_records(state)}  # noqa: F821
    assert reconciled[token]["state"] == "withdrawn", reconciled[token]
    assert reconciled[token]["withdrawal_cause"] == (
        "out_of_band_boundary_marker_became_a_turn_but_banner_body_never_injected"
    ), reconciled[token]
    assert len(_effective_mint_request_charges(state)) == 1, "正文从未注入 ⇒ 这次空转必须计入熔断"
    summary = latest_summary(state)  # noqa: F821
    assert summary["reason_code"] == "decision_point_stranded_delivery_withdrawn_this_stop", summary
    assert summary["status"] == "skipped", "如实告警必须是 developer_only，不驱动 agent"
    notice = str(summary.get("message") or summary.get("reason") or json.dumps(summary, ensure_ascii=False))
    assert "UPS" in notice and "DecisionTokenExpiredError" in notice, notice
    assert "通道是通的" not in notice, notice


def test_stranded_banner_whose_marker_became_a_turn_without_user_prompt_submit_ever_handling_it_backs_off_and_says_so(
    tmp: Path,
) -> None:
    """M9：标记落地了，但 UPS 根本没处理它（没装 / 超时 / 崩溃）。正文只能经 UPS 注入，UPS 持续失效时
    改动前会无限「铸 → 空转 → 撤回 → 冲销」且开发者看不到 UPS 坏了。现在：计入熔断 + 点名 UPS。"""
    case = tmp / "user-prompt-submit-never-ran"
    repo, state, env, token = _mint_out_of_band_banner_that_goes_stale(
        case, task_id="task-ups-never-ran", session_id="sess-ups-never-ran"
    )
    # token 也已过 TTL：M13 的「下一次 Stop 立即自注入补发」只救得了仍有效的 token（过期 token 的
    # 正文补发了 agent 也 start 不了），这一格才轮到 M9 的撤回 + 计费。
    token_path = next((rvf_home_dir(state) / "agentic-initiation-decision-tokens").glob("*.json"))  # noqa: F821
    expired_payload = json.loads(token_path.read_text(encoding="utf-8"))
    expired_payload["expires_at"] = "1970-01-01T00:00:00Z"
    token_path.write_text(json.dumps(expired_payload), encoding="utf-8")
    marker = _decision_point_boundary_marker_rendered_by_production(token)
    transcript = _write_transcript_with_banner_marker_on_ancestor_chain(case / "session.jsonl", marker)
    invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-ups-never-ran", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    reconciled = {record["token"]: record for record in decision_token_records(state)}  # noqa: F821
    assert reconciled[token]["withdrawal_cause"] == (
        "out_of_band_boundary_marker_became_a_turn_but_banner_body_never_injected"
    ), reconciled[token]
    assert len(_effective_mint_request_charges(state)) == 1, _mint_request_records(state)
    summary = latest_summary(state)  # noqa: F821
    assert summary["status"] == "skipped", summary
    notice = json.dumps(summary, ensure_ascii=False)
    assert "UserPromptSubmit" in notice and "从未处理" in notice, notice


def _write_transcript_with_banner_marker_as_mid_turn_queued_command(path: Path, marker: str) -> Path:
    """Claude Code < 2.1.260 的落盘形态：标记在 agent 回合中途以 ``queued_command`` 附件送达。

    这种形态下没有 ``message.content``，正文在 ``attachment.prompt`` 里；那些版本上它**不触发** UPS
    （2026-09-30 本机 45 天 transcript：≤2.1.257 约 800 条 queued_command 仅 1 条后跟 UPS 附件，
    ≥2.1.260 全部后跟）。
    """
    entries = [
        {"uuid": "entry-root", "parentUuid": None, "type": "user",
         "message": {"role": "user", "content": "开始干活"}},
        {"uuid": "entry-tool-use", "parentUuid": "entry-root", "type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_probe", "name": "Bash", "input": {"command": "make test"}}]}},
        {"uuid": "entry-tool-result", "parentUuid": "entry-tool-use", "type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_probe", "content": "ok"}]}},
        {"uuid": "entry-queued-banner", "parentUuid": "entry-tool-result", "type": "attachment",
         "attachment": {"type": "queued_command", "prompt": marker, "commandMode": "prompt",
                        "origin": {"kind": "human"}}},
        {"uuid": "entry-after-queued-banner", "parentUuid": "entry-queued-banner", "type": "assistant",
         "message": {"role": "assistant", "content": "（标记未随附正文，按说明忽略）测试都过了。"}},
    ]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return path


def test_decision_point_boundary_marker_that_arrived_as_queued_command_without_user_prompt_submit_is_reinjected_by_the_next_stop(
    tmp: Path,
) -> None:
    """M13：标记在回合中途以 queued_command 送达、UPS 没触发 ⇒ agent 只看到「未随附即忽略」的一行，
    决策点静默丢失。下一次 Stop **立即**以自注入补发完整正文（同一枚 token），不等在途意图 TTL、
    不撤回、不冲销也不新增熔断额度。

    修复前：意图仍在途 ⇒ 在途闸静默挡住本次 Stop；15 分钟后搁浅对账才撤回，再下一次 Stop 才重铸。
    """
    case = tmp / "queued-command-marker"
    repo = init_repo(case / "repo", dirty=True)  # noqa: F821
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id="task-queued-command")  # 意图 TTL 保持默认（在途）
    invoke(_stop_event(repo, session_id="sess-queued-command"), state_dir=state, extra_env=env)  # noqa: F821
    minted = decision_token_records(state)  # noqa: F821
    assert len(minted) == 1 and minted[0]["state"] == "minted", minted
    token = minted[0]["token"]
    assert len(_delivery_intents(state)) == 1

    marker = _decision_point_boundary_marker_rendered_by_production(token)
    transcript = _write_transcript_with_banner_marker_as_mid_turn_queued_command(
        case / "session.jsonl", marker
    )
    stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-queued-command", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    payload = parse_json(stdout)  # noqa: F821
    body = str(
        payload.get("reason")
        or (payload.get("hookSpecificOutput") or {}).get("additionalContext")
        or ""
    )
    stop_hook = load_hook_module()  # noqa: F821
    assert body.startswith(stop_hook.DECISION_POINT_BANNER_TITLE_LINE), payload
    assert f"RVF_DECISION_TOKEN={token}" in body, "补发的必须是同一枚 token 的完整正文"
    records = decision_token_records(state)  # noqa: F821
    assert [record["token"] for record in records] == [token], "不得另铸新 token"
    assert records[0]["state"] == "injected", records[0]
    assert _delivery_intents(state) == [], "补发之后这份投递意图已无对账价值"
    assert len(_effective_mint_request_charges(state)) == 1, "不冲销、也不新增熔断额度"
    names = [event.get("event") for event in latest_events(state)]  # noqa: F821
    assert "decision_point_boundary_marker_landed_without_banner_body_reinjected_by_stop" in names, names
    notice = str(payload.get("systemMessage") or "")
    assert "UserPromptSubmit" in notice and "自注入" in notice, notice


def test_stranded_banner_reverses_mint_request_charge_only_on_positive_evidence_the_body_was_injected(
    tmp: Path,
) -> None:
    """M9 的另一半：只有投递意图里记着「UPS 已把正文注入」时，才按「通道健康、只是记账没跟上」冲销。

    这一格在当前 UPS 流程里构造上不可达（UPS 先锁内推进 token 再注入正文），这里直接摆出那份证据，
    钉住对账侧「凭证据、不凭推测」的判据：冲销只看这份正向证据，不看 transcript 上有没有标记。
    """
    case = tmp / "body-injected-evidence"
    repo, state, env, token = _mint_out_of_band_banner_that_goes_stale(
        case, task_id="task-body-injected", session_id="sess-body-injected"
    )
    intent_path = next(
        (state / "kanban-followup-in-progress" / "kanban-decision-point-delivery-intent").glob("task-*.json")
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["user_prompt_submit_first_arrival_observation"] = {
        "observed_at": "2026-09-30T00:00:00Z",
        "decision_point_banner_body_text_injected": True,
        "outcome": "banner_body_text_injected_as_additional_context",
    }
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    marker = _decision_point_boundary_marker_rendered_by_production(token)
    transcript = _write_transcript_with_banner_marker_on_ancestor_chain(case / "session.jsonl", marker)
    invoke(  # noqa: F821
        _stop_event(repo, session_id="sess-body-injected", transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    reconciled = {record["token"]: record for record in decision_token_records(state)}  # noqa: F821
    assert reconciled[token]["withdrawal_cause"] == (
        "out_of_band_delivery_became_a_turn_but_token_advance_failed"
    ), reconciled[token]
    assert _effective_mint_request_charges(state) == [], _mint_request_records(state)


def test_decision_point_banner_body_text_persist_failure_falls_back_to_self_injection_and_says_why(
    tmp: Path,
) -> None:
    """L-F3d：正文写不进 token payload ⇒ 带外这条路走不通、当场回落自注入（此前没有任何测试）。

    回落旁注必须说**真实原因**：终端投递根本没有发起，不能沿用「cline-kanban 终端投递确定失败」。
    """
    case = tmp / "body-persist-fails"
    repo = init_repo(case / "repo", dirty=True)  # noqa: F821
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    fault_injection = case / "fault-injection"
    fault_injection.mkdir(parents=True)
    (fault_injection / "sitecustomize.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(SCRIPT).parent)!r})\n"  # noqa: F821
        "import rvf_decision_token_store\n"
        "def _record_body_fails(*args, **kwargs):\n"
        "    raise OSError(28, 'No space left on device')\n"
        "rvf_decision_token_store.record_decision_point_banner_body_text_for_agent = _record_body_fails\n",
        encoding="utf-8",
    )
    capture = case / "delivered-prompts.txt"
    stdout, _stderr = invoke(  # noqa: F821
        _stop_event(repo),
        state_dir=state,
        extra_env={
            **_out_of_band_env(stub_client=stub, task_id="task-body-persist-fails"),
            "PYTHONPATH": str(fault_injection),
            "RVF_TEST_DELIVERED_PROMPT_CAPTURE_PATH": str(capture),
        },
    )
    payload, _token = assert_decision_point_injection(stdout)  # noqa: F821
    assert not capture.exists(), "正文落不了盘时不得再把标记投出去"
    assert _delivery_intents(state) == []
    names = [event.get("event") for event in latest_events(state)]  # noqa: F821
    assert "decision_point_banner_body_text_persist_failed" in names, names
    notice = str(payload.get("systemMessage") or "")
    assert "已回落为 Stop hook 自注入" in notice, notice
    assert "终端投递**确定失败**" not in notice, notice
    assert "未发起" in notice and "No space left on device" in notice, notice


def test_decision_point_banner_command_wording_matches_the_rendered_command_prefix(tmp: Path) -> None:
    """L-F3c：未装 shim 时横幅给的是 ``python3 <入口脚本>``，正文就不能再说「用下面的 `rvf` 命令」；
    ``start --retry`` 那句也要带上实际前缀和 token，否则 agent 照抄出来的是一条不存在的命令。"""
    stop_hook = load_hook_module()  # noqa: F821
    summary = {"repo": str(tmp), "candidate_path_count": 1, "candidates": []}
    token = "0123456789abcdef"
    original_prefix = stop_hook._rvf_command_line_shortcut_invocation
    for prefix in ("rvf", "python3 /opt/rvf/scripts/rvf_command_line_shortcut_entrypoint.py"):
        stop_hook._rvf_command_line_shortcut_invocation = lambda prefix=prefix: prefix
        try:
            body = stop_hook._render_decision_point_injection_prompt(
                summary=summary, token=token, fork_context=None
            )
        finally:
            stop_hook._rvf_command_line_shortcut_invocation = original_prefix
        assert f"用下面的 `{prefix}` 命令" in body, (prefix, body[:600])
        assert f"`{prefix} start --token {token} --retry`" in body, (prefix, body)
        if prefix != "rvf":
            assert "用下面的 `rvf` 命令" not in body, body[:600]


def _write_rewound_transcript(path: Path, landed_boundary_marker: str) -> Path:
    """造一份「token 落地过、但被 rewind 掉」的 transcript：needle 在文件里，但不在祖先链上。

    落地的那条 user message 由调用方传**生产渲染**出的边界标记，不在 fixture 里手抄。"""
    entries = [
        {
            "uuid": "entry-root",
            "parentUuid": None,
            "type": "user",
            "message": {"role": "user", "content": "开始干活"},
        },
        {
            "uuid": "entry-banner",
            "parentUuid": "entry-root",
            "type": "user",
            "message": {"role": "user", "content": landed_boundary_marker},
        },
        # 用户 rewind 后重发的 prompt 把 parentUuid 挂回更早的条目 ⇒ 横幅那条落在祖先链之外。
        {
            "uuid": "entry-after-rewind",
            "parentUuid": "entry-root",
            "type": "user",
            "message": {"role": "user", "content": "算了，先不审"},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return path


def test_decision_point_rewound_banner_is_withdrawn_and_reasked_once(tmp: Path) -> None:
    """INV-C：用户 rewind 掉横幅 ⇒ 该 token 当轮作废、移出去重索引、不计冷却/熔断。

    这是「能 rewind」这个功能的**另一半**。没有它，用户刚刚刻意回退掉的征询会因为去重索引仍活着
    而把接下来整个 TTL 窗口堵死——而带外 1200s 的 TTL 还把这个死区拉得更长。
    """
    case = tmp / "rewind"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id="task-rewind")

    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    first = decision_token_records(state)
    assert len(first) == 1, first
    rewound_token = first[0]["token"]
    breaker_before = len(_effective_mint_request_charges(state))

    transcript = _write_rewound_transcript(
        case / "transcript.jsonl",
        _decision_point_boundary_marker_rendered_by_production(rewound_token),
    )
    invoke(
        _stop_event(repo, transcript_path=str(transcript)),
        state_dir=state,
        extra_env=env,
    )
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_withdrawn_after_user_rewind" in names, names

    records = {record["token"]: record for record in decision_token_records(state)}
    assert records[rewound_token]["state"] == "withdrawn", records[rewound_token]
    # 去重索引已被移除，于是下一次 Stop 能且只能重新征询一次。
    dedup_dir = (
        rvf_home_dir(state) / "agentic-initiation-decision-tokens" / "dedup-index"
    )
    dedup_tokens = {
        json.loads(path.read_text(encoding="utf-8")).get("token")
        for path in sorted(dedup_dir.glob("*.json"))
    }
    assert rewound_token not in dedup_tokens, dedup_tokens
    fresh = [token for token in records if token != rewound_token]
    assert len(fresh) == 1, records
    # 撤回**不计冷却**：declined 冷却表必须仍然是空的。
    cooldown_dir = rvf_home_dir(state) / "agentic-initiation-declined-fingerprints"
    assert not cooldown_dir.is_dir() or sorted(cooldown_dir.glob("*.json")) == []
    # 撤回**不计熔断**：撤回这个动作本身绝不占额度，「撤回 + 重问」这一来一回最多只净增新铸的
    # 那一份。刻意用区间而不是等号：ledger 是 append-only 的，撤回一枚 token 会不会顺手把它当初
    # 那份额度**冲销**掉（净增 0）属于 token store 的记账策略，两种策略下这条不变量都必须成立；
    # 本用例要挡的失败模式是「撤回反而多烧一份额度」（净增 2 ⇒ 连着几轮就把会话静默一小时）。
    effective_charges_after_rewind_and_reask = len(_effective_mint_request_charges(state))
    assert (
        breaker_before
        <= effective_charges_after_rewind_and_reask
        <= breaker_before + 1
    ), (breaker_before, effective_charges_after_rewind_and_reask, _mint_request_records(state))
    assert os.path.exists(transcript)


def test_decision_point_self_injected_token_is_not_withdrawn_as_rewound(tmp: Path) -> None:
    """rewind 对账**只对带外投递成立**：自注入 token 的 needle 落在祖先链外不得判成「用户 rewind」。

    ``off_ancestor_chain_rewound`` 的隐含前提是「needle 在 transcript 里出现 = 那份横幅当初落成了
    条目」。这个前提只有带外横幅满足（它是一条真实 user message）。自注入横幅的正文走 hook stdout，
    **根本不进** ``message.content`` —— 真实 transcript 上一枚确实被自注入过的 token 探出来的结果是
    ``absent_from_transcript``。所以自注入 token 上判出的 off-ancestor-chain 只可能来自**非横幅**的
    其它出现（agent 在回复里引用了该 token 等），与用户有没有 rewind 毫无关系。

    照判的后果是双重的：一枚已 ``injected`` 的合法 token 被撤回 + 清去重索引（假阳性撤回），
    同时向开发者输出「用户 rewind 掉了这次征询」这个**错误归因**。
    """
    case = tmp / "self-injected-off-chain"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"

    # 不设 KANBAN_TASK_ID ⇒ 走自注入 transport，token 由 Stop hook 当场推进 injected。
    stdout, _stderr = invoke(_stop_event(repo, session_id="self-inj-sess"), state_dir=state)
    _payload, token = assert_decision_point_injection(stdout)
    before = decision_token_records(state)
    assert len(before) == 1, before
    assert before[0]["decision_point_delivery_transport"] == "stop_hook_self_injection", before[0]
    assert before[0]["state"] == "injected", before[0]

    # agent 在自己的回复里引用了该 token，且那条不在当前 leaf 的祖先链上（用户回退过一次别的事）。
    transcript = case / "transcript.jsonl"
    entries = [
        {"uuid": "e0", "parentUuid": None, "type": "user",
         "message": {"role": "user", "content": "开始干活"}},
        {"uuid": "e1", "parentUuid": "e0", "type": "assistant",
         "message": {"role": "assistant",
                     "content": f"hook 里提到 RVF_DECISION_TOKEN={token}，我先不管它"}},
        {"uuid": "e2", "parentUuid": "e0", "type": "user",
         "message": {"role": "user", "content": "继续别的事"}},
    ]
    transcript.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )

    invoke(
        _stop_event(repo, session_id="self-inj-sess", transcript_path=str(transcript)),
        state_dir=state,
    )
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_withdrawn_after_user_rewind" not in names, names
    after = {record["token"]: record for record in decision_token_records(state)}
    assert after[token]["state"] == "injected", after[token]
    # 去重索引仍在 ⇒ 这枚 token 依然守着它的 TTL 窗口，不会被无端重问一次。
    dedup_dir = rvf_home_dir(state) / "agentic-initiation-decision-tokens" / "dedup-index"
    dedup_tokens = {
        json.loads(path.read_text(encoding="utf-8")).get("token")
        for path in sorted(dedup_dir.glob("*.json"))
    }
    assert token in dedup_tokens, dedup_tokens


def test_decision_point_crash_between_send_and_receipt_is_reconciled_from_intent(
    tmp: Path,
) -> None:
    """崩溃窗口：进程在 send 之后、拿到回执之前被杀，下一次 Stop 必须能**从意图**对账。

    这正是「外部副作用之前先落盘意图」的存在理由——本进程的返回值永远拿不到了，只有磁盘上那份
    ``recorded_before_send`` 能证明「这一轮可能已经投出去过」。没有它，下一次 Stop 会再投一条，
    于是先到的那条被判 ``superseded_by_later_delivery``，agent 眼前出现两份征询。
    """
    case = tmp / "crash-window"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id="task-crash-window")

    # 第一轮正常投递，然后把意图**手工回退**成崩溃现场：state=recorded_before_send，
    # 且没有任何回执字段（= 我们确实不知道送没送到）。
    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    intents_dir = (
        state / "kanban-followup-in-progress" / "kanban-decision-point-delivery-intent"
    )
    intent_path = sorted(intents_dir.glob("task-*.json"))[0]
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["state"] = "recorded_before_send"
    intent.pop("out_of_band_delivery_status", None)
    intent_path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
    token_before = decision_token_records(state)[0]["token"]

    # 指纹变了也不许重投——「不知道送没送到」必须按「可能已经送到」处置（宁可晚问，不可双投）。
    (repo / "more.txt").write_text("still dirty\n", encoding="utf-8")
    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    assert "decision" not in parse_json(stdout), stdout
    summary = latest_summary(state)
    assert summary["reason_code"] == "decision_point_out_of_band_delivery_in_flight", summary
    assert _delivery_events(state) == []
    tokens = decision_token_records(state)
    assert len(tokens) == 1 and tokens[0]["token"] == token_before, tokens


def test_decision_point_declined_fingerprint_is_not_delivered_out_of_band_again(
    tmp: Path,
) -> None:
    """agent 谢绝过的指纹，带外通道同样不得再投——三闸对两条 transport 一视同仁。

    这三种拒铸结局（dedup / declined 冷却 / 铸造熔断）都发生在**投递之前**，与横幅怎么送出去无关，
    故两条 transport 共用同一份处置（``_decision_point_non_minted_outcome_decision``）。
    """
    case = tmp / "declined"
    repo = init_repo(case / "repo", dirty=True)
    state = case / "state"
    stub = _write_stub_client(case / "stub-client.py")
    env = _out_of_band_env(stub_client=stub, task_id="task-declined")

    invoke(_stop_event(repo), state_dir=state, extra_env=env)
    token_record = decision_token_records(state)[0]
    store = sys.modules["rvf_decision_token_store"]
    saved_home = os.environ.get("RVF_HOME")
    os.environ["RVF_HOME"] = str(rvf_home_dir(state))
    try:
        store.mark_declined(str(token_record["token"]), reason="dogfood-decline")
    finally:
        if saved_home is None:
            os.environ.pop("RVF_HOME", None)
        else:
            os.environ["RVF_HOME"] = saved_home

    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    assert "decision" not in parse_json(stdout), stdout
    summary = latest_summary(state)
    assert summary["reason_code"] == "decision_point_declined_cooldown", summary
    assert _delivery_events(state) == []
