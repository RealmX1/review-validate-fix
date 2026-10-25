"""选腿层的「同额度池互斥」契约（计划 §8 Q2b）。

**为什么需要**：omp 腿的默认选模限定为 `openai-codex`，与既有 codex 腿共用
同一个 Codex 订阅账号。于是两件事同时发生：
① 抽到 {codex, oh_my_pi} 时，若两边默认模型相同，双评审就是同一模型评两遍；
② 该订阅额度耗尽会**同时**打掉两条腿，而 fail-over 逻辑假设各腿相互独立。

**为什么指纹取「额度池」而不是「模型名」**：各腿都可跟随 CLI 默认模型（用户随时可改），
OMP 的动态默认模型始终限定为 Codex provider。把指纹写成模型名，codex 那条
第二天就会撒谎。而额度池由鉴权来源决定、不随模型配置漂移，且「额度耗尽同打两腿」
这个后果本就比「同模型评两遍」更严重。代价是粒度偏粗：cursor 即便底层也在跑
Claude，因为走的是 Cursor 自己的订阅额度，这里判为不同池——这是刻意的取舍。

**为什么碰撞时是降级而不是拒绝**：若池内只剩同池候选，退回 R2「同一 harness 跑
两遍」会更同质（连 harness 都相同），比同池的两个不同 harness 更差。所以碰撞不可
避免时接受同池配对并标记，绝不因此减少腿数。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
REGISTRY_PATH = SKILL_ROOT / "config/reviewer-registry.json"
QUOTA_POOL_FIELD = "underlying_provider_account_quota_pool_identity"

sys.path.insert(0, str(ROOT))
from core.reviewer_harness_selection import (  # noqa: E402
    compute_reviewer_harness_selection,
    pseudo_random_select_two_avoiding_shared_quota_pool,
)


def _load_dispatch_reviewers():
    scripts_dir = SKILL_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "dispatch_reviewers_for_quota_pool_tests", scripts_dir / "dispatch_reviewers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _quota_pools() -> dict[str, str]:
    return {
        harness_id: spec[QUOTA_POOL_FIELD]
        for harness_id, spec in _registry()["harnesses"].items()
    }


# --- QP-1 · 出厂 registry 必须完整声明指纹 -------------------------------------


def test_every_shipped_harness_declares_its_quota_pool_identity():
    """运行时对「未声明」是宽松的（视为独立池），所以漏填必须在 gate 层被抓住。"""
    missing = sorted(
        harness_id
        for harness_id, spec in _registry()["harnesses"].items()
        if not str(spec.get(QUOTA_POOL_FIELD, "")).strip()
    )
    assert missing == [], f"这些 harness 未声明 {QUOTA_POOL_FIELD}: {missing}"


def test_codex_and_oh_my_pi_are_declared_as_the_same_quota_pool():
    """这条互斥的存在理由本身——omp 被 pin 到 Codex 订阅，与 codex 腿同池。"""
    pools = _quota_pools()
    assert pools["codex"] == pools["oh_my_pi"]


def test_the_other_legs_do_not_collide_with_each_other():
    """过度互斥会把可用组合压没——除 codex/oh_my_pi 外不得有第二对同池。"""
    pools = _quota_pools()
    independent = {k: v for k, v in pools.items() if k != "oh_my_pi"}
    assert len(set(independent.values())) == len(independent), independent


# --- QP-2 · 选二的互斥语义 -----------------------------------------------------


def test_selection_avoids_a_shared_quota_pool_when_a_disjoint_pair_exists():
    pools = {"a": "pool-x", "b": "pool-x", "c": "pool-y"}
    for seed in range(64):
        selected, collision_accepted = (
            pseudo_random_select_two_avoiding_shared_quota_pool(
                ["a", "b", "c"], quota_pool_identity_by_harness=pools, seed=seed
            )
        )
        assert len(selected) == 2
        assert collision_accepted is False
        assert pools[selected[0]] != pools[selected[1]], (seed, selected)


def test_selection_accepts_a_shared_pool_only_when_no_disjoint_pair_exists():
    """全池同源时不得减少腿数——两腿仍要出，只是标记这次被迫同池。"""
    pools = {"a": "pool-x", "b": "pool-x", "c": "pool-x"}
    selected, collision_accepted = pseudo_random_select_two_avoiding_shared_quota_pool(
        ["a", "b", "c"], quota_pool_identity_by_harness=pools, seed=7
    )
    assert len(selected) == 2
    assert collision_accepted is True


def test_undeclared_harnesses_are_treated_as_independent_pools():
    """未声明 ≠ 与所有人碰撞：否则新加一条还没填指纹的腿会让选腿直接选不出来。"""
    pools = {"a": "pool-x"}
    selected, collision_accepted = pseudo_random_select_two_avoiding_shared_quota_pool(
        ["a", "b"], quota_pool_identity_by_harness=pools, seed=3
    )
    assert sorted(selected) == ["a", "b"]
    assert collision_accepted is False


def test_two_undeclared_harnesses_do_not_collide_with_each_other():
    selected, collision_accepted = pseudo_random_select_two_avoiding_shared_quota_pool(
        ["a", "b"], quota_pool_identity_by_harness={}, seed=3
    )
    assert sorted(selected) == ["a", "b"]
    assert collision_accepted is False


# --- QP-3 · 保住既有的可复现/无偏序性质 ----------------------------------------


def test_same_seed_reproduces_the_same_selection_including_slot_order():
    pools = {"a": "pool-x", "b": "pool-y", "c": "pool-z", "d": "pool-w"}
    first, _ = pseudo_random_select_two_avoiding_shared_quota_pool(
        ["a", "b", "c", "d"], quota_pool_identity_by_harness=pools, seed=99
    )
    second, _ = pseudo_random_select_two_avoiding_shared_quota_pool(
        ["d", "c", "b", "a"], quota_pool_identity_by_harness=pools, seed=99
    )
    assert first == second


def test_every_disjoint_pair_remains_reachable():
    """互斥不得把选择压成常量——剩余组合仍要无偏序覆盖。"""
    pools = {"a": "pool-x", "b": "pool-x", "c": "pool-y", "d": "pool-z"}
    seen = {
        tuple(
            sorted(
                pseudo_random_select_two_avoiding_shared_quota_pool(
                    ["a", "b", "c", "d"],
                    quota_pool_identity_by_harness=pools,
                    seed=seed,
                )[0]
            )
        )
        for seed in range(400)
    }
    assert ("a", "b") not in seen
    assert seen == {("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "d")}


def test_slot_order_is_unbiased_and_not_a_function_of_the_chosen_pair():
    """slot 顺序必须独立随机,不能退化成「所选 pair 的确定函数」。

    回归钉:选 pair 与定 slot 顺序若各起一个同 seed 的 ``random.Random(seed)``,两者
    会从同一条流的相同前导 bit 取值 —— pair 分布仍然均匀,但 slot 顺序恒定。修复前
    出厂 5 腿池实测:9 个异池组合里 8 个顺序恒定,字典序最小的那条腿落 slot 1 的
    概率为 0。这与本函数声称的「与不带互斥的 sample() 一致」相悖(sample() 无偏)。

    断言是**确定性**的(seed 遍历 ``range(2000)``,不是随机抽样),故不会偶发假红;
    容差仍开到 [0.30, 0.70] 以容纳实现细节的正常抖动(实测区间 0.45–0.56)。
    """
    pools = {
        "alpha": "pool-1",
        "bravo": "pool-2",
        "charlie": "pool-3",
        "delta": "pool-4",
        "echo": "pool-4",  # 与 delta 同池:模拟出厂 codex/oh_my_pi 的形状
    }
    names = sorted(pools)
    seed_count = 2000
    ordered_counts: dict[tuple[str, str], int] = {}
    slot_one_counts: dict[str, int] = {name: 0 for name in names}
    for seed in range(seed_count):
        selected, _ = pseudo_random_select_two_avoiding_shared_quota_pool(
            names, quota_pool_identity_by_harness=pools, seed=seed
        )
        key = (selected[0], selected[1])
        ordered_counts[key] = ordered_counts.get(key, 0) + 1
        slot_one_counts[selected[0]] += 1

    legal_pairs = [
        (first, second)
        for first in names
        for second in names
        if first < second and pools[first] != pools[second]
    ]
    assert len(legal_pairs) == 9
    for first, second in legal_pairs:
        forward = ordered_counts.get((first, second), 0)
        backward = ordered_counts.get((second, first), 0)
        total = forward + backward
        assert total > 0, (first, second)
        ratio = forward / total
        assert 0.30 <= ratio <= 0.70, (
            f"{first}/{second} 的 slot 顺序偏斜:{forward} vs {backward}"
        )

    # 修复前最刺眼的症状:某条腿在 slot 1 的次数恒为 0。
    for name in names:
        assert slot_one_counts[name] > seed_count // 20, (name, slot_one_counts)


def test_pool_smaller_than_two_is_rejected():
    with pytest.raises(ValueError):
        pseudo_random_select_two_avoiding_shared_quota_pool(
            ["a"], quota_pool_identity_by_harness={}, seed=1
        )


# --- QP-4 · compute 层 ---------------------------------------------------------


def test_compute_threads_the_quota_pool_identities_through_pseudo_random_selection():
    selection = compute_reviewer_harness_selection(
        main_harness="claude_code",
        available_harnesses=["claude_code", "codex", "oh_my_pi", "kimi_code"],
        quota_pool_identity_by_harness={
            "codex": "openai-codex-subscription",
            "oh_my_pi": "openai-codex-subscription",
            "kimi_code": "kimi-code-subscription",
        },
        selection_seed=5,
    )
    assert selection.selection_mode == "pseudo_random_two"
    assert set(selection.selected_harnesses) != {"codex", "oh_my_pi"}
    assert selection.shared_quota_pool_collision_accepted is False


def test_forced_pair_reports_a_collision_instead_of_silently_pairing_same_pool():
    """受迫配对没有选择余地，但必须把「这两腿同池」如实报出去。"""
    selection = compute_reviewer_harness_selection(
        main_harness="claude_code",
        available_harnesses=["claude_code", "codex", "oh_my_pi"],
        quota_pool_identity_by_harness={
            "codex": "openai-codex-subscription",
            "oh_my_pi": "openai-codex-subscription",
        },
    )
    assert selection.selection_mode == "forced_pair"
    assert set(selection.selected_harnesses) == {"codex", "oh_my_pi"}
    assert selection.shared_quota_pool_collision_accepted is True


def test_collision_flag_reaches_the_telemetry_payload():
    selection = compute_reviewer_harness_selection(
        main_harness="claude_code",
        available_harnesses=["claude_code", "codex", "oh_my_pi"],
        quota_pool_identity_by_harness={
            "codex": "openai-codex-subscription",
            "oh_my_pi": "openai-codex-subscription",
        },
    )
    payload = selection.telemetry_payload()
    assert payload["shared_quota_pool_collision_accepted"] is True


def test_same_harness_double_instance_is_not_reported_as_a_pool_collision():
    """R2 是「只剩一条腿」的既有降级，与额度池无关，不得被这条新维度污染。"""
    selection = compute_reviewer_harness_selection(
        main_harness="claude_code",
        available_harnesses=["codex"],
        quota_pool_identity_by_harness={"codex": "openai-codex-subscription"},
    )
    assert selection.selection_mode == "same_harness_double"
    assert selection.shared_quota_pool_collision_accepted is False


# --- QP-5 · route 层集成 -------------------------------------------------------


def test_route_never_pairs_codex_with_oh_my_pi_when_a_disjoint_pair_exists():
    dispatch = _load_dispatch_reviewers()
    registry = _registry()
    available = sorted(registry["harnesses"])
    for seed in range(120):
        plan = dispatch.route("claude_code", list(available), registry, selection_seed=seed)
        chosen = {reviewer["harness_id"] for reviewer in plan["reviewers"]}
        assert chosen != {"codex", "oh_my_pi"}, (seed, plan["reviewer_selection"])


def test_route_warns_when_it_is_forced_into_a_shared_quota_pool():
    dispatch = _load_dispatch_reviewers()
    registry = _registry()
    plan = dispatch.route("claude_code", ["codex", "oh_my_pi"], registry)
    codes = {warning["code"] for warning in plan["warnings"]}
    assert "reviewer_legs_share_quota_pool" in codes, plan["warnings"]


# --- QP-6 · 生产路径用的是 user-local registry，不是出厂那份 --------------------
#
# QP-1 与 QP-5 都直接读 REGISTRY_PATH（出厂那份），于是它们证明的是「出厂 registry
# 声明完整、据它路由不会配出同池对」。而 dispatch_reviewers 运行时读的是
# `~/.rvf/.../reviewer-registry.json`——一台已部署过的机器上，那份是部署当天的形状，
# 后来新增的指纹字段曾经永远补不进去。结果是：出厂那份完美、契约测试全绿、
# 真机上互斥完全不生效且无声。下面这组把断言挪到「真实部署后的 user-local registry」上。


INSTALLER_PATH = ROOT / "scripts/install_to_codex.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "installer_for_quota_pool_tests", INSTALLER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _deployed_user_local_registry_frozen_before_the_fingerprint_existed(
    tmp_path: Path,
) -> dict:
    """复刻已部署过的机器：cursor/claude_code/codex 三条腿，全都没有指纹字段。"""
    installer = _load_installer()
    home_directory = tmp_path / "home"
    bundled = _registry()
    legacy_harnesses = {}
    for harness_id in ("cursor", "claude_code", "codex"):
        entry = dict(bundled["harnesses"][harness_id])
        entry.pop(QUOTA_POOL_FIELD, None)
        legacy_harnesses[harness_id] = entry
    user_local_registry_path = (
        home_directory
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    user_local_registry_path.parent.mkdir(parents=True, exist_ok=True)
    user_local_registry_path.write_text(
        json.dumps(
            {
                "schema_version": bundled["schema_version"],
                "harnesses": legacy_harnesses,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    installer.initialize_rvf_user_local_data(
        source_repository=ROOT,
        # 夹具播种在默认 home，隔离外层显式数据根覆盖。
        environment={},
        home_directory=home_directory,
    )
    return json.loads(user_local_registry_path.read_text(encoding="utf-8"))


def test_a_previously_deployed_machine_ends_up_declaring_every_quota_pool_identity(
    tmp_path: Path,
):
    merged = _deployed_user_local_registry_frozen_before_the_fingerprint_existed(
        tmp_path
    )["harnesses"]
    undeclared = sorted(
        harness_id
        for harness_id, spec in merged.items()
        if not str(spec.get(QUOTA_POOL_FIELD, "")).strip()
    )
    assert undeclared == [], f"这些 harness 在真实部署后仍未声明指纹: {undeclared}"


def test_route_over_a_previously_deployed_registry_still_avoids_the_shared_pool(
    tmp_path: Path,
):
    """缺陷的直接复现：指纹补不进去时，这里会稳定抽出 {codex, oh_my_pi}。"""
    dispatch = _load_dispatch_reviewers()
    merged = _deployed_user_local_registry_frozen_before_the_fingerprint_existed(
        tmp_path
    )
    available = sorted(merged["harnesses"])
    for seed in range(200):
        plan = dispatch.route("claude_code", list(available), merged, selection_seed=seed)
        chosen = {reviewer["harness_id"] for reviewer in plan["reviewers"]}
        assert chosen != {"codex", "oh_my_pi"}, (seed, plan["reviewer_selection"])
