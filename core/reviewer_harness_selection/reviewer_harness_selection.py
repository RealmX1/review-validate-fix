"""external reviewer harness 选择纯核(R15/R20,S7 抽取)。

规则语义(与 ``dispatch_reviewers.route`` 的 R0–R3 一一对应,降级路径维持既有语义):

- 合格池 = 调用方给出的 ``available_harnesses``(已过 enabled/临时禁用/冷却过滤,
  按 priority 排序)。
- **Tier-1 池 − 主 harness,池内无偏序**:两腿选择池 = 非主 harness;非主不足两路时
  主 harness 也以 external 跑(绝不退回 in-harness)。
- **无偏序的可执行语义 = 合格池内伪随机选二**(R15/R20 裁决):选择池 >2 时用
  记录在案的 seed 伪随机采样,seed 落遥测(reviewer-plan 的 ``reviewer_selection``
  块)可复现;选择池恰为 2 时为受迫配对(forced pair,无随机性,不记 seed)。
- R2(仅 1 可用)= 同 harness 双 external 实例;R3(0 可用)= last-resort 兜底,
  两者语义不变、不参与伪随机。

**SANTA 复用接口**::func:`pseudo_random_select_from_eligible_pool` 是通用的
「从合格池无偏序选 K」机制,与 reviewer 语境解耦——SANTA-method 的异构模型
评审 fork(claude/codex/cursor 同 artifact 同 rubric)可直接复用它做无偏序
选材,seed 同样落各自遥测以保证可复现。

本模块零 IO、零环境读取,便于单测与跨宿主复用。
"""
from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, field
from typing import Sequence

# 路由规则 id(沿用 dispatch_reviewers 的 R0–R3 命名,常量名自解释)。
RULE_TWO_OR_MORE_AVAILABLE_PSEUDO_RANDOM_PAIR = "R0"
RULE_EXACTLY_TWO_AVAILABLE_FORCED_PAIR = "R1"
RULE_SINGLE_AVAILABLE_SAME_HARNESS_DOUBLE = "R2"
RULE_ZERO_AVAILABLE_LAST_RESORT = "R3"

SELECTION_MODE_PSEUDO_RANDOM_TWO = "pseudo_random_two"
SELECTION_MODE_FORCED_PAIR = "forced_pair"
SELECTION_MODE_SAME_HARNESS_DOUBLE = "same_harness_double"
SELECTION_MODE_NONE_AVAILABLE = "none_available"

_SEED_BITS = 32


def _generate_selection_seed() -> int:
    """生成一次性选材 seed(记录进遥测后即可复现采样)。"""
    return secrets.randbits(_SEED_BITS)


def pseudo_random_select_from_eligible_pool(
    eligible_pool: Sequence[str],
    *,
    select_count: int,
    seed: int,
) -> list[str]:
    """从合格池按记录在案的 seed 伪随机选 ``select_count`` 项(无偏序、可复现)。

    采样前先对池做字典序排序,使结果只取决于 ``(池成员集合, seed)`` 而与调用方
    传入顺序无关——同一 seed 在任何入序下都复现同一选择(含 slot 顺序)。

    SANTA 复用接口:该函数与 reviewer 语境无关,任何「从合格候选集无偏序选 K」
    的场景(如 SANTA-method 异构评审模型选材)都可直接调用。
    """
    if select_count < 0:
        raise ValueError(f"select_count must be >= 0, got {select_count}")
    normalized_pool = sorted(dict.fromkeys(eligible_pool))
    if select_count > len(normalized_pool):
        raise ValueError(
            f"select_count {select_count} exceeds eligible pool size {len(normalized_pool)}"
        )
    return random.Random(seed).sample(normalized_pool, select_count)


@dataclass(frozen=True)
class ReviewerHarnessSelection:
    """一次 external reviewer harness 选择的结构化结果(纯数据,供 route 装配 plan)。

    ``selection_seed`` 仅在 ``selection_mode == pseudo_random_two`` 时非 None;
    调用方须把 :meth:`telemetry_payload` 落入 plan/遥测以保证可复现。
    """

    routing_rule: str
    selected_harnesses: tuple[str, ...]
    selection_mode: str
    selection_pool: tuple[str, ...]
    selection_seed: int | None = None
    main_harness_used_as_external: bool = False
    only_available_is_main: bool | None = None
    available_harnesses: tuple[str, ...] = field(default=())

    def telemetry_payload(self) -> dict[str, object]:
        """选材遥测块(落 reviewer-plan ``reviewer_selection``,seed 可复现)。"""
        payload: dict[str, object] = {
            "routing_rule": self.routing_rule,
            "selection_mode": self.selection_mode,
            "selection_pool": list(self.selection_pool),
            "selected_harnesses": list(self.selected_harnesses),
            "main_harness_used_as_external": self.main_harness_used_as_external,
        }
        if self.selection_seed is not None:
            payload["selection_seed"] = self.selection_seed
        return payload


def compute_reviewer_harness_selection(
    *,
    main_harness: str,
    available_harnesses: Sequence[str],
    selection_seed: int | None = None,
) -> ReviewerHarnessSelection:
    """给定主 harness 与(已过滤排序的)可用集,产出 R0–R3 选择结果(纯函数)。

    - ``len(available) == 0`` → R3(零可用,交调用方走 last-resort / fail 语义)。
    - ``len(available) == 1`` → R2(同 harness 双 external 实例;是否与主相同由
      ``only_available_is_main`` 报告,warning 措辞归调用方)。
    - ``len(available) >= 2`` → R1(恰两路)/ R0(三路及以上):两腿选择池 =
      非主 harness;非主不足两路时补主 harness 作 external。选择池恰 2 → 受迫
      配对(保持传入的 priority 顺序定 slot);>2 → 伪随机选二(seed 未给则
      现场生成并记录,同 seed 严格可复现)。
    """
    available = list(dict.fromkeys(available_harnesses))
    if not available:
        return ReviewerHarnessSelection(
            routing_rule=RULE_ZERO_AVAILABLE_LAST_RESORT,
            selected_harnesses=(),
            selection_mode=SELECTION_MODE_NONE_AVAILABLE,
            selection_pool=(),
            available_harnesses=(),
        )
    if len(available) == 1:
        only = available[0]
        return ReviewerHarnessSelection(
            routing_rule=RULE_SINGLE_AVAILABLE_SAME_HARNESS_DOUBLE,
            selected_harnesses=(only, only),
            selection_mode=SELECTION_MODE_SAME_HARNESS_DOUBLE,
            selection_pool=(only,),
            main_harness_used_as_external=(only == main_harness),
            only_available_is_main=(only == main_harness),
            available_harnesses=tuple(available),
        )

    routing_rule = (
        RULE_TWO_OR_MORE_AVAILABLE_PSEUDO_RANDOM_PAIR
        if len(available) >= 3
        else RULE_EXACTLY_TWO_AVAILABLE_FORCED_PAIR
    )
    non_main = [h for h in available if h != main_harness]
    if len(non_main) >= 2:
        selection_pool = non_main
    else:
        # 非主不足两路:主 harness 也以 external 跑(绝不退回 in-harness)。
        selection_pool = [*non_main, main_harness]

    if len(selection_pool) == 2:
        selected = tuple(selection_pool)
        return ReviewerHarnessSelection(
            routing_rule=routing_rule,
            selected_harnesses=selected,
            selection_mode=SELECTION_MODE_FORCED_PAIR,
            selection_pool=tuple(selection_pool),
            main_harness_used_as_external=main_harness in selected,
            available_harnesses=tuple(available),
        )

    seed = selection_seed if selection_seed is not None else _generate_selection_seed()
    selected = tuple(
        pseudo_random_select_from_eligible_pool(selection_pool, select_count=2, seed=seed)
    )
    return ReviewerHarnessSelection(
        routing_rule=routing_rule,
        selected_harnesses=selected,
        selection_mode=SELECTION_MODE_PSEUDO_RANDOM_TWO,
        selection_pool=tuple(sorted(dict.fromkeys(selection_pool))),
        selection_seed=seed,
        main_harness_used_as_external=main_harness in selected,
        available_harnesses=tuple(available),
    )
