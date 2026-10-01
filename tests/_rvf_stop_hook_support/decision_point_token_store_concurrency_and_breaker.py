#!/usr/bin/env python3
"""决策 token store 的**并发互斥**与**熔断额度记账** 测试簇。

从 tests/test_stop_review_validate_fix.py 有界拆出的兄弟子模块（与同目录既有子模块同款：模块级
``inject()`` 推入共享依赖，扁平 ``tests=[...]`` 注册表按裸名解析）。

本簇钉的是 ``rvf_decision_token_store`` 里两条与「横幅改走带外投递」直接相关的落盘契约：

  * **写侧互斥**：带外投递回执（``record_out_of_band_delivery_status``）与生命周期迁移
    （``mark_injected`` 等）是两条**真并发**的链路——Stop hook 侧写回执的同时，目标会话的
    UserPromptSubmit hook 正因为横幅落成 user message 而把 ``minted → injected``。两侧都是
    「读整份 payload → 改副本 → 整份覆盖」，无互斥时后写的一方会把先写的一方整份抹掉；一旦
    ``injected`` 被回滚成 ``minted``，搁浅对账会作废一份**确实送达且已被 agent 看到**的横幅
    （破坏 INV-A）。
  * **撤回不烧熔断额度**：INV-C 写着 withdrawn「不计冷却也不计熔断」。铸造熔断按 append-only 的
    mint-request ledger 纯计数，故撤回必须**追加一条冲销记录**把额度还回去，否则连续 rewind 两次
    之后第三次征询直接 ``circuit_breaker_tripped``，会话被静默一整个窗口（默认 3600s）。

这些用例刻意走 store 的公开 API + 显式落盘根（绝不碰 ``$RVF_HOME`` / 真实 ``~/.rvf``），不经 Stop
hook 全链路：要钉的是存储层自己的并发与记账语义。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

# 由 aggregator（tests/test_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_decision_token_stale_receipt_snapshot_cannot_roll_back_injected_lifecycle_state',
    'test_decision_token_receipt_write_and_lifecycle_transition_are_mutually_exclusive_under_threads',
    'test_decision_token_withdrawal_writes_payload_exactly_once_with_dedup_index_field',
    'test_two_consecutive_rewind_withdrawals_do_not_trip_circuit_breaker_on_third_ask',
    'test_decision_token_withdrawal_reversal_is_append_only_and_scoped_to_its_own_mint_request',
    'test_illegal_second_withdrawal_leaves_mint_request_ledger_untouched',
    'test_same_dedup_key_concurrent_mints_over_stale_index_yield_exactly_one_minted_token',
    'test_stranded_delivery_withdrawal_retains_mint_request_charge_so_repeat_stranding_backs_off',
    'test_unknown_withdrawal_cause_fails_closed_to_retaining_mint_request_charge',
    'test_banner_that_became_a_turn_but_failed_to_advance_reverses_mint_request_charge',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals。"""
    globals().update(deps)


def _load_decision_token_store_module():
    """按路径加载决策 token store（形状照抄 aggregator 的 ``load_workspace_snapshot_module``）。"""
    script = Path(SCRIPT).with_name("rvf_decision_token_store.py")  # noqa: F821 — 由 inject 注入
    spec = importlib.util.spec_from_file_location(
        "rvf_decision_token_store_for_concurrency_tests", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须**先**登记进 sys.modules 再 exec：模块里的 ``@dataclass`` 在 3.12+ 会按
    # ``cls.__module__`` 回查 sys.modules 解析注解，查不到即 AttributeError（假红）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _isolated_decision_token_store_roots(case: Path) -> dict[str, Path]:
    """一组互相隔离的显式落盘根（token / declined 冷却表 / 熔断 ledger）。

    显式传根而不是设 ``RVF_HOME``：本簇有多线程用例，进程级环境变量会在线程间串味。
    """
    case.mkdir(parents=True, exist_ok=True)
    return {
        "decision_token_root_dir": case / "decision-tokens",
        "declined_fingerprint_root_dir": case / "declined-fingerprints",
        "circuit_breaker_root_dir": case / "circuit-breaker-ledger",
    }


def _mint_out_of_band_decision_token(store, roots: dict[str, Path], **overrides: object):
    """按带外投递 transport 铸一枚 token（本簇所有用例的统一起点）。"""
    kwargs: dict[str, object] = {
        "worktree": "/wt",
        "session_id": "token-store-sess",
        "candidate_fingerprint": "fingerprint-1",
        "delivery_transport": store.DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
        "root": roots["decision_token_root_dir"],
        "cooldown_root": roots["declined_fingerprint_root_dir"],
        "circuit_breaker_root_dir": roots["circuit_breaker_root_dir"],
    }
    kwargs.update(overrides)
    return store.mint_decision_token(**kwargs)


def _mint_request_ledger_lines(store, roots: dict[str, Path], session_id: str) -> list[dict]:
    """读出该 session 的 mint-request ledger 全部物理行（含冲销行）。"""
    path = store._mint_request_ledger_path(session_id, roots["circuit_breaker_root_dir"])
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# issue #1：回执写入 与 lifecycle 迁移 必须互斥
# ---------------------------------------------------------------------------

def test_decision_token_stale_receipt_snapshot_cannot_roll_back_injected_lifecycle_state(
    tmp: Path,
) -> None:
    """回执侧持有**陈旧快照**时落盘，绝不能把 UPS 刚写下的 ``injected`` 回滚成 ``minted``。

    这是那条竞态的确定性形态（不靠线程调度就能复现）：回执侧先读到 ``minted`` 快照，UPS 随后把
    ``minted → injected``，回执侧最后才落盘。修复前回执侧整份覆盖会把 ``state`` / ``injected_at``
    / history 里的 injected 条目一并抹掉；修复后回执写入在**同一把每 token 排他锁内重读**，只叠
    加正交的投递字段。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "stale-snapshot")
    mint = _mint_out_of_band_decision_token(store, roots)
    assert mint.outcome == store.MINT_OUTCOME_MINTED, mint

    # 回执侧：在 UPS 推进**之前**读出快照（模拟 kanban 投递返回前就已读过 token）。
    stale_snapshot = store._read_record_any(
        mint.token, root=roots["decision_token_root_dir"], now=None
    )
    assert stale_snapshot.state == store.STATE_MINTED, stale_snapshot.payload

    # UPS 侧：横幅落成 user message ⇒ minted → injected。
    store.mark_injected(
        mint.token,
        root=roots["decision_token_root_dir"],
        ttl_seconds=store.DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY,
    )

    # 回执侧：现在才落盘。``delivered_and_submit_confirmed`` 的语义正是「消息已确认进入 agent」，
    # 也就是 UPS 那一侧必然已经在跑——这个交错不是纸上推演。
    store.record_out_of_band_delivery_status(
        mint.token,
        status="delivered_and_submit_confirmed",
        detail={"kanban_task_id": "task-stale-snapshot"},
        root=roots["decision_token_root_dir"],
    )

    final = store.read_decision_token(mint.token, root=roots["decision_token_root_dir"])
    assert final.state == store.STATE_INJECTED, final.payload
    assert "injected_at" in (final.payload or {}), final.payload
    transitions = [
        (entry.get("from"), entry.get("to")) for entry in (final.payload or {}).get("history") or []
    ]
    assert (store.STATE_MINTED, store.STATE_INJECTED) in transitions, transitions
    # 正交字段本身也必须真的写进去了（互斥不能靠「丢掉后写的那次」实现）。
    assert (final.payload or {})["out_of_band_delivery_status"] == "delivered_and_submit_confirmed"
    assert (final.payload or {})["out_of_band_delivery_detail"] == {
        "kanban_task_id": "task-stale-snapshot"
    }


def test_decision_token_receipt_write_and_lifecycle_transition_are_mutually_exclusive_under_threads(
    tmp: Path,
) -> None:
    """真并发：回执写入与 ``mark_injected`` 同时开跑，两侧的写都必须活下来。

    每一轮用 ``Barrier`` 把两个线程对齐到同一瞬间。修复前实测 20/20 轮丢写（要么 ``injected``
    被回滚成 ``minted``、要么回执被抹成 None）；修复后必须 0 轮丢写——每 token 的 flock 排他锁
    对同进程不同线程同样生效（flock 以「打开文件描述」为单位，两个线程各持自己的 fd）。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "threaded")
    rounds_with_lost_write: list[dict] = []

    for round_index in range(20):
        mint = _mint_out_of_band_decision_token(
            store,
            roots,
            session_id=f"threaded-sess-{round_index}",
            candidate_fingerprint=f"fingerprint-{round_index}",
        )
        assert mint.outcome == store.MINT_OUTCOME_MINTED, mint
        start_barrier = threading.Barrier(2)
        thread_failures: list[BaseException] = []

        def advance_lifecycle_from_user_prompt_submit_side() -> None:
            try:
                start_barrier.wait()
                store.mark_injected(
                    mint.token,
                    root=roots["decision_token_root_dir"],
                    ttl_seconds=store.DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY,
                )
            except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程断言
                thread_failures.append(exc)

        def record_delivery_receipt_from_stop_hook_side() -> None:
            try:
                start_barrier.wait()
                store.record_out_of_band_delivery_status(
                    mint.token,
                    status="delivered_and_submit_confirmed",
                    detail={"kanban_task_id": f"task-{round_index}"},
                    root=roots["decision_token_root_dir"],
                )
            except BaseException as exc:  # noqa: BLE001
                thread_failures.append(exc)

        threads = [
            threading.Thread(target=advance_lifecycle_from_user_prompt_submit_side),
            threading.Thread(target=record_delivery_receipt_from_stop_hook_side),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "写侧互斥不得把任一线程永久堵死（疑似自锁死）"
        assert thread_failures == [], thread_failures

        payload = store.read_decision_token(
            mint.token, root=roots["decision_token_root_dir"]
        ).payload or {}
        lifecycle_write_survived = payload.get("state") == store.STATE_INJECTED
        receipt_write_survived = (
            payload.get("out_of_band_delivery_status") == "delivered_and_submit_confirmed"
        )
        if not (lifecycle_write_survived and receipt_write_survived):
            rounds_with_lost_write.append(
                {
                    "round": round_index,
                    "state": payload.get("state"),
                    "receipt": payload.get("out_of_band_delivery_status"),
                }
            )

    assert rounds_with_lost_write == [], rounds_with_lost_write


def test_decision_token_withdrawal_writes_payload_exactly_once_with_dedup_index_field(
    tmp: Path,
) -> None:
    """``mark_withdrawn`` 只准整份写一次——「迁移写一次、补字段再写一次」是同族的坏形状。

    即便有锁，两次整份覆盖也意味着中间存在一个「已 withdrawn 但还没记下被删索引路径」的可观测
    中间态；把它合并成单次写入才是这个形状的正解。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "single-write")
    mint = _mint_out_of_band_decision_token(store, roots)
    assert mint.outcome == store.MINT_OUTCOME_MINTED, mint

    whole_payload_write_paths: list[str] = []
    original_atomic_write_json = store._atomic_write_json

    def counting_atomic_write_json(path, payload):
        whole_payload_write_paths.append(str(path))
        return original_atomic_write_json(path, payload)

    store._atomic_write_json = counting_atomic_write_json
    try:
        withdrawn = store.mark_withdrawn(
            mint.token,
            reason="user_rewound_decision_point_banner",
            root=roots["decision_token_root_dir"],
            circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
        )
    finally:
        store._atomic_write_json = original_atomic_write_json

    token_file_writes = [
        path for path in whole_payload_write_paths if path == str(withdrawn.path)
    ]
    assert len(token_file_writes) == 1, whole_payload_write_paths
    # 单次写入里 lifecycle 与两个补充字段必须一起落地。
    assert withdrawn.payload["state"] == store.STATE_WITHDRAWN, withdrawn.payload
    assert withdrawn.payload["withdrawn_reason"] == "user_rewound_decision_point_banner"
    assert withdrawn.payload["withdrawn_dedup_index_path"], withdrawn.payload
    assert not Path(withdrawn.payload["withdrawn_dedup_index_path"]).exists()
    on_disk = store.read_decision_token(
        mint.token, root=roots["decision_token_root_dir"]
    ).payload or {}
    assert on_disk["withdrawn_dedup_index_path"] == withdrawn.payload["withdrawn_dedup_index_path"]


# ---------------------------------------------------------------------------
# issue #2：撤回必须把铸造熔断额度还回去（INV-C）
# ---------------------------------------------------------------------------

def test_two_consecutive_rewind_withdrawals_do_not_trip_circuit_breaker_on_third_ask(
    tmp: Path,
) -> None:
    """连续两次 rewind 之后，第三次征询仍必须能正常铸造（阈值 3、窗口 3600s）。

    每轮换指纹，模拟「用户在等横幅期间又改了文件」——去重闸拦不住，每轮都会真的走一次铸造请求。
    修复前第三次直接 ``circuit_breaker_tripped``：INV-C 的「不计冷却」做到了，「不计熔断」没做到，
    用户于是被自己刚刻意做的两次 rewind 静默封禁一小时。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "rewind-twice")
    session_id = "rewound-twice-sess"

    outcomes: list[str] = []
    for ask_index in range(1, 4):
        result = _mint_out_of_band_decision_token(
            store,
            roots,
            session_id=session_id,
            candidate_fingerprint=f"fingerprint-after-edit-{ask_index}",
        )
        outcomes.append(result.outcome)
        assert result.outcome == store.MINT_OUTCOME_MINTED, (ask_index, result.outcome, result.message)
        if ask_index < 3:
            store.mark_withdrawn(
                result.token,
                reason="user_rewound_decision_point_banner",
                root=roots["decision_token_root_dir"],
                circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
            )

    assert outcomes == [store.MINT_OUTCOME_MINTED] * 3, outcomes
    # 只有第三次那份额度还占着。
    assert (
        store.count_recent_mint_requests(
            session_id, root=roots["circuit_breaker_root_dir"]
        )
        == 1
    )
    # 撤回同样**不计冷却**（INV-C 的另一半）：冷却表必须仍然是空的。
    cooldown_files = sorted(roots["declined_fingerprint_root_dir"].glob("*.json"))
    assert cooldown_files == [], cooldown_files
    # 熔断从未跳闸，故 tripped ledger 目录不该被建出来。
    tripped_dir = roots["circuit_breaker_root_dir"] / "circuit-breaker-tripped"
    assert not tripped_dir.is_dir() or sorted(tripped_dir.glob("*.jsonl")) == []


def test_decision_token_withdrawal_reversal_is_append_only_and_scoped_to_its_own_mint_request(
    tmp: Path,
) -> None:
    """冲销是**追加一行**，不是删/改既有行；且只冲销这枚 token 自己那一次铸造请求。

    ledger 的 append-only 是它的可审计性所在——「这个 session 到底请求过几次铸造」不能因为一次
    撤回就被改写。扣减只发生在 ``count_recent_mint_requests`` 的统计侧。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "append-only")
    session_id = "append-only-sess"

    first = _mint_out_of_band_decision_token(
        store, roots, session_id=session_id, candidate_fingerprint="fingerprint-first"
    )
    second = _mint_out_of_band_decision_token(
        store, roots, session_id=session_id, candidate_fingerprint="fingerprint-second"
    )
    assert first.outcome == store.MINT_OUTCOME_MINTED and second.outcome == store.MINT_OUTCOME_MINTED
    assert store.count_recent_mint_requests(session_id, root=roots["circuit_breaker_root_dir"]) == 2

    first_mint_request_record_id = first.record.payload[store.MINT_REQUEST_LEDGER_RECORD_ID_FIELD]
    second_mint_request_record_id = second.record.payload[store.MINT_REQUEST_LEDGER_RECORD_ID_FIELD]
    assert first_mint_request_record_id and second_mint_request_record_id
    assert first_mint_request_record_id != second_mint_request_record_id

    withdrawn = store.mark_withdrawn(
        first.token,
        reason="user_rewound_decision_point_banner",
        root=roots["decision_token_root_dir"],
        circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
    )
    assert (
        withdrawn.payload["withdrawn_mint_request_charge_reversal_of_record_id"]
        == first_mint_request_record_id
    )

    lines = _mint_request_ledger_lines(store, roots, session_id)
    # 两条原始请求行**一条都没被动过**，冲销是第三条新增行。
    assert len(lines) == 3, lines
    request_record_ids = [
        line.get(store.MINT_REQUEST_LEDGER_RECORD_ID_FIELD)
        for line in lines
        if store.MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD not in line
    ]
    assert request_record_ids == [first_mint_request_record_id, second_mint_request_record_id]
    reversal_lines = [
        line for line in lines if store.MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD in line
    ]
    assert len(reversal_lines) == 1, lines
    assert (
        reversal_lines[0][store.MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD]
        == first_mint_request_record_id
    )
    assert reversal_lines[0]["token"] == first.token

    # 只有被撤回的那一份额度被还回去；另一枚 token 仍然占着它自己的。
    assert store.count_recent_mint_requests(session_id, root=roots["circuit_breaker_root_dir"]) == 1


def test_illegal_second_withdrawal_leaves_mint_request_ledger_untouched(tmp: Path) -> None:
    """非法迁移必须**什么都不改**：重复撤回不得再冲销一次、也不得再动去重索引。

    ``mark_withdrawn`` 在删索引/写冲销**之前**先验合法性，正是为了这条——否则「先产生副作用、
    再抛非法迁移」会让熔断额度凭空多还一份。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "illegal-second")
    session_id = "illegal-second-sess"
    mint = _mint_out_of_band_decision_token(store, roots, session_id=session_id)
    assert mint.outcome == store.MINT_OUTCOME_MINTED, mint

    store.mark_withdrawn(
        mint.token,
        reason="user_rewound_decision_point_banner",
        root=roots["decision_token_root_dir"],
        circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
    )
    lines_after_first_withdrawal = _mint_request_ledger_lines(store, roots, session_id)
    assert len(lines_after_first_withdrawal) == 2, lines_after_first_withdrawal

    raised: Exception | None = None
    try:
        store.mark_withdrawn(
            mint.token,
            reason="user_rewound_decision_point_banner",
            root=roots["decision_token_root_dir"],
            circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
        )
    except store.DecisionTokenStateError as exc:
        raised = exc
    assert raised is not None, "withdrawn 是终态，重复撤回必须抛 DecisionTokenStateError"
    assert _mint_request_ledger_lines(store, roots, session_id) == lines_after_first_withdrawal


# ---------------------------------------------------------------------------
# issue #3：陈旧去重索引的「覆盖」必须与首建同样互斥（同一 dedup key 至多一枚活跃 token）
# ---------------------------------------------------------------------------

def test_same_dedup_key_concurrent_mints_over_stale_index_yield_exactly_one_minted_token(
    tmp: Path,
) -> None:
    """同键并发撞上**陈旧**去重索引时，至多一方 ``minted``，另一方必须幂等返回胜者的同一枚 token。

    修复前 ``mint_decision_token`` 的写索引阶段是不对称的：首建走 ``_atomic_create_json`` 的
    ``O_EXCL``（内核级互斥 + 竞态裁决），而「索引指向的 token 已终态 / 真过期」这一支是**无条件
    整份覆盖**，既没有 ``O_EXCL`` 也没有任何 CAS。闸一那次读索引发生在铸造之前、且不在任何锁内，
    于是同键的两个请求会双双读到「索引陈旧」、各自铸出**不同**的 token、各自把索引覆盖成自己，
    双双返回 ``minted``——一个 dedup key 两枚活跃 token，同一批改动被重复注入 / 重复发起 RVF，
    R25 的注入幂等硬保证当场失效。

    两条到达方式都要钉住，因为它们是两条独立的真实链路：

      * ``automatic_stop_hook_path``：token 自然 TTL 过期且无 run anchor 不可复活 ⇒ 索引陈旧。
        Stop hook 侧不需要任何前置条件就能走到这里。
      * ``explicit_user_request_path``：agent ``decline`` 让 token 进终态 ⇒ 索引同样陈旧。这条
        路径在「手动触发豁免 declined 冷却」落地之前不可达（闸二会先 ``declined_cooldown`` 早退），
        正是那次豁免把它打开的。

    每轮用 ``Barrier`` 把两个线程对齐到同一瞬间。修复前实测两条路径各 20/20 轮双双 ``minted``；
    修复后必须 0 轮违约，且败者刚铸的 token 文件要被 ``_discard_token_record`` 真正删掉，不留孤儿。
    """
    store = _load_decision_token_store_module()
    minted_at = "2026-08-21T00:00:00Z"
    # 铸造频率熔断（默认阈值 3）与本用例无关，抬高上限免得多轮铸造把它撞停。
    circuit_breaker_max_requests_disabled = 10_000

    def run_race_rounds(*, path_name: str, initiated_by_explicit_user_request: bool) -> None:
        roots = _isolated_decision_token_store_roots(tmp / path_name)
        rounds_with_more_than_one_active_token: list[dict] = []
        rounds_with_orphaned_token_file: list[dict] = []

        for round_index in range(20):
            session_id = f"{path_name}-sess-{round_index}"
            candidate_fingerprint = f"{path_name}-fingerprint-{round_index}"

            def mint_for_this_round(**overrides: object):
                return _mint_out_of_band_decision_token(
                    store,
                    roots,
                    session_id=session_id,
                    candidate_fingerprint=candidate_fingerprint,
                    circuit_breaker_max_requests=circuit_breaker_max_requests_disabled,
                    **overrides,
                )

            predecessor = mint_for_this_round(now=store.parse_timestamp(minted_at))
            assert predecessor.outcome == store.MINT_OUTCOME_MINTED, predecessor

            if initiated_by_explicit_user_request:
                # agent 谢绝 ⇒ 前任进终态（并写下 declined 冷却，由手动路径自己清掉）。
                store.mark_declined(
                    predecessor.token,
                    decline_kind="structural_not_needed", reason="agent_declined_for_stale_index_race_test",
                    root=roots["decision_token_root_dir"],
                    cooldown_root=roots["declined_fingerprint_root_dir"],
                    now=store.parse_timestamp(minted_at),
                )
                raced_at = store.parse_timestamp(minted_at)
            else:
                # 前任自然过期且无 run anchor ⇒ 不可复活，索引同样陈旧。
                raced_at = store.parse_timestamp("2026-08-21T01:00:00Z")

            start_barrier = threading.Barrier(2)
            mint_results: list = []
            thread_failures: list[BaseException] = []
            results_lock = threading.Lock()

            def mint_concurrently_on_the_same_dedup_key() -> None:
                try:
                    start_barrier.wait()
                    result = mint_for_this_round(
                        now=raced_at,
                        initiated_by_explicit_user_request=initiated_by_explicit_user_request,
                        explicit_user_request_provenance=(
                            store.EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT
                            if initiated_by_explicit_user_request
                            else None
                        ),
                    )
                    with results_lock:
                        mint_results.append(result)
                except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程断言
                    thread_failures.append(exc)

            threads = [
                threading.Thread(target=mint_concurrently_on_the_same_dedup_key) for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive(), "占坑互斥不得把任一线程永久堵死（疑似自锁死）"
            assert thread_failures == [], thread_failures
            assert len(mint_results) == 2, mint_results

            outcomes = sorted(result.outcome for result in mint_results)
            tokens = {result.token for result in mint_results}
            if outcomes != sorted(
                [store.MINT_OUTCOME_MINTED, store.MINT_OUTCOME_DEDUPLICATED]
            ) or len(tokens) != 1:
                rounds_with_more_than_one_active_token.append(
                    {"round": round_index, "outcomes": outcomes, "tokens": sorted(tokens)}
                )
                continue

            # 去重索引必须指向那枚唯一的胜者，且胜者仍然活跃。
            winner_token = next(iter(tokens))
            index_path = store._dedup_index_path(
                "/wt", session_id, candidate_fingerprint, roots["decision_token_root_dir"]
            )
            index_payload = store._read_dedup_index(index_path)
            assert index_payload is not None and index_payload["token"] == winner_token, (
                index_payload,
                winner_token,
            )
            assert (
                store.read_decision_token(
                    winner_token, root=roots["decision_token_root_dir"], now=raced_at
                ).state
                in store.ACTIVE_NONTERMINAL_STATES
            )

            # 败者刚铸的那枚 token 必须已被丢弃：本轮磁盘上只该留下「前任 + 胜者」两个 token 文件。
            token_files_for_this_round = {
                path.stem
                for path in store.decision_token_root(roots["decision_token_root_dir"]).glob(
                    "*.json"
                )
            }
            expected_tokens_still_on_disk = {predecessor.token, winner_token}
            orphans = token_files_for_this_round - expected_tokens_still_on_disk
            if orphans:
                rounds_with_orphaned_token_file.append(
                    {"round": round_index, "orphaned_tokens": sorted(orphans)}
                )
            # 逐轮清干净，让「孤儿」判定只反映本轮。
            for path in store.decision_token_root(roots["decision_token_root_dir"]).glob("*.json"):
                path.unlink()

        assert rounds_with_more_than_one_active_token == [], (
            path_name,
            rounds_with_more_than_one_active_token,
        )
        assert rounds_with_orphaned_token_file == [], (path_name, rounds_with_orphaned_token_file)

    run_race_rounds(
        path_name="automatic_stop_hook_path", initiated_by_explicit_user_request=False
    )
    run_race_rounds(
        path_name="explicit_user_request_path", initiated_by_explicit_user_request=True
    )


# ---------------------------------------------------------------------------
# C-②：``withdrawn`` 的两种成因对熔断额度的处置必须相反
# ---------------------------------------------------------------------------

def test_stranded_delivery_withdrawal_retains_mint_request_charge_so_repeat_stranding_backs_off(
    tmp: Path,
) -> None:
    """带外投递从未落地而撤回 ⇒ **不**冲销熔断额度，于是连续空转会如实退避。

    与 ``test_two_consecutive_rewind_withdrawals_do_not_trip_circuit_breaker_on_third_ask``
    是**同一函数、相反期望**的一对，两条必须同时绿——这正是「按成因分叉」的意义所在：

      * 用户 rewind：他还想被问，冲销额度让第三次征询仍能铸（INV-C 原义，那条用例钉住）；
      * 投递空转：他**根本没看到过**横幅，且通道坏着时重铸多少次都会照样空转。此时烧掉额度、
        让熔断如实跳闸，正是所需的退避。

    实测（2026-08-21）在冲销行为下出现过一个 session 内 ``minted → withdrawn`` 连铸三枚、全程
    从未经过 ``injected`` 的空转环——那正是这条用例要钉死的形态。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "stranded-retains-charge")
    session_id = "stranded-backoff-sess"

    outcomes: list[str] = []
    for ask_index in range(1, 4):
        result = _mint_out_of_band_decision_token(
            store,
            roots,
            session_id=session_id,
            candidate_fingerprint=f"fingerprint-after-edit-{ask_index}",
        )
        outcomes.append(result.outcome)
        if result.outcome != store.MINT_OUTCOME_MINTED:
            break
        store.mark_withdrawn(
            result.token,
            reason="decision_point_delivery_never_became_a_turn",
            withdrawal_cause=(
                store.DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN
            ),
            root=roots["decision_token_root_dir"],
            circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
        )

    # 两枚铸出后空转撤回的 token，额度**全部**留在账上（阈值 3 ⇒ 第三次请求即跳闸）。第三次请求
    # 被熔断拒掉、没铸出 token，它自己那一份当场冲销——不冲销的话熔断会自我续命。
    assert outcomes == [
        store.MINT_OUTCOME_MINTED,
        store.MINT_OUTCOME_MINTED,
        store.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED,
    ], outcomes
    assert (
        store.count_recent_mint_requests(
            session_id, root=roots["circuit_breaker_root_dir"]
        )
        == 2
    ), _mint_request_ledger_lines(store, roots, session_id)
    # 撤回不该留下任何冲销行；唯一的冲销行属于被熔断拒掉的那次请求。
    reversal_lines = [
        line
        for line in _mint_request_ledger_lines(store, roots, session_id)
        if store.MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD in line
    ]
    assert [line.get("token") for line in reversal_lines] == [None], reversal_lines
    assert all(
        line.get("reversed_because")
        == "circuit_breaker_refused_this_mint_request_so_no_token_was_minted"
        for line in reversal_lines
    ), reversal_lines
    # 第三次铸造把窗口内计数推到阈值 ⇒ 熔断跳闸，第四次不再铸。
    assert outcomes[-1] in (
        store.MINT_OUTCOME_MINTED,
        store.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED,
    ), outcomes
    fourth = _mint_out_of_band_decision_token(
        store,
        roots,
        session_id=session_id,
        candidate_fingerprint="fingerprint-after-edit-4",
    )
    assert fourth.outcome == store.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED, (
        fourth.outcome,
        fourth.message,
    )
    # 撤回同样不写冷却表（这一半两种成因共享）。
    assert sorted(roots["declined_fingerprint_root_dir"].glob("*.json")) == []


def test_unknown_withdrawal_cause_fails_closed_to_retaining_mint_request_charge(
    tmp: Path,
) -> None:
    """未知成因 ⇒ **不冲销**（fail closed），且不抛。

    冲销与否是白名单判定：将来新增一个成因而忘了想清楚它该不该退额度时，默认落到「不冲销」
    这一侧——多退避一次只是延后一次征询，而错误地无限冲销会让熔断整个失效（漏审方向）。
    同时它不能抛：撤回对账是 best-effort 链路的一环，为一个拼错的成因串打断 Stop 决策更危险。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "unknown-cause")
    session_id = "unknown-cause-sess"

    mint = _mint_out_of_band_decision_token(store, roots, session_id=session_id)
    assert mint.outcome == store.MINT_OUTCOME_MINTED, mint
    record = store.mark_withdrawn(
        mint.token,
        reason="some-future-cause-nobody-classified",
        withdrawal_cause="a-cause-that-does-not-exist-yet",
        root=roots["decision_token_root_dir"],
        circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
    )
    assert record.payload["state"] == store.STATE_WITHDRAWN, record.payload
    assert record.payload["withdrawn_mint_request_charge_retained"] is True, record.payload
    assert (
        store.count_recent_mint_requests(
            session_id, root=roots["circuit_breaker_root_dir"]
        )
        == 1
    )
    # 去重索引仍必须被清掉——那一半与成因无关（少清 = 下一次 Stop 静默停 = 漏审方向）。
    assert "withdrawn_dedup_index_path" in record.payload, record.payload


def test_banner_that_became_a_turn_but_failed_to_advance_reverses_mint_request_charge(
    tmp: Path,
) -> None:
    """横幅**确实成为过一条 turn**、只是记账没跟上 ⇒ 冲销额度，连续三次也不该把熔断打跳。

    与 ``test_stranded_delivery_withdrawal_retains_mint_request_charge_so_repeat_stranding_backs_off``
    是同一函数、相反期望的一对：那条钉「通道坏着要退避」，这条钉「通道是通的就不许退避」。
    两条同时绿才说明分叉判据落在**成因**上，而不是落在「凡是搁浅撤回一律计费」这个过粗的规则上。

    对账侧只在投递意图里有 UPS 记下的「决策正文已注入」这份正向证据时才用本成因（审计 M9）：59ffd
    之后粘贴通道只送一行无指令的边界标记，「标记成了 turn」不再等于「征询到达了 agent」。标记落地
    而正文从未注入（UPS 没跑 / 超时 / 崩溃 / 到达时 token 已过 TTL）用的是另一个**不冲销**的成因，
    见下方最后一组断言。征询确实到达过却按空转计费的话，连续 3 次记账失败就会把铸造熔断打跳，
    整个 session 的自动发起被静默封禁一个窗口（默认 3600s）——那是**漏审**方向。
    """
    store = _load_decision_token_store_module()
    roots = _isolated_decision_token_store_roots(tmp / "became-a-turn-reverses-charge")
    session_id = "became-a-turn-sess"

    for ask_index in range(1, 5):
        result = _mint_out_of_band_decision_token(
            store,
            roots,
            session_id=session_id,
            candidate_fingerprint=f"fingerprint-after-edit-{ask_index}",
        )
        # 每一轮都必须铸得出来：额度被逐轮冲销掉了，熔断永远够不到阈值。
        assert result.outcome == store.MINT_OUTCOME_MINTED, (
            ask_index,
            result.outcome,
            _mint_request_ledger_lines(store, roots, session_id),
        )
        record = store.mark_withdrawn(
            result.token,
            reason="decision_point_delivery_stranded:out_of_band_delivery_became_a_turn_but_token_advance_failed",
            withdrawal_cause=(
                store
                .DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED
            ),
            root=roots["decision_token_root_dir"],
            circuit_breaker_root_dir=roots["circuit_breaker_root_dir"],
        )
        # 如实记账的**反面**：这一支不得留下「额度被保留」的标记，且必须点名它冲销了哪一次铸造。
        assert "withdrawn_mint_request_charge_retained" not in record.payload, record.payload
        assert (
            "withdrawn_mint_request_charge_reversal_of_record_id" in record.payload
        ), record.payload

    assert (
        store.count_recent_mint_requests(
            session_id, root=roots["circuit_breaker_root_dir"]
        )
        == 0
    ), _mint_request_ledger_lines(store, roots, session_id)
    # 白名单是这条行为的唯一真相源：成因写在白名单里，才有上面的冲销。
    assert (
        store.DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED
        in store.DECISION_POINT_WITHDRAWAL_CAUSES_THAT_REVERSE_MINT_REQUEST_CHARGE
    )
    # 空转那一支必须**留在白名单外**——否则退避就没了，实测过的连铸空转环会立刻回来。
    assert (
        store.DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN
        not in store.DECISION_POINT_WITHDRAWAL_CAUSES_THAT_REVERSE_MINT_REQUEST_CHARGE
    )
    # 「标记成了 turn、正文却从未注入」同样是空转（M9），同样留在白名单外。
    assert (
        store.DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_BOUNDARY_MARKER_BECAME_A_TURN_BUT_BANNER_BODY_NEVER_INJECTED
        not in store.DECISION_POINT_WITHDRAWAL_CAUSES_THAT_REVERSE_MINT_REQUEST_CHARGE
    )
    assert (
        store.DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_BOUNDARY_MARKER_BECAME_A_TURN_BUT_BANNER_BODY_NEVER_INJECTED
        in store.DECISION_POINT_WITHDRAWAL_CAUSES
    )
    # 撤回同样不写冷却表（这一半三种成因共享）。
    assert sorted(roots["declined_fingerprint_root_dir"].glob("*.json")) == []
