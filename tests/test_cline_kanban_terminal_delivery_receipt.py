"""cline-kanban「终端投递回执」契约在 RVF 侧的判读、CLI 降级态与 per-task 派发单飞闸。

这三样东西合起来回答一个问题：**RVF 凭什么说一条 follow-up「送到了」。** 2026-08-08 那次
事故（注入的 review 躺了 49 分钟才落地，期间 RVF 一直以为已送达）的成因不是投递失败，而是
**回执口径不诚实**——RVF 把一个「mutate 之前读的快照推断」当成了投递结果。所以这里的测试
不追覆盖率，只钉四类东西：

1. **任何「不知道」都不得被读成「送到了」。** 非终态、未知取值、遗留镜像、字段缺失，四条路
   径各一条测试。这是本模块唯一真正重要的不变量：反方向的错误（把未知当成功）会让一条从未
   送达的 review 被静默丢弃，不可恢复。
2. **契约里那条显式警告被执行。** 「``cancelled_before_delivery`` 不等于是这次取消拦下的」——
   判「是不是被取消」必须读 reason，读 ``cancel_result`` 会把一条早就失败的记录误报成取消。
3. **三种降级运行态互不混淆。** 接口不存在（契约第 8 条：合法运行态）、CLI 硬超时
   （``exit 124`` + 空 stdout = 还没结论）、账本无此记录 —— 没有一种是「投递失败」。
4. **等终态的超时不得配到契约预算之下。** 那是对方的保证不是我方的偏好，配小了会在正常路径
   上把一条还在有界收敛中的投递误判成 lost，从而重投出第二份 review。

⚠️ 全部测试只用 ``tmp_path`` 与假 CLI，**不碰真实的 ``~/.rvf``**，也不需要任何 kanban runtime。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS_DIR = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"

from core.cline_kanban_terminal_delivery.cline_kanban_terminal_delivery_receipt import (  # noqa: E402
    DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS,
    KNOWN_TERMINAL_DELIVERY_FAILURE_REASONS,
    RECEIPT_FIELD_SOURCE_ABSENT,
    RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR,
    RECEIPT_FIELD_SOURCE_TERMINAL_DELIVERY_STATUS,
    REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY,
    REDISPATCH_DISPOSITION_NOT_NEEDED,
    REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
    REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION,
    TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION,
    TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS,
    TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV,
    TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED,
    TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN,
    TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
    awaiting_receipt_exceeded_contract_convergence_budget,
    cancel_receipt_indicates_cancellation_rather_than_another_failure,
    cancel_receipt_indicates_message_already_reached_agent,
    classify_cline_kanban_terminal_delivery_receipt,
    describe_terminal_delivery_receipt_for_operator,
    redispatch_disposition_for_terminal_delivery_failure_reason,
    resolve_terminal_delivery_status_convergence_timeout_seconds,
)


def _load_script_module(name: str, filename: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


kanban_client = _load_script_module(
    "cline_kanban_client_under_test", "cline_kanban_client.py"
)
followup_lock = _load_script_module(
    "kanban_followup_lock_under_test", "kanban_followup_lock.py"
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 1. 任何「不知道」都不得被读成「送到了」
# ---------------------------------------------------------------------------


def test_accepted_pending_submit_confirmation_is_never_treated_as_delivered() -> None:
    """契约点名的事故成因：非终态被当成成功。这条一旦回归，49 分钟事故的形态就回来了。"""
    receipt = classify_cline_kanban_terminal_delivery_receipt(
        {
            "ok": True,
            "terminal_delivery_status": (
                TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION
            ),
            # 遗留镜像同时说 started —— 它**不得**把结论翻过来。
            "status": "started",
        }
    )
    assert receipt.is_delivered is False
    assert receipt.is_failed is False
    assert receipt.is_terminal is False
    assert receipt.is_awaiting_terminal_status is True
    assert receipt.receipt_field_source == RECEIPT_FIELD_SOURCE_TERMINAL_DELIVERY_STATUS


@pytest.mark.parametrize(
    "status",
    [
        TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED,
        TERMINAL_DELIVERY_STATUS_DELIVERED_QUEUED_BEHIND_ACTIVE_AGENT_TURN,
    ],
)
def test_both_delivered_states_are_terminal_and_delivered(status: str) -> None:
    receipt = classify_cline_kanban_terminal_delivery_receipt(
        {"terminal_delivery_status": status}
    )
    assert receipt.is_delivered is True
    assert receipt.is_terminal is True
    assert receipt.is_awaiting_terminal_status is False
    assert receipt.redispatch_disposition is None


def test_legacy_status_mirror_started_or_queued_is_not_delivery_evidence() -> None:
    """遗留 ``status`` 的 started/queued 是 **mutate 之前**读的快照推断，与投递结果无关。

    历史实现正是据它上报「已入队」并当作已确认（于是连 pending 对账记录都不写）。
    """
    for legacy in ("started", "queued", "pending"):
        receipt = classify_cline_kanban_terminal_delivery_receipt({"status": legacy})
        assert receipt.is_delivered is False, legacy
        assert receipt.is_failed is False, legacy
        assert receipt.is_awaiting_terminal_status is True, legacy
        assert receipt.receipt_field_source == RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR


def test_legacy_status_mirror_failed_is_a_trustworthy_failure_without_a_reason() -> None:
    """``failed`` 是契约**新增**的遗留镜像取值，旧实现产生不了它 ⇒ 结论可信，但拿不到 reason。"""
    receipt = classify_cline_kanban_terminal_delivery_receipt({"status": "failed"})
    assert receipt.is_failed is True
    assert receipt.terminal_delivery_status == TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED
    assert receipt.terminal_delivery_failure_reason is None
    assert receipt.receipt_field_source == RECEIPT_FIELD_SOURCE_LEGACY_STATUS_MIRROR
    # 不知道为什么失败 ⇒ 不自动连投。
    assert receipt.redispatch_disposition == REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION


def test_unknown_terminal_delivery_status_value_degrades_to_awaiting_not_delivered() -> None:
    """对方将来新增取值时，失败方向必须是「再查一轮」而不是「猜它是送达」。"""
    receipt = classify_cline_kanban_terminal_delivery_receipt(
        {"terminal_delivery_status": "delivered_and_frobnicated_v2"}
    )
    assert receipt.is_delivered is False
    assert receipt.is_awaiting_terminal_status is True


def test_absent_receipt_fields_degrade_to_awaiting() -> None:
    for payload in ({}, None, {"ok": True, "task_id": "t"}):
        receipt = classify_cline_kanban_terminal_delivery_receipt(payload)
        assert receipt.is_delivered is False
        assert receipt.is_awaiting_terminal_status is True
        assert receipt.receipt_field_source == RECEIPT_FIELD_SOURCE_ABSENT


def test_receipt_carries_the_keys_needed_to_query_it_again_later() -> None:
    receipt = classify_cline_kanban_terminal_delivery_receipt(
        {
            "task_id": "task-77",
            "idempotency_key": "rvf-run-1",
            "message_id": "terminal:task-77:rvf-run-1",
            "terminal_delivery_status": (
                TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION
            ),
        }
    )
    assert (receipt.task_id, receipt.idempotency_key) == ("task-77", "rvf-run-1")
    assert receipt.message_id == "terminal:task-77:rvf-run-1"


# ---------------------------------------------------------------------------
# 2. 失败 reason → 重投处置
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("no_active_terminal_session", REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION),
        (
            "terminal_prompt_readiness_timeout",
            REDISPATCH_DISPOSITION_ALLOWED_WITH_FRESH_IDEMPOTENCY_KEY,
        ),
        (
            "submit_confirmation_budget_exhausted",
            REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
        ),
        (
            "human_terminal_contention_timeout",
            REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION,
        ),
        (
            "agent_awaiting_user_decision_timeout",
            REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION,
        ),
        ("superseded_by_later_delivery", REDISPATCH_DISPOSITION_NOT_NEEDED),
        ("session_ended_before_delivery", REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION),
        ("cancelled_before_delivery", REDISPATCH_DISPOSITION_NOT_NEEDED),
        (
            "runtime_restarted_before_confirmation",
            REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION,
        ),
    ],
)
def test_every_contract_failure_reason_has_an_explicit_disposition(
    reason: str, expected: str
) -> None:
    assert reason in KNOWN_TERMINAL_DELIVERY_FAILURE_REASONS
    assert redispatch_disposition_for_terminal_delivery_failure_reason(reason) == expected
    receipt = classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
            "terminal_delivery_failure_reason": reason,
        }
    )
    assert receipt.is_failed is True
    assert receipt.redispatch_disposition == expected
    assert reason in describe_terminal_delivery_receipt_for_operator(receipt)


@pytest.mark.parametrize("reason", [None, "", "   ", "some_future_reason"])
def test_unknown_failure_reason_withholds_automatic_redispatch(reason) -> None:
    """未知 reason 既不自动连投（可能把文本叠进一个已有残留的输入框），也不静默放弃。"""
    assert (
        redispatch_disposition_for_terminal_delivery_failure_reason(reason)
        == REDISPATCH_DISPOSITION_REQUIRES_HUMAN_CONFIRMATION
    )


# ---------------------------------------------------------------------------
# 3. 契约 § 取消接口的那条显式警告
# ---------------------------------------------------------------------------


def test_cancel_result_answers_only_whether_the_message_reached_the_agent() -> None:
    assert (
        cancel_receipt_indicates_message_already_reached_agent(
            {"cancel_result": "already_delivered"}
        )
        is True
    )
    assert (
        cancel_receipt_indicates_message_already_reached_agent(
            {"cancel_result": "cancelled_before_delivery"}
        )
        is False
    )


def test_cancelled_before_delivery_does_not_prove_this_cancel_stopped_it() -> None:
    """契约的警告框：一条**在取消之前就已因别的原因失败**的记录同样回 cancelled_before_delivery。

    所以「是不是被取消的」只能读 reason。用 cancel_result 回答这个问题，会把
    ``no_active_terminal_session`` 误报成「我取消掉了」——旧实现的反向撒谎正是这一族。
    """
    already_failed_for_another_reason = {
        "cancel_result": "cancelled_before_delivery",
        "terminal_delivery_status": TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
        "terminal_delivery_failure_reason": "no_active_terminal_session",
    }
    assert (
        cancel_receipt_indicates_message_already_reached_agent(
            already_failed_for_another_reason
        )
        is False
    )
    assert (
        cancel_receipt_indicates_cancellation_rather_than_another_failure(
            already_failed_for_another_reason
        )
        is False
    )

    genuinely_cancelled = {
        "cancel_result": "cancelled_before_delivery",
        "terminal_delivery_status": TERMINAL_DELIVERY_STATUS_DELIVERY_FAILED,
        "terminal_delivery_failure_reason": "cancelled_before_delivery",
    }
    assert (
        cancel_receipt_indicates_cancellation_rather_than_another_failure(genuinely_cancelled)
        is True
    )


# ---------------------------------------------------------------------------
# 4. 收敛预算
# ---------------------------------------------------------------------------


def test_convergence_timeout_can_be_raised_but_never_configured_below_the_contract_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV, raising=False)
    assert (
        resolve_terminal_delivery_status_convergence_timeout_seconds()
        == DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS
    )
    # 配大 → 照办（对方说这个数还可能再收敛，所以旋钮必须存在）。
    monkeypatch.setenv(TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV, "900")
    assert resolve_terminal_delivery_status_convergence_timeout_seconds() == 900.0
    # 配小 → 抬回契约下界。计划原文写的「TTL 从 15min 压到分钟级」正是这条会误判 lost 的配置。
    monkeypatch.setenv(TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV, "30")
    assert (
        resolve_terminal_delivery_status_convergence_timeout_seconds()
        == TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS
    )
    # 坏值 → 回落默认（默认本身已在下界之上）。
    monkeypatch.setenv(TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_ENV, "not-a-number")
    assert (
        resolve_terminal_delivery_status_convergence_timeout_seconds()
        == DEFAULT_TERMINAL_DELIVERY_STATUS_CONVERGENCE_TIMEOUT_SECONDS
    )


def test_awaiting_receipt_is_only_declared_stale_after_the_contract_budget() -> None:
    now = datetime.now(timezone.utc)
    fresh = classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": (
                TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION
            ),
            "terminal_delivery_status_updated_at": _iso(now - timedelta(seconds=30)),
        }
    )
    assert awaiting_receipt_exceeded_contract_convergence_budget(fresh) is False

    overdue = classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": (
                TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION
            ),
            "terminal_delivery_status_updated_at": _iso(now - timedelta(seconds=3600)),
        }
    )
    assert awaiting_receipt_exceeded_contract_convergence_budget(overdue) is True

    # 终态永远不算「超预算」；拿不到状态戳时**不下这个结论**（没有证据就不判），
    # 交给 pending marker 自己的 TTL 兜底。
    delivered = classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": TERMINAL_DELIVERY_STATUS_DELIVERED_AND_SUBMIT_CONFIRMED,
            "terminal_delivery_status_updated_at": _iso(now - timedelta(days=3)),
        }
    )
    assert awaiting_receipt_exceeded_contract_convergence_budget(delivered) is False
    undated = classify_cline_kanban_terminal_delivery_receipt(
        {
            "terminal_delivery_status": (
                TERMINAL_DELIVERY_STATUS_ACCEPTED_PENDING_SUBMIT_CONFIRMATION
            )
        }
    )
    assert awaiting_receipt_exceeded_contract_convergence_budget(undated) is False


# ---------------------------------------------------------------------------
# 5. CLI 的三种降级运行态
# ---------------------------------------------------------------------------


def _write_fake_kanban(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "fake_kanban.py"
    fake.write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n" + body,
        encoding="utf-8",
    )
    return f"{sys.executable} {fake}"


def test_cli_hard_timeout_is_no_verdict_yet_and_never_a_delivery_failure(
    tmp_path: Path,
) -> None:
    """``exit 124`` + 空 stdout 是 kanban CLI 的硬超时约定，语义是「还没结论」。

    这与本仓 cursor-agent 那条「空 stdout ⇒ 失败」的判据方向相反，两处不能共用同一个包装器。
    """
    task_cmd = _write_fake_kanban(tmp_path, "sys.exit(124)\n")
    with pytest.raises(kanban_client.KanbanTerminalDeliveryVerdictNotYetAvailableError):
        kanban_client.read_task_message_terminal_delivery_status(
            task_cmd=task_cmd, repo=tmp_path, task_id="t", idempotency_key="k"
        )


def test_missing_subcommand_is_a_legal_runtime_state(tmp_path: Path) -> None:
    """契约第 8 条：接口不存在时退回纯 RVF 侧语义中和，不得记成一次投递失败。"""
    task_cmd = _write_fake_kanban(
        tmp_path,
        "print(\"error: unknown command 'message-status'\", file=sys.stderr)\nsys.exit(1)\n",
    )
    with pytest.raises(kanban_client.KanbanTerminalDeliveryInterfaceUnavailableError):
        kanban_client.read_task_message_terminal_delivery_status(
            task_cmd=task_cmd, repo=tmp_path, task_id="t", idempotency_key="k"
        )


def test_unknown_idempotency_key_is_distinct_from_delivery_failure(tmp_path: Path) -> None:
    """失败记录现在**保留**在账本里，所以「查不到」只可能是从没请求过 —— 两件事不能混。"""
    task_cmd = _write_fake_kanban(
        tmp_path,
        "print(json.dumps({'ok': False, 'error': 'unknown_idempotency_key'}))\nsys.exit(1)\n",
    )
    with pytest.raises(kanban_client.KanbanTerminalDeliveryRecordNotFoundError):
        kanban_client.read_task_message_terminal_delivery_status(
            task_cmd=task_cmd, repo=tmp_path, task_id="t", idempotency_key="k"
        )


def test_other_ok_false_json_stays_an_ordinary_failure_but_keeps_its_whole_reason(
    tmp_path: Path,
) -> None:
    """非零退出 + **其他** ``{ok:false}`` JSON：既不是第四种降级运行态，也不许把原因吞掉。

    两半各钉一条，因为它们的方向相反：

    1. 它**不是**降级运行态。三个具名类各对应一种「拿不到结论但对方是健康的」形态；一个
       没见过的 ``error`` 码只是普通失败，上层（``_query_kanban_terminal_delivery_receipt``）
       本来就该把它归进 ``query_failed``。这里刻意断言抛的是**裸** ``KanbanError``，钉住
       「不要为它发明第四种分类」这个判定 —— 发明了也只会被上层的 ``except Exception``
       重新压回 ``query_failed``。
    2. 但失败**原因**必须完整带出来。同一个函数在「无 stdout」那一支已经写死了口径：非零
       退出必须带 stderr 原文抛出，否则真正的失败原因会被整个吞掉。有 JSON 的这一支过去
       靠 ``parse_json_stdout`` 兜底，而它只抛裸的 ``error`` 码 —— ``message`` 与 stderr
       双双消失，同一个函数两条路径口径自相矛盾。CLI ``main()`` 把这段文本打给运维看
       （exit 2），所以吞掉的是人排障时唯一能看到的东西。
    """
    task_cmd = _write_fake_kanban(
        tmp_path,
        "print(json.dumps({'ok': False, 'error': 'internal_error',\n"
        "                  'message': 'ledger database is locked'}))\n"
        "print('sqlite busy after 5 retries', file=sys.stderr)\n"
        "sys.exit(1)\n",
    )
    with pytest.raises(kanban_client.KanbanError) as excinfo:
        kanban_client.read_task_message_terminal_delivery_status(
            task_cmd=task_cmd, repo=tmp_path, task_id="t", idempotency_key="k"
        )
    # 1. 普通失败，不是三种降级运行态里的任何一种。
    assert type(excinfo.value) is kanban_client.KanbanError
    message = str(excinfo.value)
    # 2. error 码、payload 里的补充说明、stderr 原文、退出码，一个都不能少。
    assert "internal_error" in message
    assert "ledger database is locked" in message
    assert "sqlite busy after 5 retries" in message
    assert "message-status" in message


def test_message_status_happy_path_returns_the_contract_fields(tmp_path: Path) -> None:
    task_cmd = _write_fake_kanban(
        tmp_path,
        "assert argv[0] == 'message-status', argv\n"
        "print(json.dumps({'ok': True, 'terminal_delivery_status': 'delivery_failed',\n"
        "                  'terminal_delivery_failure_reason': 'no_active_terminal_session',\n"
        "                  'terminal_delivery_status_updated_at': '2026-08-14T00:00:00Z'}))\n",
    )
    payload = kanban_client.read_task_message_terminal_delivery_status(
        task_cmd=task_cmd, repo=tmp_path, task_id="task-77", idempotency_key="key-1"
    )
    receipt = classify_cline_kanban_terminal_delivery_receipt(payload)
    assert receipt.is_failed is True
    assert receipt.redispatch_disposition == REDISPATCH_DISPOSITION_WAIT_FOR_EXTERNAL_CONDITION
    # 回执要能被原样回写进账本 —— 查询键必须原样带回。
    assert payload["task_id"] == "task-77"
    assert payload["idempotency_key"] == "key-1"


def test_message_cancel_receipt_is_returned_verbatim_for_the_named_judgements(
    tmp_path: Path,
) -> None:
    task_cmd = _write_fake_kanban(
        tmp_path,
        "assert argv[0] == 'message-cancel', argv\n"
        "print(json.dumps({'ok': True, 'cancel_result': 'already_delivered',\n"
        "                  'terminal_delivery_status': 'delivered_and_submit_confirmed'}))\n",
    )
    payload = kanban_client.cancel_task_message_delivery(
        task_cmd=task_cmd, repo=tmp_path, task_id="task-77", idempotency_key="key-1"
    )
    assert cancel_receipt_indicates_message_already_reached_agent(payload) is True
    assert cancel_receipt_indicates_cancellation_rather_than_another_failure(payload) is False


def test_short_wait_flag_is_only_passed_when_a_positive_budget_is_requested(
    tmp_path: Path,
) -> None:
    recorder = tmp_path / "argv.json"
    task_cmd = _write_fake_kanban(
        tmp_path,
        f"open({str(recorder)!r}, 'w').write(json.dumps(argv))\n"
        "print(json.dumps({'ok': True, 'message_id': 'terminal:t:k',\n"
        "                  'terminal_delivery_status': 'accepted_pending_submit_confirmation'}))\n",
    )
    kanban_client.send_task_message(
        task_cmd=task_cmd,
        repo=tmp_path,
        task_id="t",
        prompt="hi",
        prompt_file=None,
        source="review-validate-fix",
        idempotency_key="k",
        attempt_id=None,
    )
    assert "--wait-for-terminal-status" not in json.loads(recorder.read_text(encoding="utf-8"))

    kanban_client.send_task_message(
        task_cmd=task_cmd,
        repo=tmp_path,
        task_id="t",
        prompt="hi",
        prompt_file=None,
        source="review-validate-fix",
        idempotency_key="k",
        attempt_id=None,
        wait_for_terminal_status_timeout_ms=1500,
    )
    argv = json.loads(recorder.read_text(encoding="utf-8"))
    assert "--wait-for-terminal-status" in argv
    assert argv[argv.index("--wait-timeout-ms") + 1] == "1500"


# ---------------------------------------------------------------------------
# 6. per-task 派发单飞闸（契约第 5 条）
# ---------------------------------------------------------------------------


def test_dispatch_single_flight_refuses_a_second_concurrent_dispatch(tmp_path: Path) -> None:
    """并发投同一个 task 会让先到的那条被判 ``superseded_by_later_delivery``，故必须串行化。

    换新 idempotency key 之后，这道闸是唯一还在防重复派发的东西（历史上由那枚稳定键兼职）。
    """
    with followup_lock.dispatch_single_flight_for_task("task-77", root=tmp_path) as first:
        assert first is True
        with followup_lock.dispatch_single_flight_for_task("task-77", root=tmp_path) as second:
            assert second is False
        # 另一个 task 不受影响。
        with followup_lock.dispatch_single_flight_for_task("task-88", root=tmp_path) as other:
            assert other is True
    # 释放后可以重新拿到。
    with followup_lock.dispatch_single_flight_for_task("task-77", root=tmp_path) as again:
        assert again is True


def test_dispatch_single_flight_never_blocks_dispatch_when_it_cannot_apply(
    tmp_path: Path,
) -> None:
    """这道闸是防并发的，不是安全边界 —— 缺 task_id 一律放行，绝不能让它把派发整个堵死。"""
    for task_id in (None, "", "   "):
        with followup_lock.dispatch_single_flight_for_task(task_id, root=tmp_path) as acquired:
            assert acquired is True


def test_dispatch_single_flight_lock_file_is_not_mistaken_for_a_pending_marker(
    tmp_path: Path,
) -> None:
    """闸文件必须以 ``.`` 开头 —— 否则跨 task stranded-sweep 会把它当成第二条 pending 记录。"""
    with followup_lock.dispatch_single_flight_for_task("task-77", root=tmp_path):
        pass
    assert followup_lock.iter_pending_markers(root=tmp_path) == []


def test_pending_marker_carries_the_key_needed_to_query_the_real_verdict(
    tmp_path: Path,
) -> None:
    """没有 idempotency_key，对账就只能退回「TTL 到了还没被清 ⇒ 大概是丢了」的猜测。"""
    followup_lock.write_pending_marker(
        task_id="task-77",
        session_id="sess",
        run_id="run-1",
        run_dir=str(tmp_path / "run"),
        repo=str(tmp_path),
        cwd=str(tmp_path),
        token="tok",
        delivery_channel="terminal",
        idempotency_key="rvf-run-1",
        terminal_delivery_status="accepted_pending_submit_confirmation",
        root=tmp_path,
    )
    marker = followup_lock.read_pending_marker(task_id="task-77", root=tmp_path)
    assert marker["idempotency_key"] == "rvf-run-1"
    assert marker["terminal_delivery_status"] == "accepted_pending_submit_confirmation"
    assert followup_lock.pending_redispatch_attempt_count(marker) == 0
    # 历史 marker（F2a 之前写的）没有这些字段，读侧必须容忍。
    assert followup_lock.pending_redispatch_attempt_count({"state": "dispatched_unconfirmed"}) == 0
    assert followup_lock.pending_redispatch_attempt_count(None) == 0


def test_pending_marker_family_never_touches_the_real_state_root(tmp_path: Path) -> None:
    """本仓有过测试往真实根写残留的前科，这条钉住「传了 root 就只写 root」。"""
    followup_lock.write_pending_marker(
        task_id="task-isolation-probe",
        session_id=None,
        run_id="r",
        run_dir="d",
        repo=None,
        cwd=None,
        token=None,
        delivery_channel=None,
        root=tmp_path,
    )
    written = sorted(p.name for p in (tmp_path / "kanban-followup-dispatched").glob("*.json"))
    assert written == ["task-task-isolation-probe.json"]
    real_root = Path.home() / ".rvf" / "kanban-followup-dispatched"
    assert not (real_root / "task-task-isolation-probe.json").exists()


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    raise SystemExit(pytest.main([__file__, "-q"]))
