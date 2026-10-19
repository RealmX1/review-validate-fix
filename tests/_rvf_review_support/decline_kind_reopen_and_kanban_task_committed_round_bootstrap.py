#!/usr/bin/env python3
"""修复包 F4（谢绝语义与重新征询）测试簇。

覆盖：
* D1 谢绝分型：``--decline-kind`` 必填；结构性谢绝照旧进指纹冷却 + 会话 snooze；延后型谢绝两者都不进，
  只挡「谢绝后原样停下」的那次 Stop，新回合 / HEAD 变化 / 候选内容变化任一出现即重新征询，并把延后
  理由盖进候选摘要；用户显式触发推翻延后。
* 熔断不自我续命：被熔断拒掉的铸造请求不再占额度。
* D2 agent 按开发者口头要求撤回自己的谢绝：只撤回本会话最近一枚、且必须是 declined；来源与开发者手动
  触发区分。
* D3 Kanban task 首轮（无高水位）以 task 分支点作 committed-round 下界 bootstrap。
* 回溯验收：那 3 例会被 T3 压住的延后型谢绝（2c00c 08-18、01200 08-21 16:05、ac08e 08-22 03:12）按
  真实形态构造，新语义下重新征询；同一形态按结构性谢绝仍被 snooze（T3 效果不变）。

隔离：所有落盘根都是 tmp；改环境变量的用例用上下文管理器还原；git 仓建在 tmp 下。共享 helper 经
aggregator 的 ``inject()`` 推入。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

__all__ = [
    "test_decline_kind_is_required_and_unknown_kind_is_rejected_without_touching_the_token",
    "test_deferred_until_condition_decline_writes_neither_fingerprint_cooldown_nor_session_scope_snooze",
    "test_deferred_until_condition_decline_is_reasked_only_after_new_turn_head_change_or_content_change",
    "test_structural_decline_supersedes_earlier_deferred_until_condition_decline",
    "test_explicit_user_request_overrides_deferred_until_condition_decline_and_records_provenance",
    "test_circuit_breaker_refused_mint_requests_do_not_keep_the_breaker_tripped",
    "test_extra_token_payload_fields_cannot_override_system_written_explicit_user_request_fields",
    "test_kanban_task_committed_round_baseline_bootstraps_from_branch_point_when_never_reviewed",
    "test_retrospective_deferred_declines_are_reasked_while_structural_declines_stay_snoozed",
]


def inject(**deps: object) -> None:
    globals().update(deps)


_STRUCTURAL = "structural_not_needed"
_DEFERRED = "deferred_until_condition"


def _summary(paths: list[str], *, head: str, unreviewed_lines: int, candidate_path_count: int | None = None) -> dict:
    summary = _candidate_summary_carrying_scope_identity(  # noqa: F821 - injected
        paths=paths, head=head, unreviewed_lines=unreviewed_lines
    )
    summary["candidate_path_count"] = len(paths) if candidate_path_count is None else candidate_path_count
    return summary


def _mint(dts, roots, *, session_id: str, fingerprint: str, summary: dict | None, now, **extra):
    return dts.mint_decision_token(
        worktree="/wt", session_id=session_id, candidate_fingerprint=fingerprint,
        candidate_summary=summary, now=now, **roots, **extra,
    )


@contextlib.contextmanager
def _environment(**overrides: str | None) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ---------------------------------------------------------------------------
# D1：谢绝分型
# ---------------------------------------------------------------------------

def test_decline_kind_is_required_and_unknown_kind_is_rejected_without_touching_the_token(tmp: Path) -> None:
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    cli = load_rvf_agent_decision_cli_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    minted = _mint(dts, roots, session_id="s", fingerprint="fp", summary=None, now=t0)
    try:
        dts.mark_declined(
            minted.token, decline_kind="later_maybe", reason="x",
            root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
        )
        raise AssertionError("未知 decline_kind 应被拒绝")
    except dts.DecisionTokenError:
        pass
    still = dts.read_decision_token(minted.token, root=roots["root"], now=t0)
    assert still.state == dts.STATE_MINTED, still
    assert not dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fp", cooldown_root=roots["cooldown_root"]
    )
    # CLI：不带 --decline-kind 直接是用法错误（项目未分发，不留无类型兼容）。
    try:
        cli.build_argument_parser().parse_args(["decline", "--token", minted.token, "--reason", "r"])
        raise AssertionError("decline 缺 --decline-kind 应被 argparse 拒绝")
    except SystemExit as exc:
        assert exc.code == 2, exc.code


def test_deferred_until_condition_decline_writes_neither_fingerprint_cooldown_nor_session_scope_snooze(
    tmp: Path,
) -> None:
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    summary = _summary(["a.py", "b.py"], head="h1", unreviewed_lines=200)
    minted = _mint(dts, roots, session_id="s", fingerprint="fp1", summary=summary, now=t0)
    declined = dts.mark_declined(
        minted.token, decline_kind=_DEFERRED, reason="测试还在跑，跑完再审",
        root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
    )
    assert declined.payload["decline_kind"] == _DEFERRED
    assert not dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fp1", cooldown_root=roots["cooldown_root"]
    )
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is None
    record = dts.read_deferred_until_condition_decline(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    )
    assert record is not None and record["reason"] == "测试还在跑，跑完再审", record
    assert record["candidate_fingerprint_at_deferral"] == "fp1"
    assert record["worktree_head_commit_sha_at_deferral"] == "h1"
    assert record["user_prompt_submit_observed_a_new_turn_since_deferral"] is False


def _defer(dts, roots, *, session_id: str, fingerprint: str, summary: dict, now, reason: str = "子代理还在写，写完再审"):
    minted = _mint(dts, roots, session_id=session_id, fingerprint=fingerprint, summary=summary, now=now)
    assert minted.outcome == dts.MINT_OUTCOME_MINTED, minted
    dts.mark_declined(
        minted.token, decline_kind=_DEFERRED, reason=reason,
        root=roots["root"], now=now, cooldown_root=roots["cooldown_root"],
    )
    return minted.token


def test_deferred_until_condition_decline_is_reasked_only_after_new_turn_head_change_or_content_change(
    tmp: Path,
) -> None:
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    cb_root = roots["circuit_breaker_root_dir"]

    # ① 谢绝后原样停下：同指纹、同 HEAD、没有新回合 ⇒ 拒铸，且不占熔断额度。
    summary = _summary(["a.py"], head="h1", unreviewed_lines=120)
    _defer(dts, roots, session_id="same-turn", fingerprint="fp1", summary=summary, now=t0)
    for _ in range(3):
        blocked = _mint(dts, roots, session_id="same-turn", fingerprint="fp1", summary=dict(summary), now=t0)
        assert blocked.outcome == dts.MINT_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY, blocked
        assert "子代理还在写" in (blocked.message or "")
    assert dts.count_recent_mint_requests("same-turn", now=t0, root=cb_root) == 1

    # ② 新回合（UPS 观测到开发者 prompt 或后台完成通知）⇒ 放行，延后理由盖进候选摘要，记录清掉。
    assert dts.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
        session_id="same-turn", cooldown_root=roots["cooldown_root"]
    ) == 1
    after_turn_summary = dict(summary)
    reasked = _mint(dts, roots, session_id="same-turn", fingerprint="fp1", summary=after_turn_summary, now=t0)
    assert reasked.outcome == dts.MINT_OUTCOME_MINTED, reasked
    stamped = after_turn_summary[
        dts.CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY
    ]
    assert stamped["reason"] == "子代理还在写，写完再审", stamped
    assert stamped["release_reason"] == dts.DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_TURN_BOUNDARY
    assert reasked.record.payload["candidate_summary"][
        dts.CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY
    ]["reason"] == "子代理还在写，写完再审"
    assert dts.read_deferred_until_condition_decline(
        worktree="/wt", session_id="same-turn", cooldown_root=roots["cooldown_root"]
    ) is None

    # ③ 提交（HEAD 变化）⇒ 放行。
    _defer(dts, roots, session_id="head", fingerprint="fp1", summary=summary, now=t0, reason="提交后再审")
    moved = _summary(["a.py"], head="h2", unreviewed_lines=120)
    released_by_head = _mint(dts, roots, session_id="head", fingerprint="fp1", summary=moved, now=t0)
    assert released_by_head.outcome == dts.MINT_OUTCOME_MINTED, released_by_head
    assert moved[dts.CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY][
        "release_reason"
    ] == dts.DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_WORKTREE_HEAD_CHANGE

    # ④ 同一回合里接着改了候选文件（内容指纹变了）⇒ 放行。
    _defer(dts, roots, session_id="content", fingerprint="fp1", summary=summary, now=t0)
    edited = dict(summary)
    released_by_content = _mint(dts, roots, session_id="content", fingerprint="fp1-edited", summary=edited, now=t0)
    assert released_by_content.outcome == dts.MINT_OUTCOME_MINTED, released_by_content
    assert edited[dts.CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY][
        "release_reason"
    ] == dts.DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_CANDIDATE_CONTENT_CHANGE

    # 延后记录按会话分开：别的会话的新回合不放行本会话的延后。
    _defer(dts, roots, session_id="isolated", fingerprint="fp1", summary=summary, now=t0)
    dts.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
        session_id="someone-else", cooldown_root=roots["cooldown_root"]
    )
    still_blocked = _mint(dts, roots, session_id="isolated", fingerprint="fp1", summary=dict(summary), now=t0)
    assert still_blocked.outcome == dts.MINT_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY

    _assert_deferred_until_condition_decline_survives_released_mint_that_never_delivered_a_token(
        dts, roots, summary=summary
    )


def _assert_deferred_until_condition_decline_survives_released_mint_that_never_delivered_a_token(
    dts, roots, *, summary: dict
) -> None:
    """RVF-002：闸 2c 放行 ≠ 延后已兑现。放行后被闸三熔断拒铸、或 token 落盘失败，本次没有横幅；延后
    记录必须原样保留，重试真正铸出 token 时才盖章并消费——否则重试的横幅丢掉「你上次延后到」的理由。"""
    stamp_key = dts.CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY
    t0 = _fixed_utc(0)  # noqa: F821

    def read_record(session_id: str):
        return dts.read_deferred_until_condition_decline(
            worktree="/wt", session_id=session_id, cooldown_root=roots["cooldown_root"]
        )

    # ⑤ 熔断拒铸：另一枚 + 延后那枚已占满窗口（K=3 ⇒ 第 3 次请求拒铸），新回合放行后撞上熔断。另一枚
    # 必须铸在延后之前：延后之后换指纹铸造本身就是「候选内容变了」的合法放行，会正当消费记录。
    assert _mint(dts, roots, session_id="breaker", fingerprint="fp-other", summary=None, now=t0).outcome == (
        dts.MINT_OUTCOME_MINTED
    )
    _defer(dts, roots, session_id="breaker", fingerprint="fp1", summary=summary, now=t0, reason="等子代理收尾")
    dts.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
        session_id="breaker", cooldown_root=roots["cooldown_root"]
    )
    tripped = _mint(dts, roots, session_id="breaker", fingerprint="fp1", summary=dict(summary), now=t0)
    assert tripped.outcome == dts.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED, tripped
    assert read_record("breaker") is not None, "熔断拒铸后延后记录被提前消费，重试将丢失延后理由"
    retried_summary = dict(summary)
    retried = _mint(dts, roots, session_id="breaker", fingerprint="fp1", summary=retried_summary,
                    now=_fixed_utc(3605))  # noqa: F821 - 窗口滑过，熔断解除
    assert retried.outcome == dts.MINT_OUTCOME_MINTED, retried
    assert retried_summary[stamp_key]["reason"] == "等子代理收尾", retried_summary
    assert retried_summary[stamp_key]["release_reason"] == dts.DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_TURN_BOUNDARY
    assert read_record("breaker") is None, "真正铸出 token 后延后记录应被消费，否则会重复盖章"

    # ⑥ token 落盘失败（自动路径与用户显式触发同理）：记录保留，恢复后重试照常盖章 / 回报推翻并消费。
    original_create_token_record = dts._create_token_record

    def failing_create_token_record(**_kwargs):
        raise OSError("模拟 token 落盘失败")

    for session_id, explicit in (("write-failure-auto", False), ("write-failure-explicit", True)):
        _defer(dts, roots, session_id=session_id, fingerprint="fp1", summary=summary, now=t0, reason="测试跑完再审")
        dts.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
            session_id=session_id, cooldown_root=roots["cooldown_root"]
        )
        explicit_kwargs = (
            {
                "initiated_by_explicit_user_request": True,
                "explicit_user_request_provenance": (
                    dts.EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT
                ),
            }
            if explicit
            else {}
        )
        dts._create_token_record = failing_create_token_record
        try:
            _mint(dts, roots, session_id=session_id, fingerprint="fp1", summary=dict(summary), now=t0,
                  **explicit_kwargs)
            raise AssertionError("token 落盘失败应向上抛出")
        except OSError:
            pass
        finally:
            dts._create_token_record = original_create_token_record
        assert read_record(session_id) is not None, f"{session_id}：落盘失败后延后记录被提前消费"
        recovered_summary = dict(summary)
        # 自动路径那次失败的请求已在闸三登记过额度（不在本修复范围），恢复时挪到窗口之外免得撞熔断。
        recovered = _mint(dts, roots, session_id=session_id, fingerprint="fp1", summary=recovered_summary,
                          now=_fixed_utc(3605), **explicit_kwargs)  # noqa: F821
        assert recovered.outcome == dts.MINT_OUTCOME_MINTED, recovered
        if explicit:
            assert recovered.explicit_user_request_overrode_agent_deferred_until_condition_decline is True
            assert recovered.overridden_deferred_until_condition_decline_reason == "测试跑完再审"
        else:
            assert recovered_summary[stamp_key]["reason"] == "测试跑完再审", recovered_summary
        assert read_record(session_id) is None, f"{session_id}：铸成后延后记录应被消费"


def test_structural_decline_supersedes_earlier_deferred_until_condition_decline(tmp: Path) -> None:
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    summary = _summary(["a.py"], head="h1", unreviewed_lines=50)
    first = _mint(dts, roots, session_id="s", fingerprint="fp1", summary=summary, now=t0)
    second = _mint(dts, roots, session_id="s", fingerprint="fp2", summary=summary, now=t0)
    dts.mark_declined(
        first.token, decline_kind=_DEFERRED, reason="提交后再审",
        root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
    )
    assert dts.read_deferred_until_condition_decline(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is not None
    dts.mark_declined(
        second.token, decline_kind=_STRUCTURAL, reason="只有一行日志，琐碎",
        root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
    )
    assert dts.read_deferred_until_condition_decline(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is None
    assert dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fp2", cooldown_root=roots["cooldown_root"]
    )
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is not None


def test_explicit_user_request_overrides_deferred_until_condition_decline_and_records_provenance(
    tmp: Path,
) -> None:
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    summary = _summary(["a.py"], head="h1", unreviewed_lines=50)
    _defer(dts, roots, session_id="s", fingerprint="fp1", summary=summary, now=t0, reason="测试还在跑")
    try:
        _mint(dts, roots, session_id="s", fingerprint="fp1", summary=dict(summary), now=t0,
              initiated_by_explicit_user_request=True)
        raise AssertionError("显式请求铸造缺来源应被拒绝")
    except dts.DecisionTokenError:
        pass
    manual = _mint(
        dts, roots, session_id="s", fingerprint="fp1", summary=dict(summary), now=t0,
        initiated_by_explicit_user_request=True,
        explicit_user_request_provenance=dts.EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT,
    )
    assert manual.outcome == dts.MINT_OUTCOME_MINTED, manual
    assert manual.explicit_user_request_overrode_agent_deferred_until_condition_decline is True
    assert manual.overridden_deferred_until_condition_decline_reason == "测试还在跑"
    assert manual.record.payload["explicit_user_request_provenance"] == (
        "developer_manual_trigger_via_user_prompt_submit"
    )
    assert dts.read_deferred_until_condition_decline(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is None
    automatic = _mint(dts, roots, session_id="auto", fingerprint="fp9", summary=None, now=t0)
    assert automatic.record.payload["initiated_by_explicit_user_request"] is False
    assert automatic.record.payload["explicit_user_request_provenance"] is None


# ---------------------------------------------------------------------------
# 熔断不自我续命
# ---------------------------------------------------------------------------

def test_circuit_breaker_refused_mint_requests_do_not_keep_the_breaker_tripped(tmp: Path) -> None:
    """会话 893079be 的形态：Stop 间隔短于 20 分钟时，旧实现里每次被拒的请求都把窗口计数顶在阈值
    以上，熔断永不解除。修复后被拒请求不占额度：最早那枚滑出窗口即可再铸。"""
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    cb_root = roots["circuit_breaker_root_dir"]

    def ask(seconds: int, index: int):
        return _mint(dts, roots, session_id="busy", fingerprint=f"fp{index}", summary=None,
                     now=_fixed_utc(seconds))  # noqa: F821

    assert ask(0, 0).outcome == dts.MINT_OUTCOME_MINTED
    assert ask(60, 1).outcome == dts.MINT_OUTCOME_MINTED
    for index, seconds in enumerate((120, 900, 1800, 2700, 3500), start=2):
        assert ask(seconds, index).outcome == dts.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED
    # t=3605：t=0 那枚已滑出窗口，窗口里只剩 t=60 那枚真铸出的 ⇒ 可以再铸。
    assert dts.count_recent_mint_requests("busy", now=_fixed_utc(3605), root=cb_root) == 1  # noqa: F821
    assert ask(3605, 99).outcome == dts.MINT_OUTCOME_MINTED


def test_extra_token_payload_fields_cannot_override_system_written_explicit_user_request_fields(
    tmp: Path,
) -> None:
    """``extra_token_payload_fields`` 不得盖掉系统写入的显式请求两键：否则调用方能铸出
    ``initiated_by_explicit_user_request=False`` 的手动 token、或给自动路径 token 伪造来源，污染审计。
    拒铸必须发生在任何闸门副作用之前：不落 token、不清冷却。"""
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    roots = _decision_token_store_roots(tmp)  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    summary = _summary(["a.py"], head="h1", unreviewed_lines=50)
    declined = _mint(dts, roots, session_id="s", fingerprint="fp1", summary=dict(summary), now=t0)
    dts.mark_declined(
        declined.token, decline_kind=_STRUCTURAL, reason="先不审",
        root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
    )
    token_files_before = sorted(Path(roots["root"]).rglob("*"))
    developer_manual = (
        dts.EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT
    )
    forged_attempts = (
        # 显式请求铸造，却想把「显式请求」标记盖成 False。
        dict(initiated_by_explicit_user_request=True, explicit_user_request_provenance=developer_manual,
             extra_token_payload_fields={"initiated_by_explicit_user_request": False}),
        # 显式请求铸造，却想把来源改写成另一条入口。
        dict(initiated_by_explicit_user_request=True, explicit_user_request_provenance=developer_manual,
             extra_token_payload_fields={
                 "explicit_user_request_provenance":
                     "agent_reopened_own_declined_decision_on_developer_verbal_request",
             }),
        # 自动路径铸造，却想冒充显式请求。
        dict(extra_token_payload_fields={
            "initiated_by_explicit_user_request": True,
            "explicit_user_request_provenance": developer_manual,
        }),
    )
    for forged in forged_attempts:
        try:
            _mint(dts, roots, session_id="s", fingerprint="fp1", summary=dict(summary), now=t0, **forged)
            raise AssertionError(f"extra_token_payload_fields 覆盖系统写入字段应被拒绝：{forged}")
        except dts.DecisionTokenError:
            pass
    assert sorted(Path(roots["root"]).rglob("*")) == token_files_before
    assert dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fp1", cooldown_root=roots["cooldown_root"]
    )
    # 不冲突的额外字段照常并入，系统两键保持系统写入值。
    legitimate = _mint(
        dts, roots, session_id="s", fingerprint="fp1", summary=dict(summary), now=t0,
        initiated_by_explicit_user_request=True, explicit_user_request_provenance=developer_manual,
        extra_token_payload_fields={"boundary_request_source_token": declined.token},
    )
    assert legitimate.outcome == dts.MINT_OUTCOME_MINTED, legitimate
    assert legitimate.record.payload["boundary_request_source_token"] == declined.token
    assert legitimate.record.payload["initiated_by_explicit_user_request"] is True
    assert legitimate.record.payload["explicit_user_request_provenance"] == developer_manual


def cli_session_id_environment_variable_names() -> tuple[str, ...]:
    if str(ROOT) not in sys.path:  # noqa: F821 - injected
        sys.path.insert(0, str(ROOT))  # noqa: F821
    from core.host_adapter.current_agent_session_identity_from_environment import (  # noqa: PLC0415
        CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
    )

    return tuple(CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES)


# ---------------------------------------------------------------------------
# D3：Kanban task 首轮以分支点作 committed-round 下界
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(repo),
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_kanban_task_committed_round_baseline_bootstraps_from_branch_point_when_never_reviewed(tmp: Path) -> None:
    """会话 893079be 的形态：task 分支上已提交、从未审过（无高水位），一条 prompt 把 round-baseline
    顶到 HEAD ⇒ 修复前窗口为空、这些提交永久漏审；修复后下界退到 task 分支点。"""
    if str(ROOT) not in sys.path:  # noqa: F821 - injected
        sys.path.insert(0, str(ROOT))  # noqa: F821
    from core.session_scope_allocation import (  # noqa: PLC0415
        committed_round_detection,
        review_highwater_marker,
        round_baseline_marker,
    )

    repo = tmp / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    branch_point = _commit(repo, "base.txt")
    _git(repo, "checkout", "-q", "-b", "task-branch")
    _commit(repo, "task-1.txt")
    task_head = _commit(repo, "task-2.txt")

    with _environment(
        CODEX_RVF_LOG_ROOT=str(tmp / "log-root"),
        CODEX_RVF_STATE_DIR=None,
        CODEX_RVF_COMMITTED_ROUND_BASE_REF=None,
    ):
        for identity in ({"task_id": "01457", "session_id": "s"}, {"task_id": None, "session_id": "s"}):
            round_baseline_marker.write_round_baseline_marker(
                baseline_head=task_head, repo=str(repo), prompt_excerpt="下一条 prompt", **identity
            )
        resolve = committed_round_detection._resolve_committed_round_baseline
        # Kanban task、无高水位 ⇒ 分支点（更早者）。
        assert resolve(repo, "01457", "s") == branch_point
        # 非 task 会话 ⇒ 行为不变：round-baseline。
        assert resolve(repo, None, "s") == task_head
        # 有高水位 ⇒ 高水位优先（首轮之后不再用分支点）。
        review_highwater_marker.write_review_highwater(
            task_id="01457", session_id="s", reviewed_head=task_head, repo=str(repo), source="test"
        )
        assert resolve(repo, "01457", "s") == task_head
        # round-baseline 缺失（6h TTL 过期等）时 Kanban task 仍有分支点可用。
        assert resolve(repo, "task-without-marker", "s2") == branch_point
        # 仓里没有 main（且无覆盖变量）⇒ 退回 round-baseline，不猜。
        _git(repo, "branch", "-m", "main", "trunk")
        assert resolve(repo, "task-without-marker", "s2") is None
        round_baseline_marker.write_round_baseline_marker(
            task_id="task-no-main", session_id="s3", baseline_head=task_head, repo=str(repo)
        )
        assert resolve(repo, "task-no-main", "s3") == task_head


# ---------------------------------------------------------------------------
# 回溯验收：3 例会被 T3 压住的延后型谢绝
# ---------------------------------------------------------------------------

def test_retrospective_deferred_declines_are_reasked_while_structural_declines_stay_snoozed(tmp: Path) -> None:
    """按真实 token 记录还原的三种形态（候选路径数 / 未审行数 / 期间开发者 prompt 数 / 提交数 / 指纹是否变）。

    * 2c00c 08-18：9 路径不变、719→719 行、期间 1 条开发者 prompt、无提交、指纹变了；
    * 01200 08-21 16:05：5→8 路径（3 条新）、758→776 行（+2%）、期间 3 条开发者 prompt、无提交；
    * ac08e 08-22 03:12：6 路径不变、370→371 行、谢绝与重问之间**没有任何新 UPS**（谢绝发生在 task-
      notification 回合中途，agent 同一回合里改完文件就停下）、无提交，只有指纹变了。

    三例在 T3（谢绝一律 snooze）下都被压住（无新路径或增长 < 50%，F3 的「3 倍且 ≥150 行」也不触发），
    这三轮 run 共 fixed 8 条。新语义下按延后型谢绝全部重新征询；同一形态按结构性谢绝仍被 snooze。
    """
    dts = load_rvf_decision_token_store_module()  # noqa: F821
    t0 = _fixed_utc(0)  # noqa: F821
    later = _fixed_utc(1800)  # noqa: F821
    cases = [
        {
            "name": "2c00c-0818",
            "before": (["p%d.py" % i for i in range(9)], 719),
            "after": (["p%d.py" % i for i in range(9)], 719),
            "developer_prompts_between": 1,
        },
        {
            "name": "01200-0821-1605",
            "before": (["p%d.py" % i for i in range(5)], 758),
            "after": (["p%d.py" % i for i in range(8)], 776),
            "developer_prompts_between": 3,
        },
        {
            "name": "ac08e-0822-0312",
            "before": (["p%d.py" % i for i in range(6)], 370),
            "after": (["p%d.py" % i for i in range(6)], 371),
            "developer_prompts_between": 0,
        },
    ]
    for case in cases:
        for decline_kind, expected in (
            (_DEFERRED, dts.MINT_OUTCOME_MINTED),
            (_STRUCTURAL, dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE),
        ):
            roots = _decision_token_store_roots(tmp / case["name"] / decision_kind_dirname(decline_kind))  # noqa: F821
            before_paths, before_lines = case["before"]
            after_paths, after_lines = case["after"]
            minted = _mint(
                dts, roots, session_id="sess", fingerprint="fp-at-decline",
                summary=_summary(before_paths, head="h1", unreviewed_lines=before_lines), now=t0,
            )
            dts.mark_declined(
                minted.token, decline_kind=decline_kind, reason="回放",
                root=roots["root"], now=t0, cooldown_root=roots["cooldown_root"],
            )
            for _ in range(case["developer_prompts_between"]):
                dts.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
                    session_id="sess", cooldown_root=roots["cooldown_root"]
                )
                dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
                    session_id="sess", cooldown_root=roots["cooldown_root"]
                )
            next_ask = _mint(
                dts, roots, session_id="sess", fingerprint="fp-at-next-stop",
                summary=_summary(after_paths, head="h1", unreviewed_lines=after_lines), now=later,
            )
            assert next_ask.outcome == expected, (case["name"], decline_kind, next_ask)


def decision_kind_dirname(decline_kind: str) -> str:
    return decline_kind.replace("_", "-")
