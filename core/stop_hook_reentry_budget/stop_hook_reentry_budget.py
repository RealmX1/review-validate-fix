"""Stop hook 驱动 agent 的**跨 turn** 再入预算（host-中性）。

**为什么必须由 RVF 自带**

宿主自己的防递归保险丝各家都不够用，且失效方式不同：

- Claude / Codex 在事件里给 ``stop_hook_active``，RVF 已据此短路（见
  ``evaluate_stop_event`` 的第一道闸）。那条闸管的是「Stop hook 正在执行时又触发 Stop」
  这种**同一回合内**的递归。
- Kimi Code CLI 的 ``stop_hook_active`` **恒为 false**（v1/v2 引擎求值均为 false），
  而引擎自己的 ``stopHookContinuationUsed`` **每个 turn 复位** ⇒ 「driven 一回合 → agent
  停下 → 再 driven 一回合」这种**跨 turn** 的循环在宿主侧完全没有上界。

跨 turn 循环不是理论风险：本仓已实测过一次 286 次无上界续跑（见计划附录 A），
最后靠人工 kill。那次的成因不同（hook 根本没跑起来，本模块对它无效——它活在
``stop.py`` 内部，而当时 ``stop.py`` 打不开），但**上界本身**是同一条缺口。

**范围：只管会驱动 agent 的那类出口。** ``developer_only`` 受众按定义不驱动 agent 再跑
一回合，永远不消耗预算；预算只对 ``agent_must_act_now`` 计数。超预算时的正确降级是
把该出口**降成 developer_only 诊断**——而不是静默丢弃，也不是继续驱动。这与 hook 出口
纪律一致：失败 / 降级 / 诊断一律 ``developer_only``。

**复位三条**（任一命中即把连续计数清零）：

1. ``digest_changed``——驱动正文的稳定摘要变了 ⇒ 这是新的事，不是同一件事被反复催。
2. ``user_prompt_submitted``——用户提交了新 prompt（由 UPS 侧显式调用 reset）⇒ 出现了
   真实的人类回合，循环已被打破。
3. ``ttl_expired``——距上次驱动超过 TTL ⇒ 会话已停顿，旧计数不该继续累积。

⚠️ **第 1 条是双刃的，且默认就会踩中**：``stable_agent_directive_scope_digest_source``
里若混进时间戳、token、run id 这类**每次都变**的成分，digest 就每次都变、复位每次都命中、
预算**永不生效**——一条看起来在保护、实际恒真的假保险丝。

这不是理论风险：最容易顺手传进来的「驱动正文」恰好是最坏的选择——发起决策点横幅每次
都带一枚新铸的 ``RVF_DECISION_TOKEN=<token>``，拿正文做 digest 等于关掉这道闸。

**推荐传 ``reason_code``**：它是「这次驱动属于哪一类事」的稳定标识，同一类事被反复催 ⇒
digest 不变 ⇒ 计数累积（正是要抓的形状）；换了一类事 ⇒ 复位（正是该放行的形状）。
粒度偏粗是刻意的：粗 = 更容易累积 = 保险丝更紧，而这道闸的失效方向必须偏严。

本模块是纯函数式的：时间与环境可注入，状态目录由调用方给，不读 ``os.environ`` 之外的
任何全局，也**不知道任何 host 名字**（``core/`` 的 host-中性不变量）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

STOP_HOOK_REENTRY_BUDGET_MAXIMUM_ENVIRONMENT_VARIABLE = (
    "RVF_STOP_HOOK_REENTRY_BUDGET_MAX_CONSECUTIVE_AGENT_DIRECTIVES"
)
STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS_ENVIRONMENT_VARIABLE = (
    "RVF_STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS"
)

DEFAULT_STOP_HOOK_REENTRY_BUDGET_MAXIMUM_CONSECUTIVE_AGENT_DIRECTIVES = 8
"""连续多少次「驱动 agent」的 Stop 出口之后开始拒绝。

8 的依据：正常路径上连续驱动只有两种来源——决策点横幅（自带去重与冷却，不会连发）与
「RVF 未完成的续跑提示」（每次之间隔着 agent 真的干一轮活）。连续 8 次仍未收敛，
本身就已经是异常；而真正的死循环是秒级刷屏，第 9 次之前就会撞上限。
"""

DEFAULT_STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS = 1800.0
"""距上次驱动超过这么久就清零。

方向容易搞反：**TTL 越短，保险丝越松**（计数更容易被清掉）。所以它必须显著长于正常
相邻两次驱动的间隔（agent 干一轮活的时间，通常分钟级），否则计数永远累积不起来，
预算形同虚设。取 30 分钟：正常间隔远小于它，而隔了半小时没动静的会话不该继续背着旧计数。
"""

_SESSION_IDENTIFIER_SAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9_.-]")
_SESSION_IDENTIFIER_MAX_CHARACTERS = 96

BUDGET_RESET_REASON_DIGEST_CHANGED = "digest_changed"
BUDGET_RESET_REASON_TTL_EXPIRED = "ttl_expired"
BUDGET_RESET_REASON_USER_PROMPT_SUBMITTED = "user_prompt_submitted"


@dataclass(frozen=True)
class StopHookReentryBudgetDecision:
    """一次预算判定的完整结果。

    ``agent_directive_permitted=False`` 时调用方**必须**把该出口降级成 developer_only，
    并把 ``exhaustion_diagnostic`` 作为诊断正文——不要静默丢弃（开发者会失去唯一线索），
    也不要照旧驱动（那样这道闸就白加了）。
    """

    agent_directive_permitted: bool
    consecutive_agent_directive_count: int
    budget_maximum: int
    budget_reset_reason: str | None
    exhaustion_diagnostic: str | None


def _sanitize_session_identifier_for_filename(session_identifier: str) -> str:
    """会话 id 直接进文件名，故必须限长并只保留文件名安全字符。

    空 / 全非法字符的 id 折成固定 bucket 而不是抛错：判不出会话身份时宁可让所有这类
    出口共用一个更严的预算，也不要让 Stop hook 因为一个 id 格式问题而失声。
    """
    sanitized = _SESSION_IDENTIFIER_SAFE_CHARACTERS.sub("-", session_identifier or "")
    sanitized = sanitized.strip("-")[:_SESSION_IDENTIFIER_MAX_CHARACTERS]
    return sanitized or "unidentified-session"


def _budget_state_path(state_directory: Path, session_identifier: str) -> Path:
    return (
        Path(state_directory)
        / "stop-hook-reentry-budget"
        / f"{_sanitize_session_identifier_for_filename(session_identifier)}.json"
    )


def _digest_of(stable_agent_directive_scope_digest_source: str) -> str:
    return hashlib.sha256(
        (stable_agent_directive_scope_digest_source or "").encode("utf-8")
    ).hexdigest()


def _read_budget_state(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 状态不可读一律当作「没有历史」，永不阻断出口
        return {}
    return document if isinstance(document, dict) else {}


def _write_budget_state(path: Path, document: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary_path.replace(path)
    except Exception:  # noqa: BLE001 - 落盘失败不得让 Stop hook 失声
        pass


def _resolve_positive_number(
    environment: Mapping[str, str], variable_name: str, default_value: float
) -> float:
    raw_value = environment.get(variable_name)
    if raw_value is None:
        return default_value
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return default_value
    return parsed if parsed > 0 else default_value


def consume_stop_hook_reentry_budget(
    *,
    session_identifier: str,
    stable_agent_directive_scope_digest_source: str,
    state_directory: Path | str,
    now_epoch_seconds: float | None = None,
    environment: Mapping[str, str] | None = None,
) -> StopHookReentryBudgetDecision:
    """为一次**将要驱动 agent** 的 Stop 出口消费预算。

    只在 ``audience == agent_must_act_now`` 时调用；developer_only 出口不该经过这里
    （经过了也只会白白消耗预算，把真正需要驱动的那次挤掉）。
    """
    environment = os.environ if environment is None else environment
    now = time.time() if now_epoch_seconds is None else now_epoch_seconds
    budget_maximum = int(
        _resolve_positive_number(
            environment,
            STOP_HOOK_REENTRY_BUDGET_MAXIMUM_ENVIRONMENT_VARIABLE,
            float(DEFAULT_STOP_HOOK_REENTRY_BUDGET_MAXIMUM_CONSECUTIVE_AGENT_DIRECTIVES),
        )
    )
    ttl_seconds = _resolve_positive_number(
        environment,
        STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS_ENVIRONMENT_VARIABLE,
        DEFAULT_STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS,
    )

    path = _budget_state_path(Path(state_directory), session_identifier)
    state = _read_budget_state(path)
    current_digest = _digest_of(stable_agent_directive_scope_digest_source)

    previous_count_raw = state.get("consecutive_agent_directive_count")
    previous_count = previous_count_raw if isinstance(previous_count_raw, int) else 0
    previous_digest = state.get("last_agent_directive_body_digest")
    previous_epoch_raw = state.get("last_agent_directive_epoch_seconds")
    previous_epoch = (
        float(previous_epoch_raw) if isinstance(previous_epoch_raw, (int, float)) else None
    )

    budget_reset_reason: str | None = None
    if previous_epoch is None or (now - previous_epoch) > ttl_seconds:
        budget_reset_reason = BUDGET_RESET_REASON_TTL_EXPIRED
    elif previous_digest != current_digest:
        budget_reset_reason = BUDGET_RESET_REASON_DIGEST_CHANGED

    consecutive_count = (0 if budget_reset_reason else previous_count) + 1
    permitted = consecutive_count <= budget_maximum

    _write_budget_state(
        path,
        {
            "consecutive_agent_directive_count": consecutive_count,
            "last_agent_directive_body_digest": current_digest,
            "last_agent_directive_epoch_seconds": now,
        },
    )

    exhaustion_diagnostic = None
    if not permitted:
        exhaustion_diagnostic = (
            "review-validate-fix: Stop hook 再入预算已耗尽"
            f"（连续第 {consecutive_count} 次驱动 agent，上限 {budget_maximum}），"
            "本次出口已降级为诊断、不驱动 agent。"
            "复位条件：驱动正文变化 / 用户提交新 prompt / "
            f"距上次驱动超过 {ttl_seconds:.0f}s。"
            f"如需临时放宽：{STOP_HOOK_REENTRY_BUDGET_MAXIMUM_ENVIRONMENT_VARIABLE}=<n>。"
        )

    return StopHookReentryBudgetDecision(
        agent_directive_permitted=permitted,
        consecutive_agent_directive_count=consecutive_count,
        budget_maximum=budget_maximum,
        budget_reset_reason=budget_reset_reason,
        exhaustion_diagnostic=exhaustion_diagnostic,
    )


def reset_stop_hook_reentry_budget_on_user_prompt_submit(
    *,
    session_identifier: str,
    state_directory: Path | str,
) -> bool:
    """用户提交了新 prompt ⇒ 出现了真实人类回合，连续驱动链被打破。

    由 UPS hook 侧调用。返回是否真的清掉了状态（没有历史状态时返回 ``False``，
    这不是错误）。**没有 UPS 的 host 缺这条复位路径**，只剩 digest 与 TTL 两条——
    对它们来说预算会更容易累积到上限，这是刻意的保守方向。
    """
    path = _budget_state_path(Path(state_directory), session_identifier)
    try:
        if not path.is_file():
            return False
        path.unlink()
        return True
    except Exception:  # noqa: BLE001 - 复位失败只会让预算偏严，永不阻断
        return False


def _self_check() -> None:
    """就地自检：三条复位路径 + 上限行为 + 状态不可读时的 fail-open。"""
    import tempfile

    with tempfile.TemporaryDirectory() as temporary_directory:
        state_directory = Path(temporary_directory)
        session = "session_self_check"
        environment = {STOP_HOOK_REENTRY_BUDGET_MAXIMUM_ENVIRONMENT_VARIABLE: "3"}

        # 同一 digest 连续消费：第 4 次越界。
        decisions = [
            consume_stop_hook_reentry_budget(
                session_identifier=session,
                stable_agent_directive_scope_digest_source="same-body",
                state_directory=state_directory,
                now_epoch_seconds=1000.0 + index,
                environment=environment,
            )
            for index in range(4)
        ]
        assert [d.consecutive_agent_directive_count for d in decisions] == [1, 2, 3, 4], decisions
        assert [d.agent_directive_permitted for d in decisions] == [True, True, True, False], decisions
        assert decisions[-1].exhaustion_diagnostic, "越界必须给出诊断正文"
        assert decisions[0].budget_reset_reason == BUDGET_RESET_REASON_TTL_EXPIRED, (
            "首次消费无历史 ⇒ 记为 TTL 复位"
        )

        # 复位 ①：digest 变化。
        changed = consume_stop_hook_reentry_budget(
            session_identifier=session,
            stable_agent_directive_scope_digest_source="different-body",
            state_directory=state_directory,
            now_epoch_seconds=1004.0,
            environment=environment,
        )
        assert changed.budget_reset_reason == BUDGET_RESET_REASON_DIGEST_CHANGED, changed
        assert changed.consecutive_agent_directive_count == 1, changed
        assert changed.agent_directive_permitted, changed

        # 复位 ②：TTL 过期。
        expired = consume_stop_hook_reentry_budget(
            session_identifier=session,
            stable_agent_directive_scope_digest_source="different-body",
            state_directory=state_directory,
            now_epoch_seconds=1004.0 + DEFAULT_STOP_HOOK_REENTRY_BUDGET_TTL_SECONDS + 1.0,
            environment=environment,
        )
        assert expired.budget_reset_reason == BUDGET_RESET_REASON_TTL_EXPIRED, expired
        assert expired.consecutive_agent_directive_count == 1, expired

        # 复位 ③：UPS。
        assert reset_stop_hook_reentry_budget_on_user_prompt_submit(
            session_identifier=session, state_directory=state_directory
        ), "有历史状态时应返回 True"
        assert not reset_stop_hook_reentry_budget_on_user_prompt_submit(
            session_identifier=session, state_directory=state_directory
        ), "无历史状态时应返回 False 而不是抛错"
        after_reset = consume_stop_hook_reentry_budget(
            session_identifier=session,
            stable_agent_directive_scope_digest_source="same-body",
            state_directory=state_directory,
            now_epoch_seconds=2000.0,
            environment=environment,
        )
        assert after_reset.consecutive_agent_directive_count == 1, after_reset

        # 会话 id 不合法 ⇒ 折进固定 bucket 而不是抛错。
        odd = consume_stop_hook_reentry_budget(
            session_identifier="///",
            stable_agent_directive_scope_digest_source="body",
            state_directory=state_directory,
            now_epoch_seconds=3000.0,
            environment=environment,
        )
        assert odd.agent_directive_permitted, odd

        # 状态文件损坏 ⇒ 当作没有历史，永不抛。
        corrupted_path = _budget_state_path(state_directory, session)
        corrupted_path.parent.mkdir(parents=True, exist_ok=True)
        corrupted_path.write_text("{not json", encoding="utf-8")
        recovered = consume_stop_hook_reentry_budget(
            session_identifier=session,
            stable_agent_directive_scope_digest_source="body",
            state_directory=state_directory,
            now_epoch_seconds=4000.0,
            environment=environment,
        )
        assert recovered.consecutive_agent_directive_count == 1, recovered

    print("STOP_HOOK_REENTRY_BUDGET_SELF_CHECK=OK")


if __name__ == "__main__":
    _self_check()
