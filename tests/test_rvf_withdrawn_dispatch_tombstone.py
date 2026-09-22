"""撤回竞态：dispatch token 的持久终态（``consumed`` vs ``withdrawn``）与 UPS 侧的硬中和。

要防的是一件具体的事：RVF 判定某条 follow-up 投递「丢了」并撤回/重投之后，那条投递**随后
才落地**（实测迟到过 49 分钟）。落地时它是**完全可执行**的——注入正文自包含，第一行就是
``$review-validate-fix``——于是 agent 会跑出一轮无锁、无 lease、无 scope 契约的 review，还可能
与已经重新发起的那一轮并发撞车。

这里钉三件事：

1. **CAS 是确定性的，不是尽力而为。** 谁先写下终态谁赢，且赢的一方决定另一方必须做什么：
   ``consumed`` 赢 ⇒ 超时侧**绝不可**撤回（那会拆掉一轮正在进行的 review）；``withdrawn`` 赢
   ⇒ 迟到落地必须被中和。
2. **中和是 hook 强制的，不是靠注入正文自觉。** 本次事故本身就是「agent 被拽去跑一轮它从未
   接受的 RVF」，同一类模型合规假设不能再用第二次。
3. **这道机制永远不会把主流程堵死。** token 形状不合法 / 文件系统报错 ⇒ 按无 tombstone 处理。
   它是止血机制，不是安全边界。

⚠️ 全部测试通过 ``RVF_WITHDRAWN_DISPATCH_TOMBSTONE_ROOT`` 指向 ``tmp_path``，**不碰真实的
``~/.rvf``**。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS_DIR = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tombstone = _load("rvf_withdrawn_dispatch_tombstone_under_test", "rvf_withdrawn_dispatch_tombstone.py")
ups = _load("rvf_user_prompt_submit_under_test", "rvf_user_prompt_submit.py")

TOKEN = "0123456789abcdef"
OTHER_TOKEN = "fedcba9876543210"


@pytest.fixture(autouse=True)
def _isolated_state_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 tombstone 根钉进 tmp_path，并摘掉可能从外层会话继承的 kanban task 环境。

    后者很重要：``inspect_user_prompt_submit`` 在 kanban task 内会写 PostToolUse-park 资格标记，
    真实运行时那会落到 live infra 上——本仓有过测试往真实根写残留的前科。
    """
    monkeypatch.setenv(tombstone.ROOT_ENV, str(tmp_path / "withdrawn-tokens"))
    for key in ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------


def test_first_writer_wins_and_the_loser_learns_who_won() -> None:
    first = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_WITHDRAWN, reason="pending_never_converged"
    )
    assert (first.won, first.applicable) == (True, True)

    second = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_CONSUMED, reason="delivery_landed"
    )
    assert second.won is False
    assert second.applicable is True
    assert second.existing_state == tombstone.STATE_WITHDRAWN


def test_a_consumed_dispatch_can_never_be_withdrawn_afterwards() -> None:
    """「consume 胜出后，timeout 不得再把它当未投递撤回」——撤回会拆掉一轮正在进行的 review。"""
    consumed = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_CONSUMED, reason="delivery_landed"
    )
    assert consumed.won is True

    withdraw = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_WITHDRAWN, reason="pending_ttl_elapsed"
    )
    assert withdraw.won is False
    assert withdraw.existing_state == tombstone.STATE_CONSUMED
    # 而且它**不是**「已撤回」，迟到中和不得对它生效。
    assert tombstone.dispatch_token_was_withdrawn(TOKEN) is None


def test_reclaiming_the_same_state_is_idempotent() -> None:
    """同一个 UPS 回合里两次调用不该互相判负。"""
    assert (
        tombstone.claim_dispatch_token_terminal_state(
            TOKEN, state=tombstone.STATE_CONSUMED
        ).won
        is True
    )
    again = tombstone.claim_dispatch_token_terminal_state(TOKEN, state=tombstone.STATE_CONSUMED)
    assert again.won is True
    assert again.existing_state is None


def test_a_concurrent_claimant_can_never_observe_a_half_written_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态文件绝不能有「已经出现、但内容还没写完」的中间态。

    在两版实现共有的接缝上做确定性注入：``O_EXCL`` 建文件刚返回、内容尚未写下去的那一瞬间，让
    **另一个** claimant 完整地跑一遍。有半写窗口时它读到的是空文件 ⇒ ``_read_payload`` 返回 None
    ⇒ 判成「不可信记录」⇒ unlink 后重建 ⇒ 两个**状态相反**的 claimant 双双 ``won=True``，而磁盘上
    只留得下一个——先到先得这把唯一的仲裁锁当场失效。
    """
    real_open = os.open
    tripped: list[bool] = []
    concurrent: dict[str, object] = {}

    def _let_a_concurrent_claimant_race_inside_the_exclusive_create(
        path, flags, mode=0o777, **kwargs
    ):
        fd = real_open(path, flags, mode, **kwargs)
        if not tripped and flags & os.O_EXCL:
            tripped.append(True)  # 先置位再递归调用，否则内层会无限重入这个包装。
            concurrent["claim"] = tombstone.claim_dispatch_token_terminal_state(
                TOKEN, state=tombstone.STATE_WITHDRAWN, reason="concurrent_withdrawal"
            )
        return fd

    # 注意：不要 monkeypatch.undo()——本文件的隔离根也挂在同一个 monkeypatch 实例上，撤销它会
    # 让后续调用写进真实 ``~/.rvf``。让 pytest 在 teardown 统一还原即可。
    monkeypatch.setattr(os, "open", _let_a_concurrent_claimant_race_inside_the_exclusive_create)
    mine = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_CONSUMED, reason="delivery_landed"
    )

    assert tripped, "注入点没被命中 —— 这条测试就没在测它要测的东西"
    other = concurrent["claim"]
    winners = [claim for claim in (mine, other) if claim.won]
    assert len(winners) == 1, "两个状态相反的 claimant 不得同时算赢"
    # 且赢家的态必须**就是**磁盘上的态：不能出现「A 以为自己赢了，盘上写的却是 B」。
    on_disk = tombstone.read_active_dispatch_token_terminal_state(TOKEN)
    assert on_disk is not None
    assert on_disk["state"] == (
        tombstone.STATE_CONSUMED if winners[0] is mine else tombstone.STATE_WITHDRAWN
    )


#: 子进程里跑一次 claim 并把结果打成 JSON。用**真正的另一个进程**，因为两个并发撤回者本来就是
#: 两个 Stop hook 进程；同进程里怎么模拟都证明不了跨进程的身份判定。
_CLAIM_IN_ANOTHER_PROCESS_SOURCE = """
import importlib.util, json, sys

module_path, token, state, reason, claim_id = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("tombstone_in_child_process", module_path)
module = importlib.util.module_from_spec(spec)
# 必须先登记进 sys.modules 再 exec：Python 3.9 的 dataclasses 会回查 ``cls.__module__``。
sys.modules["tombstone_in_child_process"] = module
spec.loader.exec_module(module)
claim = module.claim_dispatch_token_terminal_state(
    token, state=state, reason=reason, claim_id=claim_id or None
)
print(json.dumps({
    "won": claim.won,
    "applicable": claim.applicable,
    "existing_state": claim.existing_state,
    "reentrant": getattr(claim, "reentrant", False),
}))
"""


def _claim_from_another_process(state: str, *, reason: str, claim_id: str = "") -> dict:
    env = dict(os.environ)
    # 铁律：子进程必须继承隔离根，绝不能写真实 ``~/.rvf``。
    assert env.get(tombstone.ROOT_ENV)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _CLAIM_IN_ANOTHER_PROCESS_SOURCE,
            str(SCRIPTS_DIR / "rvf_withdrawn_dispatch_tombstone.py"),
            TOKEN,
            state,
            reason,
            claim_id,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_second_withdrawer_in_another_process_does_not_also_win() -> None:
    """同态重复 claim **不得**一律算赢：那会让两个撤回者各跑一遍清理。

    撤回不是只写一条记录：它还要 ``message-cancel``、放 tracker lease、按 task 清锁与 pending。
    两个撤回者都拿到 ``won=True``，慢的那个就可能在第一个已经放行**新一轮**之后，去删新一轮的
    锁和 pending。
    """
    first = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_WITHDRAWN, reason="first_withdrawer"
    )
    assert (first.won, first.reentrant) == (True, False)

    second = _claim_from_another_process(
        tombstone.STATE_WITHDRAWN, reason="second_withdrawer"
    )
    assert second["won"] is False
    assert second["applicable"] is True
    assert second["existing_state"] == tombstone.STATE_WITHDRAWN
    # 先到者的记录既没被覆盖，也没被换掉理由。
    payload = tombstone.read_active_dispatch_token_terminal_state(TOKEN)
    assert payload is not None and payload["reason"] == "first_withdrawer"


def test_the_same_logical_operation_can_still_prove_its_reentry_across_processes() -> None:
    """幂等重入没有被一起砍掉——只是要求**证明**自己是同一件事（显式 ``claim_id``）。"""
    first = tombstone.claim_dispatch_token_terminal_state(
        TOKEN,
        state=tombstone.STATE_WITHDRAWN,
        reason="withdraw",
        claim_id="withdrawal-of-one-particular-dispatch",
    )
    assert (first.won, first.reentrant) == (True, False)

    again = _claim_from_another_process(
        tombstone.STATE_WITHDRAWN,
        reason="withdraw",
        claim_id="withdrawal-of-one-particular-dispatch",
    )
    assert again["won"] is True
    assert again["reentrant"] is True


def test_expired_tombstone_is_treated_as_absent_and_can_be_overwritten(
    tmp_path: Path,
) -> None:
    path = tombstone.tombstone_path(TOKEN)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    path.write_text(
        json.dumps(
            {
                "tombstone_version": 1,
                "state": tombstone.STATE_WITHDRAWN,
                "expires_at": stale.isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    assert tombstone.read_active_dispatch_token_terminal_state(TOKEN) is None
    assert tombstone.dispatch_token_was_withdrawn(TOKEN) is None
    fresh = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_CONSUMED
    )
    assert fresh.won is True


def test_default_ttl_covers_the_observed_late_delivery_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计划一度写「TTL 2 分钟」——实测已迟到 49 分钟，2 分钟的 tombstone 等于没有。"""
    monkeypatch.delenv(tombstone.TTL_ENV, raising=False)
    assert tombstone.ttl_seconds() >= 6 * 60 * 60
    monkeypatch.setenv(tombstone.TTL_ENV, "1800")
    assert tombstone.ttl_seconds() == 1800.0
    monkeypatch.setenv(tombstone.TTL_ENV, "garbage")
    assert tombstone.ttl_seconds() == float(tombstone.DEFAULT_TTL_SECONDS)


def test_mechanism_degrades_open_and_never_blocks_the_main_flow() -> None:
    """token 形状不合法时必须 ``applicable=False`` —— 止血机制不得反过来堵死派发。"""
    for bad in (None, "", "   ", "not-hex!!", 12345):
        claim = tombstone.claim_dispatch_token_terminal_state(
            bad, state=tombstone.STATE_WITHDRAWN
        )
        assert claim.applicable is False
        assert claim.won is False
        assert tombstone.dispatch_token_was_withdrawn(bad) is None


def test_unknown_state_is_a_programming_error_not_a_silent_write() -> None:
    with pytest.raises(ValueError):
        tombstone.claim_dispatch_token_terminal_state(TOKEN, state="probably_delivered")


def test_tombstones_are_written_only_under_the_overridden_root(tmp_path: Path) -> None:
    tombstone.claim_dispatch_token_terminal_state(TOKEN, state=tombstone.STATE_WITHDRAWN)
    written = sorted(p.name for p in (tmp_path / "withdrawn-tokens").glob("token-*.json"))
    assert written == [f"token-{TOKEN}.json"]
    assert not (Path.home() / ".rvf" / tombstone.SUBDIR_NAME / f"token-{TOKEN}.json").exists()


def test_rvf_home_alone_is_enough_to_isolate_this_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只设 ``RVF_HOME`` 就必须能把 tombstone 关进笼子——**两侧调用点都不显式传 root**。

    这条是回归守卫，不是覆盖率：初版只认专用 env，于是 Stop hook 与 UPS 的既有测试
    harness（它们靠 ``RVF_HOME`` 隔离同族的决策 token store / 注入 ledger）全都漏了，
    实测把 4 个 tombstone 写进了真实 ``~/.rvf``。
    """
    monkeypatch.delenv(tombstone.ROOT_ENV, raising=False)
    monkeypatch.setenv(tombstone.RVF_HOME_ENV, str(tmp_path / "caged-rvf-home"))
    claim = tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_WITHDRAWN
    )
    assert claim.won is True
    caged = tmp_path / "caged-rvf-home" / tombstone.SUBDIR_NAME / f"token-{TOKEN}.json"
    assert caged.is_file()
    assert not (Path.home() / ".rvf" / tombstone.SUBDIR_NAME / f"token-{TOKEN}.json").exists()


# ---------------------------------------------------------------------------
# UPS 侧硬中和
# ---------------------------------------------------------------------------


def _dispatch_prompt(token: str) -> str:
    return (
        "$review-validate-fix\n\nRVF_KANBAN_FOLLOWUP_TRIGGER\n"
        f"RVF dispatch prep file:\nRVF_DISPATCH=token={token}\n"
    )


def test_withdrawn_dispatch_landing_late_is_blocked_not_merely_discouraged(
    tmp_path: Path,
) -> None:
    """撤回过的 token 迟到落地 → 三管齐下：block + 权威 additionalContext + 用户可见提示。

    只写一句「建议忽略」是不够的：注入正文是自包含指令，agent 会照跑。中和必须由 hook 强制。
    """
    tombstone.claim_dispatch_token_terminal_state(
        TOKEN, state=tombstone.STATE_WITHDRAWN, reason="prior_dispatch_never_converged"
    )
    result = ups.inspect_user_prompt_submit(
        {"prompt": _dispatch_prompt(TOKEN), "session_id": "s1", "cwd": str(tmp_path)},
        prep_root=str(tmp_path / "prep"),
    )
    assert result["status"] == "dispatch_withdrawn"
    assert result["workflow_started"] is False
    assert result["dispatch_withdrawn_reason"] == "prior_dispatch_never_converged"
    assert result["decision"] == "block"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "已被 RVF 撤回" in context
    assert "不要执行它" in context
    assert TOKEN in context
    assert "systemMessage" in result

    rendered = ups._render_hook_payload(result)
    assert rendered["decision"] == "block"
    assert isinstance(rendered["reason"], str) and TOKEN in rendered["reason"]
    assert rendered["hookSpecificOutput"]["additionalContext"] == context
    assert rendered["systemMessage"] == result["systemMessage"]


def test_a_live_dispatch_claims_consumed_before_anything_else_touches_it(
    tmp_path: Path,
) -> None:
    """UPS 必须在**消费之前**抢下 ``consumed``，否则一次并发超时仍能把它撤回。"""
    result = ups.inspect_user_prompt_submit(
        {"prompt": _dispatch_prompt(TOKEN), "session_id": "s1", "cwd": str(tmp_path)},
        prep_root=str(tmp_path / "prep"),
    )
    # prep 不存在 → 走 dispatch_no_prep，但**终态已经抢下**，这正是要钉的顺序。
    assert result.get("dispatch_token_terminal_state_claimed") is True
    state = tombstone.read_active_dispatch_token_terminal_state(TOKEN)
    assert state is not None and state["state"] == tombstone.STATE_CONSUMED
    # 此后任何超时侧的撤回都必须失败。
    assert (
        tombstone.claim_dispatch_token_terminal_state(
            TOKEN, state=tombstone.STATE_WITHDRAWN
        ).existing_state
        == tombstone.STATE_CONSUMED
    )


def test_withdrawal_winning_after_the_check_but_before_the_claim_still_hard_neutralizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU：撤回在「查 tombstone」之后、「抢 consumed」之前胜出，仍必须走同一条硬中和。

    这个窗口曾经是漏的：查的时候还没 tombstone（放行），抢 ``consumed`` 时输给了
    ``withdrawn``，而那个失败当时只被记进诊断字段 —— 主流程照样消费 prep、arm
    kanban-followup 锁、启动 workflow，跑出的正是这套机制要根治的形态。
    """
    ups_tombstone = ups.rvf_withdrawn_dispatch_tombstone
    real_lookup = ups_tombstone.dispatch_token_was_withdrawn

    def _withdrawal_lands_right_after_the_check(token, **kwargs):
        outcome = real_lookup(token, **kwargs)
        if outcome is None:
            # 查完说「没被撤回」，撤回侧紧接着赢下 CAS —— 就是那个窗口。
            ups_tombstone.claim_dispatch_token_terminal_state(
                token,
                state=ups_tombstone.STATE_WITHDRAWN,
                reason="withdrawn_inside_check_to_claim_window",
            )
        return outcome

    monkeypatch.setattr(
        ups_tombstone,
        "dispatch_token_was_withdrawn",
        _withdrawal_lands_right_after_the_check,
    )
    result = ups.inspect_user_prompt_submit(
        {"prompt": _dispatch_prompt(TOKEN), "session_id": "s1", "cwd": str(tmp_path)},
        prep_root=str(tmp_path / "prep"),
    )
    assert result["status"] == "dispatch_withdrawn"
    assert result["workflow_started"] is False
    assert result["decision"] == "block"
    assert result["dispatch_withdrawn_reason"] == "withdrawn_inside_check_to_claim_window"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "不要执行它" in context and TOKEN in context
    assert "systemMessage" in result
    # 输掉 CAS 这件事本身仍要留痕（诊断字段语义不变，只是不再是唯一后果）。
    assert result["dispatch_token_terminal_state_claimed"] is False
    assert result["dispatch_token_terminal_state_existing"] == tombstone.STATE_WITHDRAWN
    # 且这条投递绝不会被记成 consumed —— 撤回方赢了就该一直是 withdrawn。
    state = tombstone.read_active_dispatch_token_terminal_state(TOKEN)
    assert state is not None and state["state"] == tombstone.STATE_WITHDRAWN


def test_an_unusable_claim_mechanism_still_lets_the_prompt_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``applicable=False``（机制此刻用不了）**不是**「已撤回」，必须放行而不是当撤回中和。"""
    ups_tombstone = ups.rvf_withdrawn_dispatch_tombstone

    def _mechanism_is_unusable(token, **kwargs):
        return ups_tombstone.DispatchTokenTerminalStateClaim(
            won=False,
            applicable=False,
            existing_state=None,
            existing_payload=None,
            path=None,
        )

    monkeypatch.setattr(
        ups_tombstone, "claim_dispatch_token_terminal_state", _mechanism_is_unusable
    )
    result = ups.inspect_user_prompt_submit(
        {"prompt": _dispatch_prompt(TOKEN), "session_id": "s1", "cwd": str(tmp_path)},
        prep_root=str(tmp_path / "prep"),
    )
    assert result["status"] != "dispatch_withdrawn"
    assert "decision" not in result


def test_untouched_tokens_are_not_neutralized(tmp_path: Path) -> None:
    """只撤回过 A，B 落地时不得被误中和。"""
    tombstone.claim_dispatch_token_terminal_state(
        OTHER_TOKEN, state=tombstone.STATE_WITHDRAWN, reason="unrelated"
    )
    result = ups.inspect_user_prompt_submit(
        {"prompt": _dispatch_prompt(TOKEN), "session_id": "s1", "cwd": str(tmp_path)},
        prep_root=str(tmp_path / "prep"),
    )
    assert result.get("status") != "dispatch_withdrawn"
    assert "decision" not in result


def test_render_hook_payload_stays_silent_for_ordinary_prompts() -> None:
    assert ups._render_hook_payload({"continue": True}) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
