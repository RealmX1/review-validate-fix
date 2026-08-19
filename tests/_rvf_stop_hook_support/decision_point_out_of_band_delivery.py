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

    # ② 把意图 TTL 推到过去 = 横幅始终没成为 turn ⇒ 搁浅对账撤回它，下一次 Stop 重新征询一次。
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
    assert len(after) == 2, after


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
    """D6：投出去却始终没成为 turn 的横幅，到期后必须真的被撤回，然后**重新征询恰好一次**。

    没有这一条，一轮被判「已撤回」的横幅 20 分钟后落地照样能跑出一轮无 lease、无 scope 契约的
    review——F2a 的 tombstone 对横幅是空的（横幅不带 ``RVF_DISPATCH=token=`` needle）。
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

    stdout, _stderr = invoke(_stop_event(repo), state_dir=state, extra_env=env)
    names = [event.get("event") for event in latest_events(state)]
    assert "decision_point_out_of_band_delivery_stranded_withdrawn" in names, names

    records = {record["token"]: record for record in decision_token_records(state)}
    assert records[stranded_token]["state"] == "withdrawn", records[stranded_token]
    # 撤回后**重新征询恰好一次**：新铸一枚、再投一次，绝不多投。
    fresh = [token for token in records if token != stranded_token]
    assert len(fresh) == 1, records
    assert records[fresh[0]]["state"] == "minted", records[fresh[0]]
    assert len(_delivery_events(state)) == 1, latest_events(state)
    assert parse_json(stdout) is not None


def _write_rewound_transcript(path: Path, token: str, banner_excerpt: str) -> Path:
    """造一份「token 落地过、但被 rewind 掉」的 transcript：needle 在文件里，但不在祖先链上。"""
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
            "message": {"role": "user", "content": f"{banner_excerpt}\nRVF_DECISION_TOKEN={token}"},
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
        rewound_token,
        "=== REVIEW-VALIDATE-FIX 发起决策点（agent 决策，非自动发起）===",
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
