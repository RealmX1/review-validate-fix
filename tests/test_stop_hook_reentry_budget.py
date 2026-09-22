"""Stop hook 跨 turn 再入预算（S-B6）的行为闸。

这道闸存在的理由是**宿主的保险丝不够用**：Kimi 的 ``stop_hook_active`` 恒 false，
引擎自己的续跑标记又每 turn 复位 ⇒ 「driven 一回合 → agent 停下 → 再 driven 一回合」
在宿主侧完全没有上界。本仓已实测过一次 286 次无上界续跑。

测试分三层，缺任何一层都会留下假绿：

- **模块层**：三条复位路径与上限本身。
- **接线层**：``emit_stop_hook_payload`` 真的会在预算耗尽时把 ``agent_must_act_now``
  降级成 ``developer_only``——只断言模块正确、不断言接线，等于测了一个没人调用的函数。
- **复位判据层**：UPS 的复位条件必须排除 RVF 自己产生的机器正文，否则 RVF 的注入会把
  自己的保险丝按掉，预算恒不生效。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BUDGET_MODULE = REPOSITORY_ROOT / "core/stop_hook_reentry_budget/stop_hook_reentry_budget.py"
STOP_HOOK_CORE = (
    REPOSITORY_ROOT
    / "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py"
)
USER_PROMPT_SUBMIT_HOOK = (
    REPOSITORY_ROOT
    / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
)


def _run_python(source: str, *, environment_overrides: dict[str, str] | None = None) -> dict:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("CODEX_RVF_", "RVF_", "KANBAN_", "CLINE_KANBAN_")):
            environment.pop(name, None)
    environment.update(environment_overrides or {})
    completed = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env=environment,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- 模块层


def test_budget_module_self_check_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(BUDGET_MODULE)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert "STOP_HOOK_REENTRY_BUDGET_SELF_CHECK=OK" in completed.stdout


def test_consecutive_directives_exhaust_the_budget_and_each_reset_path_clears_it(
    tmp_path: Path,
) -> None:
    result = _run_python(
        f"""
import json, sys
sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
from core.stop_hook_reentry_budget import (
    consume_stop_hook_reentry_budget,
    reset_stop_hook_reentry_budget_on_user_prompt_submit,
)
state = {str(tmp_path)!r}
env = {{"RVF_STOP_HOOK_REENTRY_BUDGET_MAX_CONSECUTIVE_AGENT_DIRECTIVES": "2"}}

def consume(source, now):
    return consume_stop_hook_reentry_budget(
        session_identifier="s1",
        stable_agent_directive_scope_digest_source=source,
        state_directory=state,
        now_epoch_seconds=now,
        environment=env,
    )

permitted = [consume("same", 100.0 + i).agent_directive_permitted for i in range(3)]
after_digest_change = consume("other", 104.0)
after_ttl = consume("other", 104.0 + 1800.0 + 1.0)
reset_happened = reset_stop_hook_reentry_budget_on_user_prompt_submit(
    session_identifier="s1", state_directory=state
)
after_user_prompt = consume("other", 5000.0)
print(json.dumps({{
    "permitted": permitted,
    "after_digest_change": [after_digest_change.agent_directive_permitted,
                            after_digest_change.consecutive_agent_directive_count],
    "after_ttl": [after_ttl.agent_directive_permitted, after_ttl.consecutive_agent_directive_count],
    "reset_happened": reset_happened,
    "after_user_prompt": [after_user_prompt.agent_directive_permitted,
                          after_user_prompt.consecutive_agent_directive_count],
}}))
"""
    )
    assert result["permitted"] == [True, True, False], result
    assert result["after_digest_change"] == [True, 1], result
    assert result["after_ttl"] == [True, 1], result
    assert result["reset_happened"] is True, result
    assert result["after_user_prompt"] == [True, 1], result


# ---------------------------------------------------------------- 接线层


def test_exhausted_budget_downgrades_the_agent_driving_stop_exit_to_developer_only(
    tmp_path: Path,
) -> None:
    """预算耗尽后，``emit_stop_hook_payload`` 必须把驱动 agent 的出口降成 developer_only。

    这是本切片的**行为**主张。只测模块不测这里，等于把一个从没被调用的函数测绿了。
    """
    result = _run_python(
        f"""
import io, json, sys
from contextlib import redirect_stdout
sys.path.insert(0, {str(STOP_HOOK_CORE.parent)!r})
import stop_review_validate_fix as stop

from core.stop_hook_output_audience import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY as DENY_CHANNEL,
)

class FakeHostAdapter:
    def stop_hook_agent_directive_channel(self):
        return DENY_CHANNEL
    def host_display_name(self):
        return "FakeHost"

directive = stop.AgentDirectiveStopHookOutput(
    agent_directive_body="继续收尾这一轮 RVF。",
    developer_visible_notice="dev notice",
)
event = {{"session_id": "budget-wiring-session"}}
emitted = []
for _ in range(4):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        stop.emit_stop_hook_payload(
            audience=stop.STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
            agent_directive=directive,
            developer_only_payload=None,
            host_adapter=FakeHostAdapter(),
            ledger=None,
            reason_code="unit_test_same_reason",
            event=event,
        )
    emitted.append(json.loads(buffer.getvalue() or "{{}}"))

def drives_agent(payload):
    hook_specific = payload.get("hookSpecificOutput") or {{}}
    return hook_specific.get("permissionDecision") == "deny"

print(json.dumps({{
    "drives_agent": [drives_agent(p) for p in emitted],
    "last_payload_has_system_message": isinstance(emitted[-1].get("systemMessage"), str),
    "last_system_message": emitted[-1].get("systemMessage", ""),
}}))
""",
        environment_overrides={
            "CODEX_RVF_STATE_DIR": str(tmp_path / "state"),
            "RVF_STOP_HOOK_REENTRY_BUDGET_MAX_CONSECUTIVE_AGENT_DIRECTIVES": "3",
        },
    )
    assert result["drives_agent"] == [True, True, True, False], (
        f"上限 3 ⇒ 前三次驱动、第四次必须降级，实际 {result['drives_agent']}"
    )
    assert result["last_payload_has_system_message"], result
    assert "再入预算已耗尽" in result["last_system_message"], result


def test_budget_state_lands_under_the_isolatable_session_hook_state_root(tmp_path: Path) -> None:
    """预算状态必须落在可被 ``CODEX_RVF_STATE_DIR`` 隔离的根下。

    反面是本仓栽过的形状：状态根只认 ``RVF_USER_LOCAL_DATA_ROOT``，而 Stop 的测试只设
    ``CODEX_RVF_STATE_DIR`` ⇒ 测试静默写进真实的 ``~/.rvf``（live infra 污染）。
    """
    state_root = tmp_path / "state"
    _run_python(
        f"""
import json, sys
sys.path.insert(0, {str(STOP_HOOK_CORE.parent)!r})
import stop_review_validate_fix as stop
decision = stop._consume_stop_hook_reentry_budget_for_event(
    {{"session_id": "isolation-probe"}}, "isolation_probe_reason"
)
print(json.dumps({{"count": decision.consecutive_agent_directive_count}}))
""",
        environment_overrides={"CODEX_RVF_STATE_DIR": str(state_root)},
    )
    written = list(state_root.rglob("stop-hook-reentry-budget/*.json"))
    assert written, f"预算状态未落在隔离根下：{list(state_root.rglob('*'))}"


# ------------------------------------------------------ 副作用提交层（降级不得留下搁浅状态）


_DECISION_POINT_BUDGET_EXHAUSTION_SCENARIO = """
import io, json, sys
from contextlib import redirect_stdout
sys.path.insert(0, {scripts_dir!r})
import stop_review_validate_fix as stop
import rvf_decision_token_store as decision_token_store
import agentic_initiation_injection_attempt_ledger as injection_attempt_ledger
from core.stop_hook_output_audience import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY as DENY_CHANNEL,
)


class FakeLedger:
    def event(self, **kwargs):
        pass

    def summary(self, **kwargs):
        pass

    def hook_payload(self, *, status, reason_code, message=None, **kwargs):
        return {{"continue": True, "systemMessage": message}}


class FakeHostAdapter:
    def stop_hook_agent_directive_channel(self):
        return DENY_CHANNEL

    def host_display_name(self):
        return "FakeHost"


WORKTREE = {worktree!r}
SESSION = "decision-point-budget-exhaustion-session"
FINGERPRINT = "candidate-fingerprint-under-test"
EVENT = {{"session_id": SESSION}}

# 模拟本会话此前已经连续驱动过 N 次 agent（每一次都是「上一个 Stop 进程」干的，故交接槽必须
# 一并清掉，否则本轮的决策层会白捡上一轮的判定）。
for _ in range({preceding_agent_directive_count}):
    stop._consume_stop_hook_reentry_budget_for_event(EVENT, stop.REASON_DECISION_POINT_INJECTED)
stop._STOP_HOOK_REENTRY_BUDGET_DECISION_HANDED_OFF_TO_THE_EXIT_LAYER.clear()

mint = decision_token_store.mint_decision_token(
    worktree=WORKTREE,
    session_id=SESSION,
    candidate_fingerprint=FINGERPRINT,
    candidate_summary={{"paths": ["a.py"], "candidate_path_count": 1}},
)
assert mint.outcome == decision_token_store.MINT_OUTCOME_MINTED, mint.outcome
token = mint.token

decision = stop._decision_point_self_injection_decision(
    token=token,
    summary={{"paths": ["a.py"], "candidate_path_count": 1}},
    fork_context=None,
    ledger=FakeLedger(),
    repo=WORKTREE,
    cwd=WORKTREE,
    worktree=WORKTREE,
    session_id=SESSION,
    fingerprint=FINGERPRINT,
    candidate_count=1,
    has_committed=False,
    gate_status="clean",
    lost_warnings=[],
    event=EVENT,
)

buffer = io.StringIO()
with redirect_stdout(buffer):
    stop.emit_stop_hook_payload(
        audience=decision.audience,
        agent_directive=decision.agent_directive,
        developer_only_payload=decision.payload,
        host_adapter=FakeHostAdapter(),
        ledger=None,
        reason_code=decision.reason_code,
        event=EVENT,
    )
payload = json.loads(buffer.getvalue() or "{{}}")

attempts = injection_attempt_ledger.read_injection_attempts(worktree=WORKTREE)
next_mint = decision_token_store.mint_decision_token(
    worktree=WORKTREE, session_id=SESSION, candidate_fingerprint=FINGERPRINT
)
budget_state_files = sorted(
    (stop.session_hook_state_dir() / "stop-hook-reentry-budget").glob("*.json")
)
budget_state = json.loads(budget_state_files[0].read_text(encoding="utf-8"))

print(json.dumps({{
    "decision_audience": decision.audience,
    "decision_has_agent_directive": decision.agent_directive is not None,
    "payload_drives_agent": (
        (payload.get("hookSpecificOutput") or {{}}).get("permissionDecision") == "deny"
    ),
    "banner_needle_reached_agent": (
        "RVF_DECISION_TOKEN=" + token in json.dumps(payload, ensure_ascii=False)
    ),
    "developer_sees_exhaustion_diagnostic": "再入预算已耗尽" in (payload.get("systemMessage") or ""),
    "token_state": decision_token_store.read_decision_token(token).state,
    "recorded_injected_flags": [bool(record.get("injected")) for record in attempts],
    "next_stop_mint_outcome": next_mint.outcome,
    "consecutive_agent_directive_count": budget_state["consecutive_agent_directive_count"],
}}, ensure_ascii=False))
"""


def test_budget_exhaustion_never_strands_the_decision_point_token_or_its_dedup_claim(
    tmp_path: Path,
) -> None:
    """预算降级必须发生在「宣称 agent 已收到正文」的副作用**之前**。

    反面形状（本测试要挡的回归）：降级闸只装在 ``emit_stop_hook_payload`` 里，而决策层已经
    提交完 ``mark_injected`` + INJECTED 记账 + 去重索引占位。丢掉 ``agent_directive`` 会让
    ``mark_injected`` 的成立前提（正文由 ``main()`` 同步写 stdout）变假，于是：

    * token 停在 ``injected`` 这个**活跃非终态**上，而根本没人看过它；
    * 去重索引仍被占着 ⇒ 后续同指纹 Stop 一律 ``deduplicated`` 走静默路径，整个 TTL 窗口
      内用户等不到横幅；
    * 注入尝试 ledger 记下一条 ``injected=True``，liveness 分子被一次从未送达的注入撑高。
    """
    result = _run_python(
        _DECISION_POINT_BUDGET_EXHAUSTION_SCENARIO.format(
            scripts_dir=str(STOP_HOOK_CORE.parent),
            worktree=str(tmp_path / "worktree"),
            preceding_agent_directive_count=1,
        ),
        environment_overrides={
            "RVF_HOME": str(tmp_path / "rvf-home"),
            "CODEX_RVF_STATE_DIR": str(tmp_path / "state"),
            "RVF_STOP_HOOK_REENTRY_BUDGET_MAX_CONSECUTIVE_AGENT_DIRECTIVES": "1",
        },
    )

    assert result["decision_audience"] == "developer_only", (
        f"预算耗尽时决策层就该返回 developer_only，实际 {result['decision_audience']}"
    )
    assert result["decision_has_agent_directive"] is False, result
    assert result["payload_drives_agent"] is False, result
    assert result["banner_needle_reached_agent"] is False, (
        f"横幅正文（含 RVF_DECISION_TOKEN={{token}} needle）绝不能随降级出口发出去：{result}"
    )
    assert result["developer_sees_exhaustion_diagnostic"] is True, result
    assert result["token_state"] == "withdrawn", (
        "没送达的横幅其 token 必须落到 withdrawn 终态；停在 injected = 谎报 agent 已收到，"
        f"实际 {result['token_state']}"
    )
    assert result["recorded_injected_flags"] == [False], (
        f"未送达的注入不得记成 injected=True（liveness 分子污染），实际 {result['recorded_injected_flags']}"
    )
    assert result["next_stop_mint_outcome"] == "minted", (
        "去重索引必须随撤回一并清掉，否则整个 TTL 窗口内后续 Stop 全被静默去重，"
        f"实际 {result['next_stop_mint_outcome']}"
    )


def test_permitted_budget_leaves_the_self_injection_path_unchanged_and_counts_the_stop_once(
    tmp_path: Path,
) -> None:
    """预算未耗尽时，自注入路径必须逐字保持原行为，且这一次 Stop 只计一次。

    两条主张必须同测：
    * 行为不变——否则这道闸本身成了新的漏审来源；
    * 计数只 +1——决策层与出口层各真消费一次的话，每个 Stop 会把连续计数 +2，预算实际只剩
      一半，是一条提前一倍触发的闸。
    """
    result = _run_python(
        _DECISION_POINT_BUDGET_EXHAUSTION_SCENARIO.format(
            scripts_dir=str(STOP_HOOK_CORE.parent),
            worktree=str(tmp_path / "worktree"),
            preceding_agent_directive_count=0,
        ),
        environment_overrides={
            "RVF_HOME": str(tmp_path / "rvf-home"),
            "CODEX_RVF_STATE_DIR": str(tmp_path / "state"),
            "RVF_STOP_HOOK_REENTRY_BUDGET_MAX_CONSECUTIVE_AGENT_DIRECTIVES": "8",
        },
    )

    assert result["decision_audience"] == "agent_must_act_now", result
    assert result["decision_has_agent_directive"] is True, result
    assert result["payload_drives_agent"] is True, result
    assert result["banner_needle_reached_agent"] is True, result
    assert result["token_state"] == "injected", result
    assert result["recorded_injected_flags"] == [True], result
    assert result["next_stop_mint_outcome"] == "deduplicated", (
        "未耗尽路径必须保留 R25 注入幂等：同键活跃 token 在等待时不得重铸"
    )
    assert result["consecutive_agent_directive_count"] == 1, (
        "决策层判定 + 出口层判定必须是同一次消费，实际连续计数 "
        f"{result['consecutive_agent_directive_count']}"
    )


def test_the_exit_layer_handoff_is_one_shot_not_a_standing_free_pass(tmp_path: Path) -> None:
    """决策层交给出口层的那份判定只能兑现一次。

    做成常驻 memo 的话，「本进程内同一 reason_code 的后续出口一律免费」——预算恒不生效，
    正是这道闸最危险的失效方向。
    """
    result = _run_python(
        f"""
import json, sys
sys.path.insert(0, {str(STOP_HOOK_CORE.parent)!r})
import stop_review_validate_fix as stop
event = {{"session_id": "handoff-one-shot-session"}}
handed_off = stop._consume_stop_hook_reentry_budget_for_event(
    event, "same_reason", hand_off_to_the_exit_layer=True
)
taken = stop._take_stop_hook_reentry_budget_decision_for_stop_hook_exit(event, "same_reason")
taken_again = stop._take_stop_hook_reentry_budget_decision_for_stop_hook_exit(event, "same_reason")
print(json.dumps({{
    "counts": [
        handed_off.consecutive_agent_directive_count,
        taken.consecutive_agent_directive_count,
        taken_again.consecutive_agent_directive_count,
    ],
    "handoff_redeemed_the_same_decision": handed_off is taken,
}}))
""",
        environment_overrides={"CODEX_RVF_STATE_DIR": str(tmp_path / "state")},
    )
    assert result["counts"] == [1, 1, 2], (
        "交接只兑现一次：第二次取必须回到真消费并把计数推进到 2，实际 "
        f"{result['counts']}"
    )
    assert result["handoff_redeemed_the_same_decision"] is True, result


# ---------------------------------------------------------------- 复位判据层


def test_user_prompt_submit_resets_only_for_genuine_human_turns() -> None:
    """复位判据必须排除 RVF 自己产生的机器正文。

    dispatch token / origin marker / decision-point token 三者都不是用户轮边界。拿它们复位
    等于让 RVF 的注入把自己的保险丝按掉——预算会恒不生效，是一条恒真的假保险丝。
    """
    source = USER_PROMPT_SUBMIT_HOOK.read_text(encoding="utf-8")
    reset_call_index = source.index("reset_stop_hook_reentry_budget_on_user_prompt_submit(\n")
    guard = "if token is None and origin_marker is None and decision_point_token is None:"
    guard_index = source.rindex(guard, 0, reset_call_index)
    between = source[guard_index + len(guard) : reset_call_index]
    assert "\nif " not in between and "\n    if " not in between.replace(
        "            if isinstance(reentry_budget_session_id, str)", ""
    ), "复位调用与三态守卫之间混入了别的顶层条件，守卫可能已失效"
    assert "decision_point_token is None" in guard
